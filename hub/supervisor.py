# -*- coding: utf-8 -*-
"""Runtime control: start/stop services, keep Caddy in sync, build the state.

Rules that make the app quiet:
  * a disabled service is never started, never probed and never a problem;
  * enabling a service is one action - write the flag, regenerate the
    Caddyfile, start the process, reload Caddy;
  * the watchdog only watches what the user switched on.
"""

import os
import socket
import ssl
import tempfile
import threading
import time

from . import caddyfile
from . import config
from . import installer
from . import processes
from . import telemetry
from .events import BUS

CADDY_NAME = "caddy"


def _caddy_probe_host(cfg=None):
    """Address used for local health/TLS checks.

    An empty/wildcard bind includes loopback.  A concrete bind must be probed
    directly; otherwise a healthy Caddy bound to the LAN address looks down.
    """
    cfg = cfg or config.load()
    bind = str(cfg.get("bind") or "").strip()
    if bind in ("", "0.0.0.0", "::", "[::]", "*"):
        return "127.0.0.1"
    return bind[1:-1] if bind.startswith("[") and bind.endswith("]") else bind


def _caddy_runtime_status(cfg=None, port=None):
    cfg = cfg or config.load()
    port = int(port or cfg.get("httpsPort") or 8443)
    info = processes.status(CADDY_NAME)
    info["probeHost"] = _caddy_probe_host(cfg)
    info["listening"] = processes.port_open(port, info["probeHost"])
    return info
RESTART_WINDOW = 600.0        # seconds
RESTART_LIMIT = 3             # give up after this many restarts per window
_restarts = {}
_lock = threading.RLock()


def proc_name(sid):
    return "svc-%s" % sid


def _public_oauth(value):
    source = value if isinstance(value, dict) else {}
    return {
        "mode": source.get("mode") or (
            "introspection" if source.get("introspectionUrl") else "builtin"),
        "introspectionUrl": source.get("introspectionUrl") or "",
        "tokenUrl": source.get("tokenUrl") or "",
        "clientId": source.get("clientId") or "",
        "scope": source.get("scope") or "",
        "audience": source.get("audience") or "",
        "requiredScopes": source.get("requiredScopes") or "",
        "authMethod": source.get("authMethod") or "basic",
        "verifyTls": bool(source.get("verifyTls", True)),
        "hasClientSecret": bool(source.get("clientSecret")),
    }


# --------------------------------------------------------------------------- #
#  services                                                                   #
# --------------------------------------------------------------------------- #

def service_status(svc):
    """Never probe a disabled service - that is the whole point of the flag."""
    if not svc.get("enabled"):
        return {"state": "off", "running": False, "pid": None, "listening": None,
                "startedAt": None, "detail": "Выключен — ошибки не проверяются"}

    if svc.get("kind") == "stdio":
        info = processes.status(proc_name(svc["id"]), svc.get("port"))
        if info["listening"]:
            state, detail = "up", "Слушает 127.0.0.1:%s" % svc.get("port")
        elif info["running"]:
            state, detail = "starting", "Процесс жив, порт ещё не отвечает"
        else:
            state, detail = "down", "Процесс не запущен"
        info.update({"state": state, "detail": detail})
        return info

    target = config.split_upstream(config.upstream_of(svc))
    if target is None:
        return {"state": "misconfigured", "running": False, "pid": None,
                "listening": False, "startedAt": None, "detail": "Не задан URL апстрима"}
    host, port, _path, _scheme = target
    reachable = processes.port_open(port, host)
    return {
        "state": "up" if reachable else "down",
        "running": reachable, "pid": None, "listening": reachable, "startedAt": None,
        "detail": ("Апстрим отвечает" if reachable
                   else "Апстрим %s:%d не отвечает" % (host, port)),
        "external": True,
    }


def start_service(sid):
    svc = config.service(sid)
    if svc is None:
        raise KeyError(sid)
    if not svc.get("enabled"):
        raise ValueError("Сервис выключен. Сначала включите его тумблером.")
    if svc.get("kind") != "stdio":
        raise ValueError("Внешним MCP управляете вы сами — хаб только проксирует его.")
    command = config.expand_command(svc)
    if not command.strip():
        raise ValueError("У сервиса не задана команда запуска")
    if "node" in (svc.get("requires") or []) and not processes.which("node"):
        raise ValueError("Нужен Node.js — установите его на вкладке «Компоненты»")
    pid = processes.spawn(proc_name(sid), command)
    BUS.publish("service.changed", {"service": sid, "action": "start", "pid": pid})
    return pid


