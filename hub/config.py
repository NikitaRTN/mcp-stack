# -*- coding: utf-8 -*-
"""Configuration: one JSON file, atomic writes, no drift.

Every derived artefact (Caddyfile, routes, process commands) is generated from
this file, so the domain, the ports and the token can never disagree.
"""

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

# In a PyInstaller one-file build, static assets live in the temporary
# _MEIPASS bundle while mutable state must live next to MCP-Hub.exe. This keeps
# the executable portable and prevents settings from disappearing on restart.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_ROOT
CONFIG_DIR = ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "hub.json"
LOGS = ROOT / "logs"
BIN = ROOT / "bin"
WEB = BUNDLE_ROOT / "web"
DATA = ROOT / "data"

PBKDF2_ITERATIONS = 240_000

# Service templates shipped with the app. Everything is disabled until the user
# switches it on: a service that was never enabled can never report an error.
DEFAULT_SERVICES = [
    {
        "id": "dc",
        "label": "Desktop Commander",
        "path": "/mcp",
        "note": "файлы, shell, процессы на этой машине",
        "kind": "stdio",
        "enabled": False,
        "port": 8000,
        "upstreamPath": "/mcp",
        "command": (
            "npx -y supergateway --stdio \"npx -y @wonderwhy-er/desktop-commander@0.2.48\" "
            "--port {port} --outputTransport streamableHttp --stateful"
        ),
        "requires": ["node"],
        "builtin": True,
    },
    {
        "id": "windows",
        "label": "Windows UI Automation",
        "path": "/windows",
        "note": "скриншоты, доступные элементы, клики и ввод без координат",
        "kind": "stdio",
        "enabled": False,
        "port": 8002,
        "upstreamPath": "/mcp",
        "command": (
            "npx -y supergateway --stdio \"{windowsMcp}\" "
            "--port {port} --outputTransport streamableHttp --stateful"
        ),
        "requires": ["node", "windows"],
        "builtin": True,
    },
    {
        "id": "roblox",
        "label": "Roblox Studio",
        "path": "/roblox",
        "note": "отвечает только при открытой Studio",
        "kind": "stdio",
        "enabled": False,
        "port": 8001,
        "upstreamPath": "/mcp",
        "command": (
            "npx -y supergateway --stdio \"{robloxBat}\" "
            "--port {port} --outputTransport streamableHttp --stateful"
        ),
        "requires": ["node", "robloxBridge"],
        "builtin": True,
    },
    {
        "id": "real",
        "label": "Внешний MCP",
        "path": "/real",
        "note": "любой уже запущенный Streamable HTTP MCP",
        "kind": "remote",
        "enabled": False,
        "upstream": "http://127.0.0.1:3872/mcp",
        "upstreamToken": "",
        "requires": [],
        "builtin": True,
    },
]

DEFAULTS = {
    "version": 3,
    "domain": "localhost",
    "email": "",
    "httpsPort": 8443,
    "adminPort": 8765,
    "inspectorPort": 8770,
    "bind": "",
    "token": "",
    "oauthSigningKey": "",
    "telemetryDays": 14,
    "autoRestart": True,
    "openBrowser": True,
    "services": DEFAULT_SERVICES,
    "auth": {"username": "admin"},
    "firewall": {"rules": []},
}

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,48}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
AUTH_MODES = ("token", "oauth", "none")
UPSTREAM_AUTH_MODES = ("none", "bearer", "oauth")
OAUTH_FIELDS = ("mode", "introspectionUrl", "tokenUrl", "clientId", "clientSecret",
                "scope", "audience", "requiredScopes", "authMethod", "verifyTls")

_lock = threading.RLock()
_cache = None


def _ensure_dirs():
    for folder in (CONFIG_DIR, LOGS, BIN, DATA):
        folder.mkdir(parents=True, exist_ok=True)


