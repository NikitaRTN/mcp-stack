# -*- coding: utf-8 -*-
"""Panel HTTP server: static UI, one RPC endpoint, one SSE stream.

Binds to 127.0.0.1 only. Remote access goes through Caddy (/admin*), which is
why the session cookie is the single gate in front of every mutating call.
"""

import json
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from . import api
from . import config
from . import logbook
from . import oauth
from . import supervisor
from .events import BUS, encode

LOG = logbook.LOG

SESSION_COOKIE = "mcphub_session"
SESSION_TTL = 12 * 3600
MAX_FAILED = 6
LOCKOUT = 900
HEARTBEAT = 15.0          # keeps proxies from closing an idle SSE stream

_sessions = {}
_failures = {"count": 0, "until": 0.0}
_sessions_lock = threading.Lock()

PUBLIC_PATHS = {"/api/session", "/api/auth/login", "/api/auth/setup", "/healthz"}


def new_session():
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        for key, expiry in list(_sessions.items()):
            if expiry < now:
                _sessions.pop(key, None)
        _sessions[token] = now + SESSION_TTL
    return token


def session_valid(token):
    if not token:
        return False
    with _sessions_lock:
        expiry = _sessions.get(token)
        if not expiry:
            return False
        if expiry < time.time():
            _sessions.pop(token, None)
            return False
        _sessions[token] = time.time() + SESSION_TTL      # sliding window
        return True