def stop_service(sid):
    svc = config.service(sid)
    if svc is None:
        raise KeyError(sid)
    stopped = processes.stop(proc_name(sid))
    BUS.publish("service.changed", {"service": sid, "action": "stop"})
    return stopped


def restart_service(sid):
    stop_service(sid)
    time.sleep(0.4)
    return start_service(sid)


def set_enabled(sid, enabled):
    """The single switch the whole UI is built around."""
    with _lock:
        svc = config.service(sid)
        if svc is None:
            raise KeyError(sid)
        svc = config.set_service(sid, {"enabled": bool(enabled)})
        caddyfile.write()
        notes = []
        if enabled:
            if svc.get("kind") == "stdio":
                try:
                    start_service(sid)
                    notes.append("процесс запущен")
                except ValueError as exc:
                    notes.append(str(exc))
            else:
                notes.append("внешний MCP должен быть запущен вами")
        else:
            if svc.get("kind") == "stdio":
                stop_service(sid)
                notes.append("процесс остановлен")
            notes.append("маршрут %s больше не публикуется" % (svc.get("path") or ""))
        reload_caddy(quiet=True)
        BUS.publish("service.toggled", {"service": sid, "enabled": bool(enabled),
                                        "notes": notes})
        return {"service": svc, "notes": notes}


# --------------------------------------------------------------------------- #
#  caddy                                                                      #
# --------------------------------------------------------------------------- #

def caddy_status():
    cfg = config.load()
    binary = installer.caddy_path()
    info = _caddy_runtime_status(cfg)
    port_open = bool(info["listening"])
    owned_listening = bool(info["running"] and port_open)
    info.update({
        "installed": binary is not None,
        "binary": str(binary) if binary else None,
        "configPath": str(caddyfile.path()),
        "portOpen": port_open,
        "portConflict": bool(port_open and not info["running"]),
        "listening": owned_listening,
        "state": "up" if owned_listening else ("starting" if info["running"] else "down"),
    })
    return info


def caddy_log(lines=40):
    """Caddy explains every startup failure in its log - so show the log."""
    return processes.tail(processes.log_file(CADDY_NAME), lines=lines)


def port_holder(port, host="127.0.0.1"):
    """Best-effort answer to 'who already owns this port?'."""
    if not port or not processes.port_open(port, host):
        return None
    if processes.IS_WINDOWS:
        _code, out = processes.run('netstat -ano -p tcp | findstr ":%d "' % int(port),
                                   timeout=8)
    else:
        _code, out = processes.run("ss -ltnp sport = :%d" % int(port), timeout=8)
    text = (out or "").strip()
    return text[:600] if text else "порт занят неизвестным процессом"


def start_caddy(wait=6.0):
    """Start Caddy and refuse to claim success until the port really answers."""
    binary = installer.caddy_path()
    if binary is None:
        raise ValueError("Caddy не установлен — нажмите «Установить» на вкладке «Компоненты»")
    caddyfile.write()
    check = validate_caddyfile()
    if not check["ok"]:
        # Starting a process that is guaranteed to die immediately only hides
        # the real reason, so report the validation error instead.
        raise ValueError("Caddyfile не прошёл проверку, Caddy не запущен: %s"
                         % (check["detail"] or "причина не указана"))
    port = int(config.load().get("httpsPort") or 8443)
    cfg = config.load()
    busy_by = port_holder(port, _caddy_probe_host(cfg))
    if busy_by and not processes.status(CADDY_NAME)["running"]:
        raise ValueError("Порт %d уже занят другим процессом:\n%s" % (port, busy_by))
    command = '"%s" run --config "%s"' % (binary, caddyfile.path())
    pid = processes.spawn(CADDY_NAME, command)
    deadline = time.time() + float(wait)
    while time.time() < deadline:
        time.sleep(0.4)
        info = _caddy_runtime_status(cfg, port)
        if info["listening"]:
            return pid
        if not info["running"]:
            break
    if not processes.status(CADDY_NAME)["running"]:
        raise ValueError("Caddy завершился сразу после запуска. Последние строки лога:\n%s"
                         % (caddy_log(25) or "лог пуст"))
    raise ValueError("Caddy работает, но за %.0f с не начал слушать порт %d. Лог:\n%s"
                     % (wait, port, caddy_log(25) or "лог пуст"))


