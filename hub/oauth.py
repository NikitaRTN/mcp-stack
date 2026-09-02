# -*- coding: utf-8 -*-
"""OAuth 2.0 helpers used by MCP routes and the developer tools explorer.

Two deliberately explicit flows are supported:
  * client credentials for an outbound MCP connection;
  * RFC 7662 token introspection for protecting one published MCP route.

Secrets never leave this module in returned dictionaries and cache keys contain
only SHA-256 digests.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from . import config


class OAuthError(ValueError):
    def __init__(self, message, status=400, oauth_error="invalid_request"):
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error


_cache_lock = threading.RLock()
_token_cache = {}
_introspection_cache = {}
MAX_RESPONSE = 1_000_000


def _url(value, label):
    raw = str(value or "").strip()
    if not raw:
        raise OAuthError("%s не задан" % label)
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OAuthError("%s должен быть HTTP(S) URL" % label)
    if parts.username or parts.password:
        raise OAuthError("Логин и пароль нельзя помещать в URL")
    return raw


def _context(verify_tls=True):
    return ssl.create_default_context() if verify_tls else ssl._create_unverified_context()


def _post_form(url, fields, client_id="", client_secret="", timeout=12,
               verify_tls=True, auth_method="basic"):
    target = _url(url, "OAuth endpoint")
    payload = dict((key, value) for key, value in fields.items()
                   if value not in (None, "", []))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "MCP-Hub-OAuth/1",
    }
    if client_id:
        if auth_method == "body":
            payload["client_id"] = client_id
            if client_secret:
                payload["client_secret"] = client_secret
        else:
            credentials = (str(client_id) + ":" + str(client_secret or "")).encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    request = urllib.request.Request(
        target, data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=max(2, int(timeout)),
                                    context=_context(verify_tls)) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise OAuthError("OAuth endpoint вернул слишком большой ответ")
    except urllib.error.HTTPError as exc:
        body = exc.read(3000).decode("utf-8", "replace") if exc.fp else ""
        raise OAuthError("OAuth endpoint ответил HTTP %d: %s" %
                         (exc.code, body.strip()[:600] or exc.reason))
    except (urllib.error.URLError, OSError) as exc:
        raise OAuthError("OAuth endpoint недоступен: %s" % getattr(exc, "reason", exc))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise OAuthError("OAuth endpoint вернул не JSON")
    if not isinstance(data, dict):
        raise OAuthError("OAuth endpoint вернул неожиданный ответ")
    return data


def _cache_key(settings, extra=""):
    safe = {
        "tokenUrl": settings.get("tokenUrl"),
        "introspectionUrl": settings.get("introspectionUrl"),
        "clientId": settings.get("clientId"),
        "clientSecret": settings.get("clientSecret"),
        "scope": settings.get("scope"),
        "audience": settings.get("audience"),
        "requiredScopes": settings.get("requiredScopes"),
        "authMethod": settings.get("authMethod"),
        "extra": extra,
    }
    raw = json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def client_credentials(settings, timeout=12, verify_tls=True):
    settings = settings or {}
    token_url = _url(settings.get("tokenUrl"), "Token URL")
    client_id = str(settings.get("clientId") or "").strip()
    client_secret = str(settings.get("clientSecret") or "")
    if not client_id or not client_secret:
        raise OAuthError("Для OAuth Client Credentials нужны Client ID и Client Secret")
    key = _cache_key(settings)
    now = time.time()
    with _cache_lock:
        cached = _token_cache.get(key)
        if cached and cached["expiresAt"] > now:
            return cached["token"]
    data = _post_form(
        token_url,
        {
            "grant_type": "client_credentials",
            "scope": settings.get("scope") or "",
            "audience": settings.get("audience") or "",
        },
        client_id=client_id,
        client_secret=client_secret,
        timeout=timeout,
        verify_tls=verify_tls,
        auth_method=settings.get("authMethod") or "basic",
    )
    token = str(data.get("access_token") or "")
    if not token:
        raise OAuthError("OAuth endpoint не вернул access_token")
    try:
        lifetime = max(30, min(86400, int(data.get("expires_in") or 300)))
    except (TypeError, ValueError):
        lifetime = 300
    with _cache_lock:
        _token_cache[key] = {"token": token, "expiresAt": now + max(10, lifetime - 30)}
    return token


def _scopes(value):
    if isinstance(value, (list, tuple, set)):
        return set(str(item).strip() for item in value if str(item).strip())
    return set(str(value or "").replace(",", " ").split())


def introspect(settings, token, timeout=8, verify_tls=True):
    settings = settings or {}
    token = str(token or "").strip()
    if not token:
        return {"active": False, "detail": "Access token отсутствует"}
    endpoint = _url(settings.get("introspectionUrl"), "Introspection URL")
    key = _cache_key(settings, hashlib.sha256(token.encode("utf-8")).hexdigest())
    now = time.time()
    with _cache_lock:
        cached = _introspection_cache.get(key)
        if cached and cached["cacheUntil"] > now:
            return dict(cached["result"])
    data = _post_form(
        endpoint,
        {"token": token, "token_type_hint": "access_token"},
        client_id=str(settings.get("clientId") or ""),
        client_secret=str(settings.get("clientSecret") or ""),
        timeout=timeout,
        verify_tls=verify_tls,
        auth_method=settings.get("authMethod") or "basic",
    )
    active = data.get("active") is True or str(data.get("active")).lower() == "true"
    granted = _scopes(data.get("scope"))
    required = _scopes(settings.get("requiredScopes"))
    missing = sorted(required - granted)
    if active and missing:
        result = {"active": False, "forbidden": True,
                  "detail": "Не хватает OAuth scope: %s" % ", ".join(missing)}
    else:
        result = {
            "active": bool(active),
            "detail": "OAuth token активен" if active else "OAuth token неактивен",
            "scope": sorted(granted),
            "subject": data.get("sub") or data.get("username"),
            "clientId": data.get("client_id"),
            "expiresAt": data.get("exp"),
        }
    ttl = 20
    try:
        if data.get("exp"):
            ttl = max(2, min(30, int(data["exp"]) - int(now)))
    except (TypeError, ValueError):
        pass
    with _cache_lock:
        _introspection_cache[key] = {"result": dict(result), "cacheUntil": now + ttl}
    return result


def bearer_from_header(header):
    scheme, _, value = str(header or "").strip().partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else ""


def validate_incoming(settings, authorization, timeout=8, resource=None):
    if (settings or {}).get("mode") == "builtin":
        return validate_builtin(settings or {}, authorization, resource=resource)
    token = bearer_from_header(authorization)
    if not token:
        return False, 401, "Требуется OAuth Bearer access token"
    try:
        result = introspect(settings, token, timeout=timeout,
                            verify_tls=bool((settings or {}).get("verifyTls", True)))
    except OAuthError as exc:
        return False, 503, str(exc)
    if result.get("active"):
        return True, 200, result.get("detail") or "OAuth token активен"
    return False, 403 if result.get("forbidden") else 401, result.get("detail") or "OAuth token отклонён"


# --------------------------------------------------------------------------- #
#  Built-in OAuth Authorization Server: discovery, DCR, Authorization Code    #
#  with mandatory PKCE S256, resource-bound access and refresh tokens.        #
# --------------------------------------------------------------------------- #

BUILTIN_SCOPE = "mcp:tools"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
MAX_CLIENTS = 200
_oauth_lock = threading.RLock()
_codes = {}
_password_failures = {}


def public_base(cfg=None):
    cfg = cfg or config.load()
    domain = str(cfg.get("domain") or "localhost").strip()
    if domain in ("", "localhost", "127.0.0.1"):
        return "http://localhost:%d" % int(cfg.get("httpsPort") or 8443)
    return "https://" + domain


def _scope_list(value):
    values = value if isinstance(value, (list, tuple, set)) else \
        str(value or "").replace(",", " ").split()
    result = []
    for item in values:
        item = str(item or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _required(settings):
    return _scope_list((settings or {}).get("requiredScopes")) or [BUILTIN_SCOPE]


def _builtin_services(cfg=None):
    cfg = cfg or config.load()
    return [service for service in config.enabled_services(cfg)
            if (service.get("authMode") or "token") == "oauth"
            and (service.get("oauth") or {}).get("mode") == "builtin"]


def _resource_service(resource, cfg=None):
    cfg = cfg or config.load()
    wanted = str(resource or "").rstrip("/")
    return next((service for service in _builtin_services(cfg)
                 if config.public_url(service, cfg).rstrip("/") == wanted), None)


def resource_metadata_url(service, cfg=None):
    return (public_base(cfg) + "/.well-known/oauth-protected-resource"
            + str((service or {}).get("path") or ""))


def challenge(service, cfg=None, error="invalid_token"):
    return ('Bearer realm="MCP Hub", resource_metadata="%s", scope="%s", error="%s"'
            % (resource_metadata_url(service, cfg),
               " ".join(_required((service or {}).get("oauth"))), error))


def authorization_server_metadata(cfg=None):
    cfg = cfg or config.load()
    base = public_base(cfg)
    scopes = [BUILTIN_SCOPE]
    for service in _builtin_services(cfg):
        for scope in _required(service.get("oauth")):
            if scope not in scopes:
                scopes.append(scope)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": scopes,
        "resource_parameter_supported": True,
    }


def protected_resource_metadata(request_path, cfg=None):
    cfg = cfg or config.load()
    prefix = "/.well-known/oauth-protected-resource"
    path = urllib.parse.urlsplit(str(request_path or "")).path
    suffix = path[len(prefix):] if path.startswith(prefix) else ""
    services = _builtin_services(cfg)
    service = next((item for item in services
                    if suffix and str(item.get("path") or "").rstrip("/")
                    == suffix.rstrip("/")), None)
    if service is None and not suffix and len(services) == 1:
        service = services[0]
    if service is None:
        raise OAuthError("OAuth-ресурс не найден или не включён", 404, "invalid_target")
    return {
        "resource": config.public_url(service, cfg),
        "authorization_servers": [public_base(cfg)],
        "bearer_methods_supported": ["header"],
        "scopes_supported": _required(service.get("oauth")),
        "resource_name": service.get("label") or service.get("id") or "MCP Hub",
    }


def _client_path():
    return config.DATA / "oauth-clients.json"


def _read_clients():
    try:
        data = json.loads(_client_path().read_text(encoding="utf-8"))
        return data.get("clients", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_clients(clients):
    path = _client_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"clients": clients}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _valid_redirect(uri):
    try:
        parsed = urllib.parse.urlsplit(str(uri or ""))
    except ValueError:
        return False
    if len(str(uri or "")) > 2048 or not parsed.hostname or parsed.fragment \
            or parsed.username or parsed.password:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and
        parsed.hostname in ("localhost", "127.0.0.1", "::1"))


def register_client(payload):
    if not isinstance(payload, dict):
        raise OAuthError("Ожидался JSON-объект", 400, "invalid_client_metadata")
    redirects = payload.get("redirect_uris")
    if not isinstance(redirects, list) or not 1 <= len(redirects) <= 10 \
            or any(not _valid_redirect(uri) for uri in redirects):
        raise OAuthError("Нужны HTTPS redirect_uris; HTTP разрешён только для localhost",
                         400, "invalid_redirect_uri")
    grants = payload.get("grant_types") or ["authorization_code", "refresh_token"]
    responses = payload.get("response_types") or ["code"]
    if "authorization_code" not in grants or any(
            grant not in ("authorization_code", "refresh_token") for grant in grants):
        raise OAuthError("Поддерживаются authorization_code и refresh_token",
                         400, "invalid_client_metadata")
    if set(responses) != {"code"}:
        raise OAuthError("Поддерживается только response_type=code",
                         400, "invalid_client_metadata")
    if (payload.get("token_endpoint_auth_method") or "none") != "none":
        raise OAuthError("Динамический клиент должен использовать PKCE без client_secret",
                         400, "invalid_client_metadata")
    now = int(time.time())
    client_id = "mcp_" + secrets.token_urlsafe(24)
    record = {
        "client_id": client_id,
        "client_name": str(payload.get("client_name") or "MCP client")[:120],
        "redirect_uris": list(dict.fromkeys(str(item) for item in redirects)),
        "grant_types": list(grants),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": now,
    }
    with _oauth_lock:
        clients = _read_clients()
        if len(clients) >= MAX_CLIENTS:
            oldest = sorted(clients, key=lambda key: int(
                (clients[key] or {}).get("client_id_issued_at") or 0))
            for key in oldest[:len(clients) - MAX_CLIENTS + 1]:
                clients.pop(key, None)
        clients[client_id] = record
        _write_clients(clients)
    return dict(record)


def _client(client_id):
    with _oauth_lock:
        return _read_clients().get(str(client_id or ""))


def _one(params, name, default=""):
    value = (params or {}).get(name, default)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value if value is not None else default)


def _param(value, name, limit=2048, required=True):
    value = str(value or "")
    if required and not value:
        raise OAuthError("Не указан %s" % name)
    if len(value) > limit:
        raise OAuthError("Параметр %s слишком длинный" % name)
    return value


def authorization_request(params, cfg=None):
    cfg = cfg or config.load()
    if _one(params, "response_type") != "code":
        raise OAuthError("Поддерживается только response_type=code", 400,
                         "unsupported_response_type")
    client_id = _param(_one(params, "client_id"), "client_id", 256)
    client = _client(client_id)
    if not client:
        raise OAuthError("OAuth-клиент не зарегистрирован", 400, "unauthorized_client")
    redirect = _param(_one(params, "redirect_uri"), "redirect_uri")
    if redirect not in client.get("redirect_uris", []):
        raise OAuthError("redirect_uri не зарегистрирован")
    code_challenge = _param(_one(params, "code_challenge"), "code_challenge", 128)
    allowed_challenge = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if _one(params, "code_challenge_method") != "S256" or not 43 <= len(code_challenge) <= 128 \
            or any(char not in allowed_challenge for char in code_challenge):
        raise OAuthError("Требуется корректный PKCE S256 code_challenge")
    resource = _one(params, "resource")
    services = _builtin_services(cfg)
    if not resource and len(services) == 1:
        resource = config.public_url(services[0], cfg)
    resource = _param(resource, "resource")
    service = _resource_service(resource, cfg)
    if not service:
        raise OAuthError("MCP resource не найден или не использует встроенный OAuth",
                         400, "invalid_target")
    supported = _required(service.get("oauth"))
    requested = _scope_list(_one(params, "scope")) or list(supported)
    if set(requested) != set(supported):
        raise OAuthError("Запрошены неверные scopes", 400, "invalid_scope")
    return {
        "response_type": "code", "client_id": client_id,
        "client_name": client.get("client_name") or "MCP client",
        "redirect_uri": redirect, "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": _param(_one(params, "state"), "state", required=False),
        "resource": resource,
        "resource_name": service.get("label") or service.get("id") or "MCP Hub",
        "scope": " ".join(requested),
    }


def _redirect(uri, values):
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value not in (None, ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    urllib.parse.urlencode(query), parsed.fragment))


def denied_redirect(details):
    return _redirect(details["redirect_uri"], {
        "error": "access_denied", "state": details.get("state")})


def verify_owner_password(password, client_ip="unknown"):
    now = time.time()
    key = str(client_ip or "unknown")
    with _oauth_lock:
        state = _password_failures.get(key, {"count": 0, "until": 0.0})
        if state["until"] > now:
            return False, "Слишком много попыток. Повторите через несколько минут."
        username = config.load().get("auth", {}).get("username", "admin")
        if config.verify_password(username, password or ""):
            _password_failures.pop(key, None)
            return True, ""
        state["count"] += 1
        if state["count"] >= 6:
            state = {"count": 0, "until": now + 900}
        _password_failures[key] = state
    time.sleep(0.4)
    return False, "Неверный пароль MCP Hub"


def authorization_page(details, error=""):
    hidden = "".join('<input type="hidden" name="%s" value="%s">' %
                     (html.escape(key, quote=True),
                      html.escape(str(details.get(key) or ""), quote=True))
                     for key in ("response_type", "client_id", "redirect_uri",
                                 "code_challenge", "code_challenge_method", "state",
                                 "resource", "scope"))
    error_box = ('<div class="error" role="alert">%s</div>' % html.escape(error)) if error else ""
    setup_box = "" if config.has_password() else \
        '<div class="error">Сначала задайте пароль в локальной панели MCP Hub.</div>'
    disabled = "" if config.has_password() else " disabled"
    scopes = "".join('<span class="scope">%s</span>' % html.escape(item)
                     for item in _scope_list(details.get("scope")))
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключение к MCP Hub</title><style>
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:#070b14;color:#f4f7fb}*{box-sizing:border-box}body{min-height:100vh;margin:0;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 50%% 0,#16233c 0,transparent 42%%),#070b14}.card{width:min(100%%,480px);padding:28px;border:1px solid #2a3854;border-radius:22px;background:rgba(15,22,36,.96);box-shadow:0 24px 80px #0008}.brand{display:flex;gap:12px;align-items:center;margin-bottom:24px}.logo{display:grid;place-items:center;width:44px;height:44px;border-radius:13px;background:linear-gradient(145deg,#6ea8fe,#8b5cf6);font-size:23px}.eyebrow{color:#9fb2d1;font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}h1{margin:3px 0 0;font-size:24px;line-height:1.15}.request{padding:16px;border:1px solid #283652;border-radius:15px;background:#0a101d}.request strong{display:block;font-size:16px}.request p{margin:7px 0 0;color:#aebbd0;font-size:14px;line-height:1.45;overflow-wrap:anywhere}.scopes{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}.scope{padding:5px 8px;border-radius:999px;background:#1b2a45;color:#b9d4ff;font:700 12px ui-monospace,monospace}label{display:block;margin-top:18px;font-size:13px;font-weight:800;color:#dbe6f7}input[type=password]{width:100%%;margin-top:8px;padding:13px 14px;border:1px solid #334464;border-radius:12px;background:#070b14;color:#fff;font:inherit;outline:none}input[type=password]:focus{border-color:#78a9ff;box-shadow:0 0 0 3px #4b83e633}.actions{display:grid;grid-template-columns:1fr 1.5fr;gap:10px;margin-top:18px}button{min-height:44px;border:0;border-radius:12px;font:800 14px inherit;cursor:pointer}.deny{border:1px solid #334464;background:transparent;color:#c7d2e4}.allow{background:linear-gradient(135deg,#6ea8fe,#8b5cf6);color:#07101f}.error{margin-top:14px;padding:11px 12px;border:1px solid #7d3042;border-radius:11px;background:#351522;color:#ffc7d1;font-size:13px}.fine{margin:16px 0 0;color:#7f8da5;font-size:12px;line-height:1.5}@media(max-width:420px){.card{padding:21px}.actions{grid-template-columns:1fr}}
</style></head><body><main class="card"><div class="brand"><div class="logo">🛡️</div><div><div class="eyebrow">MCP Hub OAuth</div><h1>Разрешить подключение?</h1></div></div><div class="request"><strong>%s</strong><p>Клиент <b>%s</b> запрашивает доступ к:<br>%s</p><div class="scopes">%s</div></div>%s%s<form method="post" action="/oauth/authorize">%s<label for="password">Пароль администратора</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus%s><div class="actions"><button class="deny" name="decision" value="deny" formnovalidate>Отменить</button><button class="allow" name="decision" value="allow"%s>Разрешить</button></div></form><p class="fine">Пароль проверяется локально и не передаётся клиенту. Доступ ограничен выбранным MCP-ресурсом.</p></main></body></html>""" % (
        html.escape(str(details.get("resource_name") or "MCP Hub")),
        html.escape(str(details.get("client_name") or "MCP client")),
        html.escape(str(details.get("resource") or "")), scopes, setup_box,
        error_box, hidden, disabled, disabled)