def drop_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MCPHub-Panel"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------- #

    @property
    def route(self):
        path = urlsplit(self.path).path
        if path.startswith("/admin"):
            path = path[len("/admin"):] or "/"
        return path if path.startswith("/") else "/" + path

    def cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_html(self, status, content):
        body = str(content or "").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # form-action also applies to redirects after a form submission. OAuth
        # consent posts to this server and then redirects to the registered
        # client's callback, so HTTPS callbacks (and local native-app callbacks)
        # must be allowed here. The form action itself remains explicitly local.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location):
        # OAuth consent is submitted with POST. 303 explicitly tells browsers
        # and embedded OAuth webviews to open the callback with GET; with 302
        # some clients can keep the consent window on the current page instead
        # of completing the callback and closing the popup.
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._body_consumed = True
            self.close_connection = True
            raise api.RpcError("Некорректный Content-Length")
        if length <= 0:
            self._body_consumed = True
            return {}
        if length > 2_000_000:
            # Do not keep an HTTP/1.1 connection alive with an unread body.
            self._body_consumed = True
            self.close_connection = True
            raise api.RpcError("Слишком большой запрос", 413)
        try:
            raw = self.rfile.read(length)
            self._body_consumed = True
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise api.RpcError("Некорректный JSON")

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._body_consumed = True
            self.close_connection = True
            raise oauth.OAuthError("Некорректный Content-Length")
        if length <= 0:
            self._body_consumed = True
            return {}
        if length > 100_000:
            self._body_consumed = True
            self.close_connection = True
            raise oauth.OAuthError("Слишком большой OAuth-запрос", 413, "invalid_request")
        raw = self.rfile.read(length)
        self._body_consumed = True
        try:
            return parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
        except (UnicodeError, ValueError):
            raise oauth.OAuthError("Некорректная OAuth-форма")

    def discard_body(self):
        """Consume an unread POST body before reusing the HTTP/1.1 socket.

        Some handlers reject a request before calling ``read_json`` (for
        example, an expired session returns 401).  Leaving that body in rfile
        makes BaseHTTPRequestHandler parse the JSON followed by the next GET as
        one request line, producing ``Bad request syntax ('{...}GET / ...')``.
        """
        if getattr(self, "_body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 2_000_000:
                self.close_connection = True
            elif length:
                self.rfile.read(length)
        except (OSError, TypeError, ValueError):
            self.close_connection = True
        finally:
            self._body_consumed = True

    def authorized(self):
        return session_valid(self.cookie(SESSION_COOKIE))

    # -- GET -------------------------------------------------------------- #

    def _guard(self, verb, handler):
        """Ни один запрос панели не должен падать молча.

        Раньше исключение внутри обработчика превращалось в обрыв соединения:
        браузер видел «Failed to fetch», а причина оставалась только в консоли.
        Теперь ошибка попадает в журнал с трассировкой, а клиент получает
        честный JSON с кодом errorId.
        """
        started = time.time()
        route = self.route
        try:
            handler()
        except oauth.OAuthError as exc:
            LOG.warn("oauth", "%s %s: %s" % (verb, route, exc), event="oauth.rejected",
                     status=exc.status, oauthError=exc.oauth_error)
            try:
                self.send_json(exc.status, {"error": exc.oauth_error,
                                            "error_description": str(exc)})
            except Exception:                                # noqa: BLE001
                pass
        except api.RpcError as exc:
            LOG.warn("http", "%s %s: %s" % (verb, route, exc), event="http.rejected",
                     status=exc.status, ms=round((time.time() - started) * 1000, 1))
            try:
                self.send_json(exc.status, {"error": str(exc)})
            except Exception:                                # noqa: BLE001
                pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            LOG.debug("http", "%s %s: клиент закрыл соединение" % (verb, route),
                      event="http.closed")
        except Exception as exc:                             # noqa: BLE001
            entry = LOG.exception("http", "%s %s упал" % (verb, route), exc,
                                  event="http.failed",
                                  ms=round((time.time() - started) * 1000, 1))
            try:
                self.send_json(500, {
                    "error": "Внутренняя ошибка панели (%s). Подробности во вкладке «Логи»"
                             % ((entry or {}).get("errorId") or "без кода"),
                    "errorId": (entry or {}).get("errorId"),
                })
            except Exception:                                # noqa: BLE001
                pass

    def do_GET(self):
        self._guard("GET", self._get)

    def do_HEAD(self):
        if self.route.startswith("/.well-known/"):
            return self._guard("HEAD", self._get)
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get(self):
        route = self.route
        if route == "/healthz":
            return self.send_json(200, {"ok": True})
        if route in ("/.well-known/oauth-authorization-server",
                     "/.well-known/openid-configuration"):
            return self.send_json(200, oauth.authorization_server_metadata())
        if route.startswith("/.well-known/oauth-protected-resource"):
            return self.send_json(200, oauth.protected_resource_metadata(self.path))
        if route == "/oauth/authorize":
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                             max_num_fields=32)
            details = oauth.authorization_request(query)
            return self.send_html(200, oauth.authorization_page(details))
        if route == "/api/session":
            return self.send_json(200, {
                "authenticated": self.authorized(),
                "needsSetup": not config.has_password(),
                "username": config.load().get("auth", {}).get("username", "admin"),
                "version": _version(),
            })
        if route == "/api/events":
            if not self.authorized():
                return self.send_json(401, {"error": "Нужен вход"})
            return self.stream_events()
        if route.startswith("/api/"):
            LOG.warn("http", "Неизвестный эндпоинт GET %s" % route, event="http.notFound")
            return self.send_json(404, {"error": "Нет такого эндпоинта"})
        return self.serve_static(route)

    def do_POST(self):
        self._body_consumed = False
        try:
            self._guard("POST", self._post)
        finally:
            self.discard_body()

    def _post(self):
        route = self.route
        try:
            if route == "/api/auth/setup":
                return self.handle_setup()
            if route == "/oauth/register":
                return self.send_json(201, oauth.register_client(self.read_json()))
            if route == "/oauth/token":
                return self.send_json(200, oauth.token_request(self.read_form()))
            if route == "/oauth/authorize":
                return self.handle_oauth_authorize()
            if route == "/api/auth/login":
                return self.handle_login()
            if route == "/api/auth/logout":
                token = self.cookie(SESSION_COOKIE)
                drop_session(token)
                return self.send_json(200, {"ok": True}, [(
                    "Set-Cookie",
                    "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % SESSION_COOKIE)])
            if route == "/api/auth/password":
                return self.handle_password()
            if route == "/api/rpc":
                return self.handle_rpc()
        except api.RpcError as exc:
            return self.send_json(exc.status, {"error": str(exc)})
        return self.send_json(404, {"error": "Нет такого эндпоинта"})

    def handle_oauth_authorize(self):
        form = self.read_form()
        details = oauth.authorization_request(form)
        if (form.get("decision") or ["allow"])[0] == "deny":
            return self.redirect(oauth.denied_redirect(details))
        password = (form.get("password") or [""])[0]
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
        client_ip = forwarded or (self.client_address[0] if self.client_address else "unknown")
        allowed, detail = oauth.verify_owner_password(password, client_ip)
        if not allowed:
            LOG.warn("oauth", "Неверный пароль OAuth", event="oauth.passwordFailed",
                     client=client_ip, clientId=details.get("client_id"))
            return self.send_html(401, oauth.authorization_page(details, detail))
        LOG.info("oauth", "Доступ OAuth разрешён", event="oauth.authorized",
                 client=client_ip, clientId=details.get("client_id"),
                 resource=details.get("resource"))
        return self.redirect(oauth.authorized_redirect(details))

    # -- auth ------------------------------------------------------------- #

    def handle_setup(self):
        if config.has_password():
            raise api.RpcError("Пароль уже задан", 409)
        payload = self.read_json()
        try:
            config.set_password(payload.get("password") or "",
                               payload.get("username") or "admin")
        except ValueError as exc:
            raise api.RpcError(str(exc))
        token = new_session()
        LOG.info("auth", "Задан пароль администратора", event="auth.setup")
        return self.send_json(200, {"ok": True}, [("Set-Cookie", _cookie(token))])

    def handle_login(self):
        now = time.time()
        if _failures["until"] > now:
            raise api.RpcError("Слишком много попыток. Подождите %d мин."
                               % max(1, int((_failures["until"] - now) / 60)), 429)
        payload = self.read_json()
        if config.verify_password(payload.get("username") or "admin", payload.get("password") or ""):
            _failures["count"] = 0
            token = new_session()
            LOG.info("auth", "Вход в панель выполнен", event="auth.login",
                     client=self.client_address[0] if self.client_address else "?")
            return self.send_json(200, {"ok": True}, [("Set-Cookie", _cookie(token))])
        _failures["count"] += 1
        LOG.warn("auth", "Неверный пароль панели", event="auth.failed",
                 attempt=_failures["count"],
                 client=self.client_address[0] if self.client_address else "?")
        if _failures["count"] >= MAX_FAILED:
            _failures["count"] = 0
            _failures["until"] = now + LOCKOUT
            LOG.error("auth", "Вход заблокирован на 15 минут после %d неудачных попыток"
                      % MAX_FAILED, event="auth.lockout")
        time.sleep(0.4)                      # slow down guessing
        raise api.RpcError("Неверный логин или пароль", 401)

    def handle_password(self):
        if not self.authorized():
            raise api.RpcError("Нужен вход", 401)
        payload = self.read_json()
        username = config.load().get("auth", {}).get("username", "admin")
        if not config.verify_password(username, payload.get("current") or ""):
            raise api.RpcError("Текущий пароль не подошёл", 401)
        try:
            config.set_password(payload.get("password") or "", payload.get("username") or username)
        except ValueError as exc:
            raise api.RpcError(str(exc))
        return self.send_json(200, {"ok": True})

    # -- rpc -------------------------------------------------------------- #

    def handle_rpc(self):
        if not self.authorized():
            raise api.RpcError("Нужен вход", 401)
        payload = self.read_json()
        batch = payload if isinstance(payload, list) else [payload]
        if len(batch) > 20:
            raise api.RpcError("Слишком большой батч")
        results = []
        for item in batch:
            method = (item or {}).get("method")
            started = time.time()
            try:
                results.append({
                    "method": method,
                    "ok": True,
                    "result": api.dispatch(method, (item or {}).get("params")),
                    "ms": round((time.time() - started) * 1000, 1),
                })
            except api.RpcError as exc:
                results.append({"method": method, "ok": False, "error": str(exc),
                                "status": exc.status,
                                "errorId": getattr(exc, "error_id", None)})
            except Exception as exc:                     # noqa: BLE001
                entry = LOG.exception("rpc", "Необработанная ошибка в %s" % method, exc,
                                      event="rpc.crashed", method=method)
                results.append({"method": method, "ok": False,
                                "error": "Внутренняя ошибка %s. Подробности во вкладке «Логи»"
                                         % ((entry or {}).get("errorId") or "без кода"),
                                "status": 500,
                                "errorId": (entry or {}).get("errorId")})
        if isinstance(payload, list):
            return self.send_json(200, {"results": results})
        single = results[0]
        return self.send_json(200 if single["ok"] else single.get("status", 400), single)

    # -- SSE -------------------------------------------------------------- #

    def stream_events(self):
        query = parse_qs(urlsplit(self.path).query)
        last_seq = int(self.headers.get("Last-Event-ID")
                       or (query.get("lastSeq", [0])[0]) or 0)
        sub, backlog = BUS.subscribe(last_seq)
        LOG.debug("sse", "Подключён поток событий", event="sse.open",
                  lastSeq=last_seq, backlog=len(backlog), listeners=BUS.listeners + 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            for event in backlog:
                self.wfile.write(encode(event))
            self.wfile.flush()
            while True:
                try:
                    event = sub.get(timeout=HEARTBEAT)
                    self.wfile.write(encode(event))
                except Exception:                       # queue.Empty -> heartbeat
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(sub)
            LOG.debug("sse", "Поток событий закрыт", event="sse.close",
                      listeners=BUS.listeners)

    # -- static ------------------------------------------------------------ #

    def serve_static(self, route):
        if route in ("/", ""):
            route = "/index.html"
        target = (config.WEB / route.lstrip("/")).resolve()
        try:
            target.relative_to(config.WEB.resolve())        # no path traversal
        except ValueError:
            return self.send_json(403, {"error": "Запрещено"})
        if not target.exists() or target.is_dir():
            target = config.WEB / "index.html"              # SPA fallback
            if not target.exists():
                return self.send_json(500, {"error": "Нет файлов интерфейса в web/"})
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.name == "index.html"
                         else "max-age=60")
        self.end_headers()
        self.wfile.write(data)


def _cookie(token):
    return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
            % (SESSION_COOKIE, token, SESSION_TTL))


def _version():
    from . import __version__
    return __version__


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A browser closing an SSE stream is normal, not a crash.

        Without this, every closed tab or reloaded page prints a full
        ConnectionAbortedError / WinError 10053 traceback to the console.
        """
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def serve(port, host="127.0.0.1", blocking=True):
    server = PanelServer((host, int(port)), PanelHandler)
    if blocking:
        server.serve_forever(poll_interval=0.4)
        return server, None
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4},
                             name="panel", daemon=True)
    thread.start()
    return server, thread