def stop_caddy():
    return processes.stop(CADDY_NAME)


def reload_caddy(quiet=False):
    """Hot reload keeps live MCP sessions alive; fall back to a start."""
    binary = installer.caddy_path()
    if binary is None:
        return {"ok": False, "detail": "Caddy не установлен"}
    caddyfile.write()
    status = _caddy_runtime_status()
    if not status["running"]:
        if quiet:
            return {"ok": False, "detail": "Caddy не запущен"}
        start_caddy()
        return {"ok": True, "detail": "Caddy запущен"}
    code, out = processes.run('"%s" reload --config "%s"' % (binary, caddyfile.path()),
                             timeout=30)
    if code != 0:
        restart_caddy()
        return {"ok": True, "detail": "Reload не удался, выполнен перезапуск", "out": out}
    return {"ok": True, "detail": "Конфигурация перезагружена без разрыва соединений"}


def restart_caddy():
    stop_caddy()
    time.sleep(0.4)
    return start_caddy()


def diagnose_caddy():
    """Every reason Caddy can fail to listen, as a checklist the panel shows."""
    cfg = config.load()
    port = int(cfg.get("httpsPort") or 8443)
    admin_port = int(cfg.get("caddyAdminPort") or 2019)
    binary = installer.caddy_path()
    steps = []

    if binary is None:
        steps.append({"id": "binary", "label": "Бинарник Caddy", "status": "bad",
                      "detail": "Не найден — установите его на вкладке «Компоненты»"})
        return {"checkedAt": time.time(), "level": "error", "steps": steps,
                "log": caddy_log(25), "configPath": str(caddyfile.path()),
                "port": port, "adminPort": admin_port}

    _code, version = processes.run('"%s" version' % binary, timeout=10)
    steps.append({"id": "binary", "label": "Бинарник Caddy", "status": "ok",
                  "detail": "%s (%s)" % ((version or "").strip().splitlines()[0]
                                         if version.strip() else "версия неизвестна",
                                         binary)})

    check = validate_caddyfile()
    steps.append({"id": "config", "label": "Проверка Caddyfile",
                  "status": "ok" if check["ok"] else "bad",
                  "detail": "Синтаксис корректен" if check["ok"]
                            else (check["detail"] or "конфиг отклонён")})

    info = _caddy_runtime_status(cfg, port)
    holder = port_holder(port, info["probeHost"])
    if info["listening"] and info["running"]:
        port_step = {"status": "ok", "detail": "Порт %d слушает наш Caddy" % port}
    elif holder:
        port_step = {"status": "bad",
                     "detail": "Порт %d занят другим процессом:\n%s" % (port, holder)}
    else:
        port_step = {"status": "warn", "detail": "Порт %d свободен — никто его не слушает"
                                                 % port}
    steps.append(dict({"id": "port", "label": "Порт %d" % port}, **port_step))

    admin_busy = processes.port_open(admin_port)
    steps.append({"id": "admin", "label": "Admin API %d" % admin_port,
                  "status": "ok" if (not admin_busy or info["running"]) else "warn",
                  "detail": ("Свободен" if not admin_busy
                             else ("Занят нашим процессом" if info["running"]
                                   else "Занят другим Caddy — горячая перезагрузка попадёт не туда"))})

    steps.append({"id": "process", "label": "Процесс",
                  "status": "ok" if info["running"] else "bad",
                  "detail": ("PID %s, запущен хабом" % info["pid"]) if info["running"]
                            else "Не запущен или завершился — смотрите лог ниже"})

    owned_listener = bool(info["listening"] and info["running"])
    steps.append({"id": "listening", "label": "Приём запросов",
                  "status": "ok" if owned_listener else "bad",
                  "detail": ("Наш Caddy отвечает на %s:%d" % (info["probeHost"], port)
                             if owned_listener else
                             ("Порт отвечает, но процесс не принадлежит MCP Hub"
                              if info["listening"] else
                              "На %s:%d ничего не отвечает" % (info["probeHost"], port)))})

    level = "ok" if all(s["status"] == "ok" for s in steps) else (
        "error" if any(s["status"] == "bad" for s in steps) else "warn")
    return {"checkedAt": time.time(), "level": level, "steps": steps,
            "log": caddy_log(30), "configPath": str(caddyfile.path()),
            "port": port, "adminPort": admin_port}


