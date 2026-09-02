# -*- coding: utf-8 -*-
"""RPC surface used by the panel.

One transport rule: reads and commands go over a single POST /api/rpc (JSON-RPC
style, batchable), and everything the server wants to tell the browser goes over
the SSE stream. The UI therefore never polls.
"""

import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import caddyfile
from . import firewall
from . import mcp_client
from . import config
from . import installer
from . import logbook
from . import processes
from . import supervisor
from . import telemetry
from .events import BUS

LOG = logbook.LOG

# Методы, которые только читают состояние: писать их в журнал как «действия»
# бессмысленно, иначе журнал забьётся обновлениями панели.
QUIET_METHODS = frozenset((
    "state.get", "calls.list", "calls.stats", "calls.detail", "logs.tail",
    "logs.snapshot", "logs.file", "install.job", "install.detect", "service.logs",
    "firewall.status", "domain.status",
))


class RpcError(Exception):
    def __init__(self, message, status=400, error_id=None):
        super().__init__(message)
        self.status = status
        self.error_id = error_id


def _need(params, key):
    value = (params or {}).get(key)
    if value in (None, ""):
        raise RpcError("Параметр %s обязателен" % key)
    return value


# --------------------------------------------------------------------------- #
#  state / telemetry                                                          #
# --------------------------------------------------------------------------- #

def state_get(params):
    return supervisor.build_state(include_components=bool((params or {}).get("components", True)))


def calls_list(params):
    params = params or {}
    return {
        "calls": telemetry.list_calls(
            service=params.get("service"),
            limit=params.get("limit", 200),
            status=params.get("status"),
            tool=params.get("tool"),
            search=params.get("search"),
            since=params.get("since"),
        )
    }


def calls_detail(params):
    row_id = int(_need(params, "id"))
    rows = telemetry.list_calls(limit=1000)
    for row in rows:
        if row["id"] == row_id:
            return {"call": row}
    raise RpcError("Вызов не найден", 404)


def calls_stats(params):
    params = params or {}
    window = int(params.get("windowSec") or 3600)
    service = params.get("service")
    return {
        "stats": telemetry.stats(service, window),
        "series": telemetry.buckets(service, window, int(params.get("buckets") or 40)),
    }


def calls_purge(params):
    telemetry.purge((params or {}).get("service"))
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  services                                                                   #
# --------------------------------------------------------------------------- #

def _service_public(sid):
    state = supervisor.build_state(include_components=False)
    return next((item for item in state.get("services", []) if item.get("id") == sid),
                {"id": sid})


def service_set_enabled(params):
    sid = _need(params, "id")
    enabled = bool((params or {}).get("enabled"))
    try:
        result = supervisor.set_enabled(sid, enabled)
    except KeyError:
        raise RpcError("Сервис не найден", 404)
    return {"service": _service_public(sid), "notes": result["notes"],
            "state": supervisor.build_state(include_components=False)}


def _service_op(op, sid):
    try:
        return op(sid)
    except KeyError:
        raise RpcError("Сервис не найден", 404)
    except ValueError as exc:
        raise RpcError(str(exc))


def service_start(params):
    pid = _service_op(supervisor.start_service, _need(params, "id"))
    return {"pid": pid}


def service_stop(params):
    return {"stopped": _service_op(supervisor.stop_service, _need(params, "id"))}


def service_restart(params):
    return {"pid": _service_op(supervisor.restart_service, _need(params, "id"))}


EDITABLE_SERVICE_FIELDS = (
    "label", "note", "path", "port", "command", "upstream", "upstreamToken",
    "upstreamPath", "kind", "authMode", "oauth", "upstreamAuthMode", "upstreamOAuth",
)


def _merge_oauth_patch(current, incoming, clear_secret=False):
    merged = dict(current or {})
    if isinstance(incoming, dict):
        for key in config.OAUTH_FIELDS:
            if key not in incoming:
                continue
            value = incoming[key]
            if key == "clientSecret" and value in (None, "") and not clear_secret:
                continue
            merged[key] = value
    if clear_secret:
        merged["clientSecret"] = ""
    return merged