def load(force=False):
    """Return the live config. Missing keys fall back to DEFAULTS."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        _ensure_dirs()
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except ValueError:
                backup = CONFIG_PATH.with_suffix(".broken.json")
                CONFIG_PATH.replace(backup)
                raw = {}
        cfg = copy.deepcopy(DEFAULTS)
        for key, value in (raw or {}).items():
            cfg[key] = value
        cfg["services"] = _merge_services(raw.get("services"))
        generated = False
        if not cfg.get("token"):
            cfg["token"] = secrets.token_urlsafe(32)
            generated = True
        if not cfg.get("oauthSigningKey"):
            cfg["oauthSigningKey"] = secrets.token_urlsafe(48)
            generated = True
        if generated:
            _write(cfg)
        _cache = cfg
        return cfg


def _merge_services(stored):
    """Keep built-ins and migrate per-service auth defaults without breaking old configs."""
    stored = list(stored or [])
    by_id = {s.get("id"): s for s in stored if isinstance(s, dict)}
    merged = []
    for template in DEFAULT_SERVICES:
        item = copy.deepcopy(template)
        item.update(by_id.pop(template["id"], {}))
        item["builtin"] = True
        _service_defaults(item)
        merged.append(item)
    for extra in by_id.values():  # user-defined services keep their order
        extra.setdefault("kind", "remote")
        extra.setdefault("enabled", False)
        extra["builtin"] = False
        _service_defaults(extra)
        merged.append(extra)
    return merged


def _service_defaults(item):
    item.setdefault("authMode", "token")
    item.setdefault("oauth", {})
    item.setdefault("upstreamAuthMode", "bearer" if item.get("upstreamToken") else "none")
    item.setdefault("upstreamOAuth", {})
    return item


def _write(cfg):
    _ensure_dirs()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)  # atomic: a crash never leaves half a config


def save(cfg):
    global _cache
    with _lock:
        _write(cfg)
        _cache = cfg
        return cfg


def update(patch):
    with _lock:
        cfg = copy.deepcopy(load())
        cfg.update(patch)
        return save(cfg)


# --------------------------------------------------------------------------- #
#  services                                                                   #
# --------------------------------------------------------------------------- #

def services(cfg=None):
    return (cfg or load())["services"]


def service(sid, cfg=None):
    for item in services(cfg):
        if item.get("id") == sid:
            return item
    return None


def enabled_services(cfg=None):
    return [s for s in services(cfg) if s.get("enabled")]


def set_service(sid, patch):
    """Patch, validate and persist one service. Returns the updated service."""
    with _lock:
        cfg = copy.deepcopy(load())
        for item in cfg["services"]:
            if item.get("id") == sid:
                item.update(copy.deepcopy(patch or {}))
                _service_defaults(item)
                _validate_service(cfg, item)
                save(cfg)
                return item
        raise KeyError(sid)


def add_service(payload):
    with _lock:
        cfg = copy.deepcopy(load())
        sid = str(payload.get("id", "")).strip().lower()
        if not ID_RE.match(sid):
            raise ValueError("id: только латиница, цифры, дефис и подчёркивание")
        if any(s.get("id") == sid for s in cfg["services"]):
            raise ValueError("Сервис с таким id уже есть")
        kind = "stdio" if payload.get("kind") == "stdio" else "remote"
        item = {
            "id": sid,
            "label": str(payload.get("label") or sid),
            "path": str(payload.get("path") or "/" + sid),
            "note": str(payload.get("note") or ""),
            "kind": kind,
            "enabled": False,
            "requires": ["node"] if kind == "stdio" else [],
            "builtin": False,
            "authMode": str(payload.get("authMode") or "token"),
            "oauth": _clean_oauth(payload.get("oauth")),
        }
        if kind == "stdio":
            item.update({
                "port": int(payload.get("port") or 0) or _free_service_port(cfg),
                "upstreamPath": str(payload.get("upstreamPath") or "/mcp"),
                "command": str(payload.get("command") or "").strip(),
                "upstreamAuthMode": "none",
                "upstreamOAuth": {},
            })
        else:
            item.update({
                "upstream": str(payload.get("upstream") or "").strip(),
                "upstreamToken": str(payload.get("upstreamToken") or ""),
                "upstreamAuthMode": str(payload.get("upstreamAuthMode") or
                                        ("bearer" if payload.get("upstreamToken") else "none")),
                "upstreamOAuth": _clean_oauth(payload.get("upstreamOAuth")),
            })
        _service_defaults(item)
        _validate_service(cfg, item)
        cfg["services"].append(item)
        save(cfg)
        return item


def _clean_oauth(value):
    source = value if isinstance(value, dict) else {}
    cleaned = {}
    for key in OAUTH_FIELDS:
        if key not in source:
            continue
        cleaned[key] = bool(source[key]) if key == "verifyTls" else str(source[key] or "").strip()
    mode = cleaned.get("mode") or (
        "introspection" if cleaned.get("introspectionUrl") else "builtin")
    cleaned["mode"] = mode if mode in ("builtin", "introspection") else "builtin"
    cleaned.setdefault("verifyTls", True)
    cleaned.setdefault("authMethod", "basic")
    if cleaned["authMethod"] not in ("basic", "body"):
        raise ValueError("OAuth auth method должен быть basic или body")
    return cleaned


def _validate_http_url(value, label, required=False):
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("%s обязателен" % label)
        return
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("%s должен быть HTTP(S) URL" % label)
    if parts.username or parts.password:
        raise ValueError("Логин и пароль нельзя помещать в %s" % label)


def _validate_service(cfg, item):
    sid = str(item.get("id") or "").strip().lower()
    if not ID_RE.match(sid):
        raise ValueError("Некорректный id сервиса")
    item["id"] = sid
    item["label"] = str(item.get("label") or sid).strip()[:80]
    item["note"] = str(item.get("note") or "").strip()[:300]
    item["path"] = str(item.get("path") or "/" + sid).strip()
    if not PATH_RE.match(item["path"]):
        raise ValueError("Некорректный путь маршрута")
    _assert_unique_path(cfg, item)

    item["authMode"] = str(item.get("authMode") or "token")
    if item["authMode"] not in AUTH_MODES:
        raise ValueError("Режим доступа должен быть token, oauth или none")
    item["oauth"] = _clean_oauth(item.get("oauth"))
    if item["authMode"] == "oauth" and item["oauth"].get("mode") == "introspection":
        _validate_http_url(item["oauth"].get("introspectionUrl"),
                           "OAuth Introspection URL", required=True)

    item["kind"] = "stdio" if item.get("kind") == "stdio" else "remote"
    if item["kind"] == "stdio":
        try:
            item["port"] = int(item.get("port") or 0)
        except (TypeError, ValueError):
            raise ValueError("Локальный порт MCP должен быть числом")
        if not 1 <= item["port"] <= 65535:
            raise ValueError("Локальный порт MCP вне диапазона 1–65535")
        used_global = {int(cfg.get("httpsPort") or 0), int(cfg.get("adminPort") or 0),
                       int(cfg.get("inspectorPort") or 0)}
        if item["port"] in used_global:
            raise ValueError("Порт %d уже используется самим MCP Hub" % item["port"])
        for other in cfg["services"]:
            if other.get("id") != sid and other.get("kind") == "stdio" and \
                    int(other.get("port") or 0) == item["port"]:
                raise ValueError("Порт %d уже занят сервисом %s" %
                                 (item["port"], other.get("id")))
        item["command"] = str(item.get("command") or "").strip()
        if not item["command"]:
            raise ValueError("Для stdio-сервиса нужна команда запуска")
        item["upstreamPath"] = str(item.get("upstreamPath") or "/mcp").strip()
        if not item["upstreamPath"].startswith("/"):
            raise ValueError("Путь локального MCP должен начинаться с /")
        item["upstreamAuthMode"] = "none"
        item["upstreamOAuth"] = {}
    else:
        item["upstream"] = str(item.get("upstream") or "").strip()
        _validate_http_url(item["upstream"], "URL внешнего MCP", required=True)
        item["upstreamAuthMode"] = str(item.get("upstreamAuthMode") or
                                       ("bearer" if item.get("upstreamToken") else "none"))
        if item["upstreamAuthMode"] not in UPSTREAM_AUTH_MODES:
            raise ValueError("Авторизация апстрима должна быть none, bearer или oauth")
        item["upstreamToken"] = str(item.get("upstreamToken") or "")
        item["upstreamOAuth"] = _clean_oauth(item.get("upstreamOAuth"))
        if item["upstreamAuthMode"] == "bearer" and not item["upstreamToken"]:
            raise ValueError("Для Bearer-авторизации апстрима нужен токен")
        if item["upstreamAuthMode"] == "oauth":
            oauth_cfg = item["upstreamOAuth"]
            _validate_http_url(oauth_cfg.get("tokenUrl"), "OAuth Token URL", required=True)
            if not oauth_cfg.get("clientId") or not oauth_cfg.get("clientSecret"):
                raise ValueError("Для OAuth апстрима нужны Client ID и Client Secret")
    return item

def delete_service(sid):
    with _lock:
        cfg = copy.deepcopy(load())
        target = service(sid, cfg)
        if target is None:
            raise KeyError(sid)
        if target.get("builtin"):
            raise ValueError("Встроенный сервис можно только выключить")
        cfg["services"] = [s for s in cfg["services"] if s.get("id") != sid]
        save(cfg)
        return True


def _assert_unique_path(cfg, item):
    for other in cfg["services"]:
        if other.get("id") != item["id"] and other.get("path") == item["path"]:
            raise ValueError("Путь %s уже занят сервисом %s" % (item["path"], other.get("id")))
    if item["path"] in ("/admin", "/healthz"):
        raise ValueError("Путь зарезервирован панелью")


def _free_service_port(cfg):
    used = {int(s.get("port") or 0) for s in cfg["services"]}
    used |= {int(cfg["httpsPort"]), int(cfg["adminPort"]), int(cfg["inspectorPort"])}
    port = 8010
    while port in used:
        port += 1
    return port


def upstream_of(svc):
    """Where the inspector forwards this service's traffic."""
    if svc.get("kind") == "stdio":
        return "http://127.0.0.1:%d%s" % (int(svc.get("port") or 0),
                                          svc.get("upstreamPath") or "/mcp")
    return (svc.get("upstream") or "").strip()