def validate_caddyfile():
    binary = installer.caddy_path()
    if binary is None:
        return {"ok": False, "detail": "Caddy не установлен"}
    caddyfile.write()
    code, out = processes.run('"%s" validate --config "%s"' % (binary, caddyfile.path()),
                             timeout=25)
    return {"ok": code == 0, "detail": (out or "").strip()[-1500:]}


# --------------------------------------------------------------------------- #
#  whole stack                                                                #
# --------------------------------------------------------------------------- #

def start_all():
    started, notes = [], []
    for svc in config.enabled_services():
        if svc.get("kind") != "stdio":
            continue
        try:
            start_service(svc["id"])
            started.append(svc["id"])
        except ValueError as exc:
            notes.append("%s: %s" % (svc.get("label") or svc["id"], exc))
    try:
        if not processes.status(CADDY_NAME)["running"]:
            start_caddy()
        else:
            reload_caddy(quiet=True)
    except ValueError as exc:
        notes.append(str(exc))
    return {"started": started, "notes": notes}


def stop_all():
    for svc in config.services():
        processes.stop(proc_name(svc["id"]), quiet=True)
    stop_caddy()
    BUS.publish("stack.stopped", {})
    return True


# --------------------------------------------------------------------------- #
#  state                                                                      #
# --------------------------------------------------------------------------- #

def build_state(include_components=True):
    cfg = config.load()
    services = []
    problems = []

    for svc in config.services(cfg):
        status = service_status(svc)
        stats = telemetry.stats(svc["id"], 3600)
        services.append({
            "id": svc["id"],
            "label": svc.get("label") or svc["id"],
            "note": svc.get("note") or "",
            "kind": svc.get("kind"),
            "path": svc.get("path"),
            "port": svc.get("port"),
            "enabled": bool(svc.get("enabled")),
            "builtin": bool(svc.get("builtin")),
            "command": svc.get("command"),
            "upstream": config.upstream_of(svc),
            "upstreamPath": svc.get("upstreamPath") or "/mcp",
            "url": config.public_url(svc, cfg),
            "authMode": svc.get("authMode") or "token",
            "oauth": _public_oauth(svc.get("oauth")),
            "upstreamAuthMode": svc.get("upstreamAuthMode") or (
                "bearer" if svc.get("upstreamToken") else "none"),
            "upstreamOAuth": _public_oauth(svc.get("upstreamOAuth")),
            "hasUpstreamToken": bool(svc.get("upstreamToken")),
            "manageable": svc.get("kind") == "stdio",
            "requires": svc.get("requires") or [],
            "status": status,
            "metrics": {
                "calls1h": stats["total"],
                "failed1h": stats["failed"],
                "errorRate": stats["errorRate"],
                "p50Ms": stats["p50Ms"],
                "p95Ms": stats["p95Ms"],
                "lastCallAt": stats["lastCallAt"],
            },
        })
        # Only enabled services can produce problems.
        if svc.get("enabled"):
            if status["state"] == "down" and svc.get("kind") == "stdio":
                problems.append({
                    "id": "service_down:%s" % svc["id"],
                    "service": svc["id"],
                    "level": "error",
                    "text": "%s включён, но не отвечает на 127.0.0.1:%s"
                            % (svc.get("label"), svc.get("port")),
                    "action": {"method": "service.restart", "params": {"id": svc["id"]},
                               "label": "Перезапустить"},
                })
            elif status["state"] == "down":
                problems.append({
                    "id": "upstream_down:%s" % svc["id"],
                    "service": svc["id"],
                    "level": "warn",
                    "text": "%s: %s" % (svc.get("label"), status["detail"]),
                    "action": {"method": "service.setEnabled",
                               "params": {"id": svc["id"], "enabled": False},
                               "label": "Выключить"},
                })
            elif status["state"] == "misconfigured":
                problems.append({
                    "id": "misconfigured:%s" % svc["id"], "service": svc["id"],
                    "level": "warn", "text": "%s: %s" % (svc.get("label"), status["detail"]),
                })

    caddy = caddy_status()
    enabled_count = len(config.enabled_services(cfg))
    if not caddy["installed"]:
        problems.append({"id": "caddy_missing", "level": "error",
                         "text": "Caddy не установлен — внешние адреса не работают",
                         "action": {"method": "install.component",
                                    "params": {"component": "caddy"},
                                    "label": "Установить"}})
    elif not caddy["listening"] and enabled_count:
        problems.append({"id": "caddy_down", "level": "error",
                         "text": "Caddy не слушает порт %s" % cfg["httpsPort"],
                         "action": {"method": "caddy.restart", "params": {},
                                    "label": "Перезапустить"}})

    state = {
        "now": time.time(),
        "domain": cfg.get("domain"),
        "email": cfg.get("email"),
        "httpsPort": cfg.get("httpsPort"),
        "adminPort": cfg.get("adminPort"),
        "inspectorPort": cfg.get("inspectorPort"),
        "bind": cfg.get("bind"),
        "token": cfg.get("token"),
        "autoRestart": bool(cfg.get("autoRestart")),
        "openBrowser": bool(cfg.get("openBrowser")),
        "telemetryDays": cfg.get("telemetryDays"),
        "firewall": cfg.get("firewall") or {"rules": []},
        "services": services,
        "enabledCount": enabled_count,
        "caddy": caddy,
        "routes": caddyfile.routes(cfg),
        "problems": problems,
        "totals": telemetry.stats(None, 3600),
        "liveListeners": BUS.listeners,
    }
    if include_components:
        state["components"] = installer.summary()
    return state


