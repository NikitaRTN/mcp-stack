# -*- coding: utf-8 -*-
"""Windows Firewall integration with an explicit UI confirmation + UAC prompt."""

import os
import subprocess

from . import config
from . import processes


class FirewallError(ValueError):
    pass


ALLOWED_PROFILES = {"private", "domain", "private,domain", "any"}


def _port(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise FirewallError("Порт брандмауэра должен быть числом")
    if not 1 <= value <= 65535:
        raise FirewallError("Порт брандмауэра вне диапазона 1–65535")
    return value


def _profile(value):
    value = str(value or "private,domain").strip().lower().replace(" ", "")
    if value not in ALLOWED_PROFILES:
        raise FirewallError("Профиль должен быть private, domain, private,domain или any")
    return value


def rule_name(port):
    return "MCP Hub HTTPS %d" % _port(port)


def _is_admin():
    if not processes.IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_netsh(arguments, elevate=True, timeout=180):
    if not processes.IS_WINDOWS:
        raise FirewallError("Автонастройка брандмауэра доступна только в Windows")
    if _is_admin() or not elevate:
        try:
            proc = subprocess.run(
                ["netsh.exe"] + list(arguments), capture_output=True, text=True,
                timeout=timeout, creationflags=0x08000000)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FirewallError("Не удалось запустить netsh: %s" % exc)
        if proc.returncode:
            raise FirewallError((proc.stdout or "") + (proc.stderr or "") or
                                "netsh вернул код %d" % proc.returncode)
        return (proc.stdout or "") + (proc.stderr or "")

    powershell = processes.which("powershell") or processes.which("pwsh")
    if not powershell:
        raise FirewallError("Для запроса прав администратора нужен PowerShell")
    quoted = ",".join("'%s'" % str(arg).replace("'", "''") for arg in arguments)
    script = (
        "$p=Start-Process -FilePath 'netsh.exe' -Verb RunAs "
        "-ArgumentList @(%s) -Wait -PassThru; exit $p.ExitCode" % quoted)
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        raise FirewallError("Ожидание разрешения Windows UAC истекло")
    except OSError as exc:
        raise FirewallError("Не удалось открыть Windows UAC: %s" % exc)
    if proc.returncode:
        raise FirewallError("Правило не добавлено: запрос UAC отменён или netsh вернул код %d" %
                            proc.returncode)
    return (proc.stdout or "") + (proc.stderr or "")


def _record(port, profile, present):
    cfg = config.load()
    current = list((cfg.get("firewall") or {}).get("rules") or [])
    name = rule_name(port)
    current = [item for item in current if item.get("name") != name]
    if present:
        current.append({"name": name, "port": int(port), "profile": profile})
    config.update({"firewall": {"rules": current}})


def _recorded(port, cfg=None):
    cfg = cfg or config.load()
    name = rule_name(port)
    return next((item for item in (cfg.get("firewall") or {}).get("rules") or []
                 if item.get("name") == name), None)


def status(port=None):
    cfg = config.load()
    port = _port(port or cfg.get("httpsPort") or 8443)
    recorded = _recorded(port, cfg)
    return {
        "supported": bool(processes.IS_WINDOWS),
        "platform": "windows" if processes.IS_WINDOWS else os.name,
        "port": port,
        "name": rule_name(port),
        "configured": bool(recorded),
        "profile": (recorded or {}).get("profile"),
        "detail": ("Правило MCP Hub записано для TCP %d" % port if recorded
                   else "Правило MCP Hub для TCP %d ещё не добавлено" % port),
    }


def authorize(port, profile="private,domain"):
    port = _port(port)
    profile = _profile(profile)
    name = rule_name(port)
    arguments = [
        "advfirewall", "firewall", "add", "rule",
        "name=%s" % name, "dir=in", "action=allow", "protocol=TCP",
        "localport=%d" % port, "profile=%s" % profile, "enable=yes",
    ]
    _run_netsh(arguments, elevate=True)
    _record(port, profile, True)
    result = status(port)
    result["detail"] = "Разрешён входящий TCP %d (%s)" % (port, profile)
    return result


def remove(port):
    port = _port(port)
    name = rule_name(port)
    _run_netsh(["advfirewall", "firewall", "delete", "rule", "name=%s" % name],
               elevate=True)
    _record(port, "", False)
    result = status(port)
    result["detail"] = "Правило для TCP %d удалено" % port
    return result