def _issue_code(details):
    code = secrets.token_urlsafe(32)
    record = {key: details.get(key) for key in
              ("client_id", "redirect_uri", "code_challenge", "resource", "scope")}
    record["exp"] = time.time() + CODE_TTL
    with _oauth_lock:
        now = time.time()
        for key in [key for key, value in _codes.items()
                    if float((value or {}).get("exp") or 0) <= now]:
            _codes.pop(key, None)
        _codes[code] = record
    return code


def authorized_redirect(details):
    return _redirect(details["redirect_uri"], {
        "code": _issue_code(details), "state": details.get("state")})


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))


def _key():
    value = str(config.load().get("oauthSigningKey") or "")
    if not value:
        raise OAuthError("Не создан ключ подписи OAuth", 503, "temporarily_unavailable")
    return hashlib.sha256(value.encode("utf-8")).digest()


def _issue_token(kind, client_id, resource, scope, ttl):
    now = int(time.time())
    payload = {"typ": kind, "client_id": client_id, "resource": resource,
               "scope": _scope_list(scope), "iat": now, "exp": now + int(ttl),
               "jti": secrets.token_urlsafe(12)}
    body = _b64(json.dumps(payload, separators=(",", ":"),
                            sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(_key(), ("mh1." + body).encode("ascii"),
                              hashlib.sha256).digest())
    return "mh1.%s.%s" % (body, signature)


def _decode_token(token, kind):
    try:
        prefix, body, signature = str(token or "").split(".", 2)
        expected = _b64(hmac.new(_key(), (prefix + "." + body).encode("ascii"),
                                 hashlib.sha256).digest())
        if prefix != "mh1" or not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(_unb64(body).decode("utf-8"))
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("OAuth token недействителен", 401, "invalid_token")
    if payload.get("typ") != kind or int(payload.get("exp") or 0) <= int(time.time()):
        raise OAuthError("OAuth token истёк или имеет неверный тип", 401, "invalid_token")
    return payload


def _token_pair(client_id, resource, scope):
    return {"access_token": _issue_token("access", client_id, resource, scope, ACCESS_TTL),
            "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": _issue_token("refresh", client_id, resource, scope, REFRESH_TTL),
            "scope": " ".join(_scope_list(scope)), "resource": resource}


def token_request(params):
    client_id = _param(_one(params, "client_id"), "client_id", 256)
    if not _client(client_id):
        raise OAuthError("OAuth-клиент не зарегистрирован", 401, "invalid_client")
    grant = _one(params, "grant_type")
    if grant == "authorization_code":
        code = _param(_one(params, "code"), "code", 256)
        with _oauth_lock:
            record = _codes.pop(code, None)
        if not record or float(record.get("exp") or 0) <= time.time() \
                or record.get("client_id") != client_id:
            raise OAuthError("Authorization code недействителен", 400, "invalid_grant")
        if _one(params, "redirect_uri") != record.get("redirect_uri"):
            raise OAuthError("redirect_uri не совпадает", 400, "invalid_grant")
        verifier = _param(_one(params, "code_verifier"), "code_verifier", 128)
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        if not 43 <= len(verifier) <= 128 or any(char not in allowed for char in verifier):
            raise OAuthError("Некорректный PKCE code_verifier", 400, "invalid_grant")
        expected = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(expected, str(record.get("code_challenge") or "")):
            raise OAuthError("PKCE code_verifier не подошёл", 400, "invalid_grant")
        resource = _one(params, "resource")
        if resource and resource != record.get("resource"):
            raise OAuthError("resource не совпадает", 400, "invalid_target")
        return _token_pair(client_id, record["resource"], record.get("scope"))
    if grant == "refresh_token":
        payload = _decode_token(_one(params, "refresh_token"), "refresh")
        if payload.get("client_id") != client_id:
            raise OAuthError("Refresh token выдан другому клиенту", 400, "invalid_grant")
        resource = _one(params, "resource")
        if resource and resource != payload.get("resource"):
            raise OAuthError("resource не совпадает", 400, "invalid_target")
        requested = _scope_list(_one(params, "scope")) or payload.get("scope") or []
        if not set(requested).issubset(set(payload.get("scope") or [])):
            raise OAuthError("Нельзя расширить scope", 400, "invalid_scope")
        return _token_pair(client_id, payload["resource"], requested)
    raise OAuthError("Неподдерживаемый grant_type", 400, "unsupported_grant_type")


def validate_builtin(settings, authorization, resource=None):
    token = bearer_from_header(authorization)
    if not token:
        return False, 401, "Нужен Bearer access token"
    try:
        payload = _decode_token(token, "access")
    except OAuthError as exc:
        return False, exc.status, str(exc)
    if resource and str(payload.get("resource") or "").rstrip("/") != str(resource).rstrip("/"):
        return False, 401, "OAuth token выдан для другого MCP resource"
    missing = set(_required(settings)) - set(payload.get("scope") or [])
    if missing:
        return False, 403, "OAuth token не содержит scopes: %s" % ", ".join(sorted(missing))
    return True, 200, "Встроенный OAuth token активен"