def service_update(params):
    sid = _need(params, "id")
    current = config.service(sid)
    if current is None:
        raise RpcError("Сервис не найден", 404)
    previous_kind = current.get("kind")
    source = params.get("patch") or {}
    patch = {key: value for key, value in source.items()
             if key in EDITABLE_SERVICE_FIELDS}
    if current.get("builtin") and "kind" in patch and patch["kind"] != current.get("kind"):
        raise RpcError("Тип встроенного MCP менять нельзя")
    if "port" in patch:
        try:
            patch["port"] = int(patch["port"])
        except (TypeError, ValueError):
            raise RpcError("Порт должен быть числом")
    if "path" in patch and not config.PATH_RE.match(str(patch["path"])):
        raise RpcError("Некорректный путь маршрута")
    if "oauth" in patch:
        patch["oauth"] = _merge_oauth_patch(
            current.get("oauth"), patch["oauth"], bool(params.get("clearOAuthSecret")))
    if "upstreamOAuth" in patch:
        patch["upstreamOAuth"] = _merge_oauth_patch(
            current.get("upstreamOAuth"), patch["upstreamOAuth"],
            bool(params.get("clearUpstreamOAuthSecret")))
    if patch.get("upstreamToken") in (None, "") and current.get("upstreamToken") and \
            not params.get("clearUpstreamToken"):
        patch.pop("upstreamToken", None)
    try:
        svc = config.set_service(sid, patch)
    except KeyError:
        raise RpcError("Сервис не найден", 404)
    except ValueError as exc:
        raise RpcError(str(exc))
    caddyfile.write()
    if svc.get("enabled"):
        if previous_kind == "stdio" and svc.get("kind") != "stdio":
            supervisor.stop_service(sid)
        if svc.get("kind") == "stdio":
            supervisor.restart_service(sid)
        supervisor.reload_caddy(quiet=True)
    BUS.publish("state.dirty", {})
    return {"service": _service_public(sid)}


def service_create(params):
    try:
        svc = config.add_service(params or {})
    except (TypeError, ValueError) as exc:
        raise RpcError(str(exc))
    caddyfile.write()
    BUS.publish("state.dirty", {})
    return {"service": _service_public(svc["id"])}


def service_delete(params):
    sid = _need(params, "id")
    try:
        supervisor.stop_service(sid)
        config.delete_service(sid)
    except KeyError:
        raise RpcError("Сервис не найден", 404)
    except ValueError as exc:
        raise RpcError(str(exc))
    caddyfile.write()
    supervisor.reload_caddy(quiet=True)
    BUS.publish("state.dirty", {})
    return {"ok": True}


def service_logs(params):
    sid = _need(params, "id")
    path = processes.log_file(supervisor.proc_name(sid))
    return {"path": str(path), "text": processes.tail(path, int((params or {}).get("lines") or 200))}


# --------------------------------------------------------------------------- #
#  caddy / stack                                                              #
# --------------------------------------------------------------------------- #

def caddy_start(_params=None):
    try:
        return {"pid": supervisor.start_caddy()}
    except ValueError as exc:
        raise RpcError(str(exc))


def caddy_stop(_params=None):
    return {"stopped": supervisor.stop_caddy()}


def caddy_restart(_params=None):
    try:
        return {"pid": supervisor.restart_caddy()}
    except ValueError as exc:
        raise RpcError(str(exc))


def caddy_reload(_params=None):
    try:
        return supervisor.reload_caddy()
    except ValueError as exc:
        raise RpcError(str(exc))


def caddy_diagnose(_params=None):
    return supervisor.diagnose_caddy()


def caddy_config(_params=None):
    return {"path": str(caddyfile.path()), "content": caddyfile.render(),
            "validate": supervisor.validate_caddyfile()}


def stack_start(_params=None):
    return supervisor.start_all()


def stack_stop(_params=None):
    supervisor.stop_all()
    return {"ok": True}


def health_check(_params=None):
    cfg = config.load()
    checks = []
    caddy = supervisor.caddy_status()
    checks.append({"id": "caddy", "label": "Caddy",
                   "ok": bool(caddy["listening"]),
                   "detail": "слушает :%s" % cfg["httpsPort"] if caddy["listening"]
                             else "не слушает :%s" % cfg["httpsPort"]})
    checks.append({"id": "inspector", "label": "Инспектор вызовов",
                   "ok": processes.port_open(cfg["inspectorPort"]),
                   "detail": "127.0.0.1:%s" % cfg["inspectorPort"]})
    for svc in config.services(cfg):
        if not svc.get("enabled"):
            checks.append({"id": svc["id"], "label": svc.get("label"), "ok": None,
                           "detail": "выключен — проверка пропущена"})
            continue
        status = supervisor.service_status(svc)
        checks.append({"id": svc["id"], "label": svc.get("label"),
                       "ok": status["state"] == "up", "detail": status["detail"]})
    return {"checkedAt": time.time(), "checks": checks, "cert": supervisor.cert_info()}