def split_upstream(url):
    """('host', port, '/path') for an upstream URL, or None when unusable."""
    raw = (url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/mcp"
    if parts.query:
        path += "?" + parts.query
    return host, int(port), path, (parts.scheme or "http")


def public_url(svc, cfg=None):
    cfg = cfg or load()
    domain = (cfg.get("domain") or "localhost").strip()
    path = svc.get("path") or "/"
    if domain in ("localhost", "127.0.0.1", ""):
        return "http://localhost:%d%s" % (int(cfg.get("httpsPort") or 8443), path)
    return "https://%s%s" % (domain, path)


def expand_command(svc):
    """Fill {port} / {robloxBat} placeholders in a service command."""
    roblox_bat = ""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roblox_bat = str(Path(local) / "Roblox" / "mcp.bat")
    windows_mcp = str(ROOT / "tools" / "windows_mcp.cmd")
    return (svc.get("command") or "").format(
        port=int(svc.get("port") or 0),
        robloxBat=roblox_bat,
        windowsMcp=windows_mcp,
    )


# --------------------------------------------------------------------------- #
#  auth                                                                       #
# --------------------------------------------------------------------------- #

def has_password(cfg=None):
    return bool((cfg or load()).get("auth", {}).get("hash"))


def set_password(password, username=None):
    if len(password) < 8:
        raise ValueError("Пароль должен быть не короче 8 символов")
    with _lock:
        cfg = copy.deepcopy(load())
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        cfg["auth"] = {
            "username": (username or cfg.get("auth", {}).get("username") or "admin").strip(),
            "algorithm": "pbkdf2_sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(digest).decode("ascii"),
        }
        save(cfg)
        return True


def verify_password(username, password):
    auth = load().get("auth", {})
    if not auth.get("hash"):
        return False
    if (username or "").strip().lower() != str(auth.get("username", "admin")).lower():
        return False
    try:
        salt = base64.b64decode(auth["salt"])
        expected = base64.b64decode(auth["hash"])
        iterations = int(auth.get("iterations", PBKDF2_ITERATIONS))
    except Exception:
        return False
    got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, expected)