def _decode_der_certificate(der):
    if not der:
        return {}
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="ascii", suffix=".pem", delete=False) as handle:
            path = handle.name
            handle.write(ssl.DER_cert_to_PEM_cert(der))
        return ssl._ssl._test_decode_cert(path)
    except Exception:
        return {}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _name_parts(value):
    return dict(part for group in (value or ()) for part in group)


def _hostname_matches(pattern, domain):
    pattern = str(pattern or "").rstrip(".").lower()
    domain = str(domain or "").rstrip(".").lower()
    if pattern.startswith("*."):
        return domain.endswith(pattern[1:]) and domain.count(".") == pattern.count(".")
    return pattern == domain


def _certificate_summary(cert, domain, trusted, verification_error="", tls_version="", cipher=""):
    issuer = _name_parts(cert.get("issuer"))
    subject = _name_parts(cert.get("subject"))
    names = [value for kind, value in (cert.get("subjectAltName") or ()) if kind in ("DNS", "IP Address")]
    common_name = subject.get("commonName")
    if not names and common_name:
        names = [common_name]
    domain_matches = bool(trusted or any(_hostname_matches(name, domain) for name in names))
    not_after = cert.get("notAfter")
    expires_at = None
    days_remaining = None
    try:
        if not_after:
            expires_at = ssl.cert_time_to_seconds(not_after)
            days_remaining = int((expires_at - time.time()) / 86400)
    except (TypeError, ValueError, OverflowError):
        pass
    expired = expires_at is not None and expires_at <= time.time()
    issuer_name = issuer.get("organizationName") or issuer.get("commonName") or "unknown"
    self_signed = bool(cert.get("issuer") == cert.get("subject") or issuer_name == "Caddy Local Authority")
    ok = bool(trusted and domain_matches and not expired)
    detail = ("Доверенный SSL-сертификат активен" if ok else
              "TLS отвечает, но сертификат не прошёл проверку: %s" % verification_error if verification_error else
              "TLS отвечает, но сертификат не является доверенным для домена")
    return {"applicable": True, "ok": ok, "trusted": bool(trusted),
            "domainMatches": domain_matches, "issuer": issuer_name,
            "subject": common_name, "names": names, "notBefore": cert.get("notBefore"),
            "notAfter": not_after, "expiresAt": expires_at, "daysRemaining": days_remaining,
            "expired": expired, "selfSigned": self_signed, "tlsVersion": tls_version,
            "cipher": cipher, "detail": detail}


def _read_tls_peer(host, port, domain, timeout, verify):
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            cert = tls.getpeercert() or _decode_der_certificate(tls.getpeercert(binary_form=True))
            cipher = tls.cipher()
            return cert, tls.version() or "", cipher[0] if cipher else ""