# --------------------------------------------------------------------------- #
#  route self-test                                                            #
# --------------------------------------------------------------------------- #

HANDSHAKE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "mcp-hub-selftest", "version": "1"}},
}


def _probe(url, token=None, timeout=12):
    """Send one real MCP handshake and report exactly what answered."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "MCP-Hub-SelfTest",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    body = json.dumps(HANDSHAKE).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = response.read(600).decode("utf-8", "replace")
            return {"status": response.status, "server": response.headers.get("Server"),
                    "body": payload.strip()[:300], "error": None}
    except urllib.error.HTTPError as exc:
        payload = exc.read(600).decode("utf-8", "replace") if exc.fp else ""
        return {"status": exc.code, "server": exc.headers.get("Server") if exc.headers else None,
                "body": payload.strip()[:300], "error": None}
    except Exception as exc:                                  # noqa: BLE001
        return {"status": None, "server": None, "body": "", "error": str(exc)}


def _domain_facts(cfg=None):
    """Everything the panel needs to say out loud about publishing mode."""
    cfg = cfg or config.load()
    domain = (cfg.get("domain") or "localhost").strip()
    port = int(cfg.get("httpsPort") or 8443)
    local = domain in ("localhost", "127.0.0.1", "")
    facts = {
        "domain": domain,
        "httpsPort": port,
        "mode": "local" if local else "public",
        "acme": not local,
        "publicBase": ("http://localhost:%d" % port) if local
                      else "https://%s" % domain,
        "email": cfg.get("email") or "",
        "dns": None,
        "dnsError": None,
        "notes": [],
        "bind": str(cfg.get("bind") or "").strip(),
    }
    if local:
        facts["notes"].append(
            "Локальный режим: наружу не публикуется ничего, сертификат не "
            "запрашивается. Внешний домен обслуживает не этот хаб, поэтому "
            "токен из панели там не действует.")
        return facts
    try:
        facts["dns"] = socket.gethostbyname(domain)
    except OSError as exc:
        facts["dnsError"] = str(exc)
        facts["notes"].append("DNS-имя %s не разрешается — сертификат получить нельзя."
                              % domain)
    if port != 443:
        facts["notes"].append(
            "Caddy слушает локальный порт %d. На роутере нужен проброс "
            "WAN 443 -> этот компьютер:%d; внешний адрес остаётся https://%s."
            % (port, port, domain))
    if not facts["email"]:
        facts["notes"].append("Email не задан — Let's Encrypt пришлёт сертификат, но "
                              "без уведомлений об истечении.")
    return facts


def _config_freshness(caddy=None):
    """Is the running Caddy older than the Caddyfile on disk?"""
    caddy = caddy or supervisor.caddy_status()
    try:
        changed = caddyfile.path().stat().st_mtime
    except OSError:
        return {"stale": False, "detail": "Caddyfile ещё не создан"}
    if not caddy.get("running"):
        return {"stale": False, "fileChangedAt": changed, "detail": "Caddy не запущен"}
    started = caddy.get("startedAt")
    if not started:
        return {"stale": None, "fileChangedAt": changed,
                "detail": "Время запуска Caddy неизвестно"}
    stale = float(started) + 1.0 < changed
    return {
        "stale": stale,
        "fileChangedAt": changed,
        "startedAt": started,
        "detail": ("Caddyfile изменён после запуска Caddy — нужна перезагрузка"
                   if stale else "Работающий Caddy соответствует файлу"),
    }


def domain_status(_params=None):
    """Visible answer to: does the domain work and is the certificate real?"""
    cfg = config.load()
    caddy = supervisor.caddy_status()
    return {
        "checkedAt": time.time(),
        "domain": _domain_facts(cfg),
        "cert": supervisor.cert_info(),
        "caddy": caddy,
        "caddyfile": supervisor.validate_caddyfile(),
        "freshness": _config_freshness(caddy),
    }


def certificate_issue(params=None):
    """Trigger Caddy ACME issuance and return a verified certificate result."""
    cfg = config.load()
    facts = _domain_facts(cfg)
    if facts.get("mode") != "public":
        raise RpcError("Сначала укажите публичный домен вместо localhost")
    if facts.get("dnsError"):
        raise RpcError("DNS домена не разрешается: %s" % facts["dnsError"])
    try:
        wait = float((params or {}).get("wait") or 50)
    except (TypeError, ValueError):
        raise RpcError("Время ожидания сертификата должно быть числом")
    try:
        result = supervisor.ensure_certificate(wait=wait)
    except ValueError as exc:
        raise RpcError(str(exc))
    result["checkedAt"] = time.time()
    result["domain"] = facts
    if result.get("ok"):
        LOG.info("certificate", "SSL-сертификат проверен", event="certificate.ready",
                 domain=facts.get("domain"), issuer=(result.get("cert") or {}).get("issuer"))
    else:
        LOG.warn("certificate", "SSL пока не выпущен", event="certificate.pending",
                 domain=facts.get("domain"), detail=(result.get("cert") or {}).get("detail"))
    BUS.publish("state.dirty", {})
    return result


def route_self_test(params=None):
    """Prove, from the panel, whether the published route accepts the token."""
    cfg = config.load()
    token = cfg.get("token") or ""
    enabled = config.enabled_services(cfg)
    if not enabled:
        raise RpcError("Нет включённых сервисов — включите MCP тумблером")
    wanted = (params or {}).get("service")
    svc = next((s for s in enabled if s["id"] == wanted), None) or enabled[0]
    route = next((r for r in caddyfile.routes(cfg) if r.get("service") == svc["id"]), None)
    if route is None:
        raise RpcError("Для сервиса нет опубликованного маршрута")
    url = route["url"]

    auth_mode = svc.get("authMode", "token")
    access_token = str((params or {}).get("accessToken") or "").strip()
    probe_token = None if auth_mode == "none" else (access_token if auth_mode == "oauth" else token)
    with_token = _probe(url, probe_token)
    without_token = _probe(url, None)

    if with_token["error"]:
        verdict = ("error", "Маршрут недоступен: %s. Проверьте, что Caddy запущен и "
                            "порт открыт снаружи." % with_token["error"])
    elif auth_mode == "none" and with_token["status"] in (200, 202):
        verdict = ("ok", "Маршрут отвечает без авторизации — это соответствует настройке MCP.")
    elif auth_mode == "oauth" and not access_token and without_token["status"] in (401, 403):
        verdict = ("warn", "OAuth включён: запрос без access token отклонён. Для полной "
                           "проверки передайте access token в MCP Studio.")
    elif auth_mode == "oauth" and with_token["status"] in (200, 202) and without_token["status"] in (401, 403):
        verdict = ("ok", "OAuth access token принимается, без токена доступ закрыт.")
    elif auth_mode == "token" and with_token["status"] == 401:
        verdict = ("error", "Caddy отклонил актуальный токен. Почти всегда это значит, "
                            "что работающий Caddy использует старый Caddyfile — нажмите "
                            "«Перезагрузить Caddy» и повторите проверку.")
    elif auth_mode == "token" and with_token["status"] in (200, 202) and without_token["status"] == 401:
        verdict = ("ok", "Токен принимается, без токена доступ закрыт — так и должно быть.")
    elif with_token["status"] in (200, 202):
        verdict = ("warn", "Маршрут отвечает, но фактическая авторизация не соответствует настройке.")
    elif with_token["status"] == 404:
        verdict = ("error", "404: путь %s не опубликован. Сохраните настройки и "
                            "перезагрузите Caddy." % route.get("path"))
    else:
        verdict = ("warn", "Неожиданный ответ HTTP %s от %s."
                   % (with_token["status"], with_token["server"] or "неизвестного сервера"))

    facts = _domain_facts(cfg)
    caddy = supervisor.caddy_status()
    freshness = _config_freshness(caddy)

    if facts["mode"] == "local" and verdict[0] == "ok":
        verdict = ("warn", "Локальный маршрут работает и токен принимается, но домен = "
                           "localhost: наружу хаб ничего не публикует. Внешний адрес "
                           "обслуживает другой сервер со своим токеном — впишите свой "
                           "домен в настройках и примените их.")
    elif freshness.get("stale") and verdict[0] != "ok":
        verdict = ("error", "Caddy работает со старым конфигом: %s. Нажмите "
                            "«Перезагрузить Caddy» и повторите проверку."
                   % freshness["detail"])

    return {
        "checkedAt": time.time(),
        "service": svc["id"],
        "label": svc.get("label"),
        "url": url,
        "authMode": auth_mode,
        "needsAccessToken": auth_mode == "oauth" and not access_token,
        "tokenTail": token[-6:] if auth_mode == "token" and token else "",
        "withToken": with_token,
        "withoutToken": without_token,
        "caddy": caddy,
        "caddyfile": supervisor.validate_caddyfile(),
        "domain": facts,
        "cert": supervisor.cert_info(),
        "freshness": freshness,
        "level": verdict[0],
        "detail": verdict[1],
    }



# --------------------------------------------------------------------------- #
#  developer workspace: MCP tools                                            #
# --------------------------------------------------------------------------- #

def _developer_target(params):
    params = params or {}
    cfg = config.load()
    svc = config.service(params.get("service"), cfg) if params.get("service") else None
    direct = bool(params.get("directUpstream"))
    url = str(params.get("url") or "").strip()
    if not url and svc:
        url = config.upstream_of(svc) if direct else config.public_url(svc, cfg)
    if not url:
        raise RpcError("Укажите MCP URL или выберите сервис")

    auth = dict(params.get("auth") or {})
    mode = str(auth.get("mode") or "").strip()
    if mode == "hub_token":
        auth = {"mode": "bearer", "token": cfg.get("token") or ""}
    elif not mode and svc:
        if direct:
            upstream_mode = svc.get("upstreamAuthMode") or \
                            ("bearer" if svc.get("upstreamToken") else "none")
            if upstream_mode == "bearer":
                auth = {"mode": "bearer", "token": svc.get("upstreamToken") or ""}
            elif upstream_mode == "oauth":
                auth = dict(svc.get("upstreamOAuth") or {})
                auth["mode"] = "oauth_client_credentials"
            else:
                auth = {"mode": "none"}
        else:
            route_mode = svc.get("authMode") or "token"
            if route_mode == "token":
                auth = {"mode": "bearer", "token": cfg.get("token") or ""}
            elif route_mode == "none":
                auth = {"mode": "none"}
            else:
                raise RpcError("Для OAuth-маршрута задайте access token или OAuth Client Credentials")
    elif not mode:
        auth = {"mode": "none"}
    try:
        timeout = max(3, min(120, int(params.get("timeout") or 25)))
    except (TypeError, ValueError):
        raise RpcError("Таймаут должен быть числом")
    return url, auth, timeout, bool(params.get("verifyTls", True))


def mcp_tools_list(params):
    url, auth, timeout, verify_tls = _developer_target(params)
    try:
        return mcp_client.list_tools(url, auth=auth, timeout=timeout,
                                     verify_tls=verify_tls)
    except mcp_client.McpClientError as exc:
        raise RpcError(str(exc), 502)


def mcp_tool_call(params):
    name = _need(params, "name")
    arguments = (params or {}).get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError("Аргументы tool должны быть JSON-объектом")
    url, auth, timeout, verify_tls = _developer_target(params)
    try:
        return mcp_client.call_tool(url, name, arguments=arguments, auth=auth,
                                    timeout=timeout, verify_tls=verify_tls)
    except mcp_client.McpClientError as exc:
        raise RpcError(str(exc), 502)


# --------------------------------------------------------------------------- #
#  Windows Firewall                                                          #
# --------------------------------------------------------------------------- #

def firewall_status(params=None):
    try:
        return firewall.status((params or {}).get("port"))
    except firewall.FirewallError as exc:
        raise RpcError(str(exc))


def firewall_authorize(params):
    try:
        return firewall.authorize(_need(params, "port"),
                                  (params or {}).get("profile") or "private,domain")
    except firewall.FirewallError as exc:
        raise RpcError(str(exc))


def firewall_remove(params):
    try:
        return firewall.remove(_need(params, "port"))
    except firewall.FirewallError as exc:
        raise RpcError(str(exc))

# --------------------------------------------------------------------------- #
#  installer                                                                  #
# --------------------------------------------------------------------------- #

def install_detect(_params=None):
    return installer.summary()


def install_component(params):
    component = _need(params, "component")
    try:
        job = installer.start_job(component)
    except ValueError as exc:
        raise RpcError(str(exc))
    return {"job": job.snapshot()}


def install_job(params):
    snapshot = installer.job(_need(params, "jobId"))
    if snapshot is None:
        raise RpcError("Задача не найдена", 404)
    return {"job": snapshot}


# --------------------------------------------------------------------------- #
#  settings                                                                   #
# --------------------------------------------------------------------------- #

SETTINGS_FIELDS = ("domain", "email", "httpsPort", "adminPort", "inspectorPort",
                   "bind", "telemetryDays", "autoRestart", "openBrowser")


def settings_update(params):
    before = config.load()
    patch = {}
    for key in SETTINGS_FIELDS:
        if key in (params or {}):
            patch[key] = params[key]
    if "domain" in patch:
        domain = str(patch["domain"]).strip()
        if domain and not config.DOMAIN_RE.match(domain):
            raise RpcError("Некорректный домен")
        patch["domain"] = domain or "localhost"
    for key in ("httpsPort", "adminPort", "inspectorPort", "telemetryDays"):
        if key in patch:
            try:
                patch[key] = int(patch[key])
            except (TypeError, ValueError):
                raise RpcError("%s должен быть числом" % key)
    for key in ("httpsPort", "adminPort", "inspectorPort"):
        value = int(patch.get(key, before.get(key) or 0))
        if not 1 <= value <= 65535:
            raise RpcError("%s вне диапазона 1–65535" % key)
    if "telemetryDays" in patch and not 1 <= patch["telemetryDays"] <= 3650:
        raise RpcError("История должна храниться от 1 до 3650 дней")
    combined_ports = {
        "HTTPS": int(patch.get("httpsPort", before.get("httpsPort") or 8443)),
        "панель": int(patch.get("adminPort", before.get("adminPort") or 8765)),
        "инспектор": int(patch.get("inspectorPort", before.get("inspectorPort") or 8770)),
    }
    if len(set(combined_ports.values())) != len(combined_ports):
        raise RpcError("HTTPS-порт, порт панели и порт инспектора должны отличаться")
    for svc in config.services(before):
        if svc.get("kind") == "stdio" and int(svc.get("port") or 0) in combined_ports.values():
            raise RpcError("Порт %s уже занят MCP %s" % (svc.get("port"), svc.get("label")))
    if "bind" in patch:
        bind = str(patch["bind"] or "").strip()
        if bind:
            if bind.isdigit():
                raise RpcError("Bind — это локальный IP, не порт. Для вашей схемы оставьте Bind пустым")
            try:
                ipaddress.ip_address(bind.strip("[]"))
            except ValueError:
                raise RpcError("Bind должен быть пустым или локальным IP-адресом, например 192.168.1.50")
        patch["bind"] = bind
    for key in ("autoRestart", "openBrowser"):
        if key in patch:
            patch[key] = bool(patch[key])

    restart_required = [key for key in ("adminPort", "inspectorPort")
                        if key in patch and patch[key] != before.get(key)]
    cfg = config.update(patch)
    caddyfile.write(cfg)
    check = supervisor.validate_caddyfile()
    applied_live = not restart_required
    if check["ok"] and applied_live:
        supervisor.reload_caddy(quiet=True)
    BUS.publish("state.dirty", {})
    return {
        "config": {key: cfg.get(key) for key in SETTINGS_FIELDS},
        "validate": check,
        "restartRequired": restart_required,
        "appliedLive": applied_live,
    }


def logs_tail(params):
    params = params or {}
    return logbook.LOG.tail(
        limit=params.get("limit", 300),
        level=params.get("level"),
        source=params.get("source"),
        search=params.get("search"),
        since_seq=params.get("sinceSeq"),
        event=params.get("event"),
    )


def logs_snapshot(_params=None):
    return logbook.LOG.snapshot()


def logs_clear(_params=None):
    logbook.LOG.clear()
    LOG.info("panel", "Журнал очищен пользователем", event="logs.clear")
    return {"ok": True}


def logs_file(params):
    """Сырой хвост JSONL-файла — если журнал нужно отдать в поддержку."""
    path = logbook.LOG.path()
    lines = int((params or {}).get("lines") or 500)
    return {"path": str(path), "text": processes.tail(path, lines)}


CLIENT_LEVELS = ("debug", "info", "warn", "error")
CLIENT_LOG_RESERVED_FIELDS = frozenset(("level", "source", "message", "event"))


def client_log(params):
    """Ошибки из браузера попадают в тот же журнал, что и серверные."""
    entries = (params or {}).get("entries")
    if not isinstance(entries, list):
        entries = [params or {}]
    accepted = 0
    for item in entries[:50]:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "error").lower()
        if level not in CLIENT_LEVELS:
            level = "error"
        raw_fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        event = item.get("event") or raw_fields.get("event") or "client"
        fields = {}
        for key, value in list(raw_fields.items())[:40]:
            name = str(key)
            if name in CLIENT_LOG_RESERVED_FIELDS:
                continue
            fields[name[:80]] = value
        LOG.record(level, "browser", item.get("message") or "Ошибка интерфейса",
                   event=str(event)[:60], **fields)
        accepted += 1
    return {"accepted": accepted}


def token_rotate(_params=None):
    token = config.rotate_token()
    caddyfile.write()
    supervisor.reload_caddy(quiet=True)
    BUS.publish("state.dirty", {})
    return {"token": token}


METHODS = {
    "state.get": state_get,
    "calls.list": calls_list,
    "calls.detail": calls_detail,
    "calls.stats": calls_stats,
    "calls.purge": calls_purge,
    "service.setEnabled": service_set_enabled,
    "service.start": service_start,
    "service.stop": service_stop,
    "service.restart": service_restart,
    "service.update": service_update,
    "service.create": service_create,
    "service.delete": service_delete,
    "service.logs": service_logs,
    "mcp.tools.list": mcp_tools_list,
    "mcp.tool.call": mcp_tool_call,
    "caddy.start": caddy_start,
    "caddy.stop": caddy_stop,
    "caddy.restart": caddy_restart,
    "caddy.reload": caddy_reload,
    "caddy.config": caddy_config,
    "caddy.diagnose": caddy_diagnose,
    "stack.start": stack_start,
    "stack.stop": stack_stop,
    "health.check": health_check,
    "route.selfTest": route_self_test,
    "domain.status": domain_status,
    "certificate.issue": certificate_issue,
    "install.detect": install_detect,
    "install.component": install_component,
    "install.job": install_job,
    "settings.update": settings_update,
    "firewall.status": firewall_status,
    "firewall.authorize": firewall_authorize,
    "firewall.remove": firewall_remove,
    "token.rotate": token_rotate,
    "logs.tail": logs_tail,
    "logs.snapshot": logs_snapshot,
    "logs.clear": logs_clear,
    "logs.file": logs_file,
    "client.log": client_log,
}

SLOW_MS = 1500.0        # всё, что дольше, попадает в журнал как предупреждение


def dispatch(method, params):
    """Единая точка входа: каждый вызов измеряется и попадает в журнал.

    Необработанная ошибк�� больше не утекает в консоль: она записывается с
    трассировкой и получает короткий errorId, который виден в интерфейсе и по
    которому подробности находятся во вкладке «Логи».
    """
    handler = METHODS.get(method)
    if handler is None:
        LOG.warn("rpc", "Неизвестный метод %s" % method, event="rpc.unknown",
                 method=method)
        raise RpcError("Неизвестный метод %s" % method, 404)
    started = time.time()
    try:
        result = handler(params or {})
    except RpcError as exc:
        LOG.warn("rpc", "%s: %s" % (method, exc), event="rpc.rejected", method=method,
                 status=exc.status, ms=round((time.time() - started) * 1000, 1),
                 params=params or {})
        raise
    except Exception as exc:                                 # noqa: BLE001
        entry = LOG.exception("rpc", "Метод %s завершился с ошибкой" % method, exc,
                              event="rpc.failed", method=method,
                              ms=round((time.time() - started) * 1000, 1),
                              params=params or {})
        error_id = (entry or {}).get("errorId")
        raise RpcError(
            "Внутренняя ошибка %s. Подробности — во вкладке «Логи» (%s)"
            % (type(exc).__name__, error_id or "без кода"), 500, error_id)
    elapsed = round((time.time() - started) * 1000, 1)
    if elapsed >= SLOW_MS:
        LOG.warn("rpc", "%s ответил медленно" % method, event="rpc.slow",
                 method=method, ms=elapsed)
    elif method in QUIET_METHODS:
        LOG.debug("rpc", "%s → ok" % method, event="rpc.ok", method=method, ms=elapsed)
    else:
        LOG.info("rpc", "%s → ok" % method, event="rpc.ok", method=method, ms=elapsed,
                 params=params or {})
    return result