def rotate_token():
    return update({"token": secrets.token_urlsafe(32)})["token"]


# --------------------------------------------------------------------------- #
#  migration from the old .env layout                                         #
# --------------------------------------------------------------------------- #

def migrate_from_env(env_path):
    """Import domain/ports/token from a v2 `.env`. Safe to call repeatedly."""
    path = Path(env_path)
    if not path.exists():
        return False
    env = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    if not env:
        return False
    with _lock:
        cfg = copy.deepcopy(load())
        mapping = {
            "MCP_DOMAIN": "domain", "MCP_EMAIL": "email", "MCP_TOKEN": "token",
            "MCP_HTTPS_PORT": "httpsPort", "MCP_ADMIN_PORT": "adminPort",
            "MCP_BIND": "bind",
        }
        for src, dst in mapping.items():
            if env.get(src):
                cfg[dst] = int(env[src]) if dst.endswith("Port") else env[src]
        for svc in cfg["services"]:
            if svc["id"] == "dc" and env.get("MCP_DC_PORT"):
                svc["port"] = int(env["MCP_DC_PORT"])
            if svc["id"] == "roblox" and env.get("MCP_ROBLOX_PORT"):
                svc["port"] = int(env["MCP_ROBLOX_PORT"])
            if svc["id"] == "real" and env.get("MCP_REAL_UPSTREAM"):
                svc["upstream"] = env["MCP_REAL_UPSTREAM"]
                svc["upstreamToken"] = env.get("MCP_REAL_TOKEN", "")
        save(cfg)
        return True