def cert_info(timeout=3.0):
    cfg = config.load()
    domain = (cfg.get("domain") or "").strip()
    if domain in ("", "localhost", "127.0.0.1"):
        return {"applicable": False, "ok": False,
                "detail": "Локальный режим — публичный сертификат не запрашивается"}
    port = int(cfg.get("httpsPort") or 8443)
    probe_host = _caddy_probe_host(cfg)
    try:
        cert, tls_version, cipher = _read_tls_peer(probe_host, port, domain, timeout, True)
        return _certificate_summary(cert, domain, True, tls_version=tls_version, cipher=cipher)
    except ssl.SSLCertVerificationError as exc:
        verification_error = str(exc)
    except (OSError, ssl.SSLError) as exc:
        return {"applicable": True, "ok": False, "trusted": False,
                "detail": "Не удалось подключиться к TLS: %s" % exc}
    try:
        cert, tls_version, cipher = _read_tls_peer(probe_host, port, domain, timeout, False)
        return _certificate_summary(cert, domain, False, verification_error, tls_version, cipher)
    except (OSError, ssl.SSLError) as exc:
        return {"applicable": True, "ok": False, "trusted": False,
                "detail": "TLS недоступен после ошибки проверки: %s; %s" % (verification_error, exc)}


def ensure_certificate(wait=50.0):
    cfg = config.load()
    domain = (cfg.get("domain") or "").strip()
    if domain in ("", "localhost", "127.0.0.1"):
        raise ValueError("Укажите публичный домен вместо localhost")
    wait = max(5.0, min(90.0, float(wait or 50.0)))
    caddyfile.write(cfg)
    check = validate_caddyfile()
    if not check.get("ok"):
        raise ValueError("Caddyfile не прошёл проверку: %s" % (check.get("detail") or "неизвестная ошибка"))
    before = cert_info(timeout=2.0)
    status = caddy_status()
    if status.get("running"):
        action = reload_caddy()
    else:
        start_caddy()
        action = {"ok": True, "detail": "Caddy запущен; ACME-запрос отправлен"}
    deadline = time.time() + wait
    certificate = cert_info(timeout=2.0)
    while not certificate.get("ok") and time.time() < deadline:
        if not caddy_status().get("running"):
            break
        time.sleep(min(2.0, max(0.1, deadline - time.time())))
        certificate = cert_info(timeout=2.0)
    return {"ok": bool(certificate.get("ok")), "alreadyValid": bool(before.get("ok")),
            "action": action, "cert": certificate, "caddy": caddy_status(),
            "caddyfile": check,
            "waitedSeconds": round(max(0.0, wait - max(0.0, deadline - time.time())), 1),
            "log": caddy_log(35)}


# --------------------------------------------------------------------------- #
#  watchdog                                                                   #
# --------------------------------------------------------------------------- #

class Watchdog(threading.Thread):
    """Restarts only what the user enabled, with a bounded retry budget."""

    def __init__(self, interval=6.0):
        super().__init__(name="watchdog", daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self._last = {}

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as exc:                 # noqa: BLE001
                BUS.publish("watchdog.error", {"detail": str(exc)})

    def _tick(self):
        cfg = config.load(force=True)
        changed = False
        for svc in config.services(cfg):
            status = service_status(svc)
            key = svc["id"]
            if self._last.get(key) != status["state"]:
                self._last[key] = status["state"]
                changed = True
                BUS.publish("service.state", {"service": key, "state": status["state"],
                                              "detail": status["detail"]})
            if (cfg.get("autoRestart") and svc.get("enabled")
                    and svc.get("kind") == "stdio" and status["state"] == "down"):
                self._maybe_restart(svc)
        if config.enabled_services(cfg):
            caddy = _caddy_runtime_status(cfg)
            if self._last.get("__caddy") != caddy["listening"]:
                self._last["__caddy"] = caddy["listening"]
                changed = True
        if changed:
            BUS.publish("state.dirty", {})

    def _maybe_restart(self, svc):
        now = time.time()
        history = [t for t in _restarts.get(svc["id"], []) if now - t < RESTART_WINDOW]
        if len(history) >= RESTART_LIMIT:
            BUS.publish("service.giveup", {
                "service": svc["id"],
                "detail": "%d перезапуска за 10 минут — автовосстановление остановлено"
                          % len(history),
            })
            return
        history.append(now)
        _restarts[svc["id"]] = history
        try:
            start_service(svc["id"])
            BUS.publish("service.autorestart", {"service": svc["id"], "attempt": len(history)})
        except ValueError as exc:
            BUS.publish("service.giveup", {"service": svc["id"], "detail": str(exc)})
