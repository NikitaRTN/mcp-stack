# -*- coding: utf-8 -*-
"""Transparent MCP reverse proxy that records every tool call.

Traffic path:  client -> Caddi -> inspector (/_inspect/<service>) -> MCP server

The inspector speaks Streamable HTTP MCP: it parses the JSON-RPC request body,
remembers each `tools/call`, then watches the response - plain JSON or an
`text/event-stream` - and closes the matching telemetry row with ok/error and a
duration. Bytes are forwarded chunk by chunk and flushed immediately, so
streaming behaviour of the underlying MCP is preserved exactly.
"""

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from . import oauth
from . import telemetry
from .events import BUS

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host",
}
MAX_BODY = 32 * 1024 * 1024      # 32 MiB request cap, plenty for MCP payloads
STREAM_CHUNK = 8192
UPSTREAM_CONNECT_TIMEOUT = 4.0
CALL_COUNTER = {"n": 0}


class _Pending:
    """A JSON-RPC request we are waiting for an answer to."""

    __slots__ = ("row_id", "method", "tool", "rpc_id")

    def __init__(self, row_id, method, tool, rpc_id):
        self.row_id = row_id
        self.method = method
        self.tool = tool
        self.rpc_id = rpc_id


class SseScanner:
    """Extract complete SSE `data:` payloads from a byte stream."""

    def __init__(self):
        self._buffer = b""

    def feed(self, chunk):
        self._buffer += chunk
        out = []
        while True:
            for separator in (b"\n\n", b"\r\n\r\n"):
                index = self._buffer.find(separator)
                if index != -1:
                    frame = self._buffer[:index]
                    self._buffer = self._buffer[index + len(separator):]
                    payload = self._payload(frame)
                    if payload is not None:
                        out.append(payload)
                    break
            else:
                return out

    @staticmethod
    def _payload(frame):
        lines = []
        for raw in frame.split(b"\n"):
            line = raw.strip()
            if line.startswith(b"data:"):
                lines.append(line[5:].strip())
        if not lines:
            return None
        try:
            return json.loads(b"\n".join(lines).decode("utf-8", errors="replace"))
        except ValueError:
            return None


def parse_requests(body):
    """Return the JSON-RPC request objects contained in a request body."""
    if not body:
        return []
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    return [i for i in items if isinstance(i, dict) and i.get("method")]


def describe(request):
    """(method, tool, args) for telemetry."""
    method = str(request.get("method") or "?")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "tools/call":
        return method, params.get("name"), params.get("arguments")
    if method in ("resources/read", "prompts/get"):
        return method, params.get("uri") or params.get("name"), params
    return method, None, params or None


def classify(response):
    """(status, error_text) for a JSON-RPC response object."""
    error = response.get("error")
    if isinstance(error, dict):
        text = error.get("message") or "Ошибка JSON-RPC"
        code = error.get("code")
        if code is not None:
            text = "%s (code %s)" % (text, code)
        data = error.get("data")
        if data:
            text += "\n" + json.dumps(data, ensure_ascii=False)[:800]
        return "error", text
    result = response.get("result")
    if isinstance(result, dict) and result.get("isError"):
        # MCP tools report failures inside a successful JSON-RPC result.
        return "error", _content_text(result.get("content")) or "Инструмент вернул ошибку"
    return "ok", None


def _content_text(content):
    if not isinstance(content, list):
        return None
    parts = [str(i.get("text")) for i in content
             if isinstance(i, dict) and i.get("type") == "text" and i.get("text")]
    return ("\n".join(parts))[:1500] or None


class InspectorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MCPHub-Inspector"

    # -- plumbing --------------------------------------------------------- #

    def log_message(self, fmt, *args):        # keep stderr clean
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, status, message, code=-32000):
        self._json(status, {"jsonrpc": "2.0", "error": {"code": code, "message": message},
                           "id": None})

    def _oauth_error(self, status, message, service=None, cfg=None):
        body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32001,
                          "message": message}, "id": None}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        error = "insufficient_scope" if status == 403 else "invalid_token"
        self.send_header("WWW-Authenticate", oauth.challenge(service, cfg, error=error))
        self.end_headers()
        self.wfile.write(body)

    # -- entry points ----------------------------------------------------- #

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        if self.path in ("/", "/healthz"):
            return self._json(200, {"ok": True, "role": "inspector"})
        self._proxy("GET")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- the proxy itself -------------------------------------------------- #

    def _resolve(self):
        if not self.path.startswith("/_inspect/"):
            return None, None
        rest = self.path[len("/_inspect/"):]
        sid, _, tail = rest.partition("/")
        sid = sid.split("?")[0]
        return sid, ("/" + tail if tail else "")

    def _proxy(self, method):
        sid, _tail = self._resolve()
        if not sid:
            return self._rpc_error(404, "Неизвестный маршрут инспектора")

        cfg = config.load()
        svc = config.service(sid, cfg)
        if svc is None:
            return self._rpc_error(404, "Сервис %s не настроен" % sid)
        if not svc.get("enabled"):
            # Disabled on purpose: answer clearly instead of pretending to fail.
            return self._rpc_error(503, "Сервис выключен в панели MCP Hub")
        if (svc.get("authMode") or "token") == "oauth":
            allowed, status, detail = oauth.validate_incoming(
                svc.get("oauth") or {}, self.headers.get("Authorization"),
                resource=config.public_url(svc, cfg))
            if not allowed:
                return self._oauth_error(status, detail, svc, cfg)

        target = config.split_upstream(config.upstream_of(svc))
        if target is None:
            return self._rpc_error(503, "У сервиса не задан upstream")
        host, port, base_path, scheme = target

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._rpc_error(413, "Слишком большое тело запроса")
        body = self.rfile.read(length) if length else b""

        session = self.headers.get("mcp-session-id")
        client = self.headers.get("X-Forwarded-For") or self.client_address[0]
        pending = self._open_rows(svc, body, session, client)

        try:
            self._forward(method, host, port, base_path, scheme, body, pending, session, svc)
        except oauth.OAuthError as exc:
            self._close_all(pending, "error", str(exc))
            self._safe_error(502, "OAuth апстрима: %s" % exc)
        except (socket.timeout, TimeoutError):
            self._close_all(pending, "timeout", "Апстрим не ответил вовремя")
            self._upstream_problem(svc, "Апстрим не ответил вовремя")
            self._safe_error(504, "MCP не ответил вовремя")
        except (ConnectionRefusedError, OSError) as exc:
            self._close_all(pending, "error", "Нет соединения с MCP: %s" % exc)
            self._upstream_problem(svc, "Нет соединения с %s:%d" % (host, port))
            self._safe_error(502, "MCP-сервер недоступен: %s" % exc)
        except Exception as exc:                      # noqa: BLE001 - proxy must not die
            self._close_all(pending, "error", str(exc))
            self._safe_error(500, "Ошибка инспектора: %s" % exc)

    def _safe_error(self, status, message):
        try:
            self._rpc_error(status, message)
        except OSError:
            pass

    def _open_rows(self, svc, body, session, client):
        pending = {}
        for request in parse_requests(body):
            method, tool, args = describe(request)
            rpc_id = request.get("id")
            row_id = telemetry.begin(
                service=svc["id"], method=method, tool=tool, rpc_id=rpc_id,
                session=session, args=args, req_bytes=len(body), client=client)
            if rpc_id is None:
                # Notification: no answer will ever come, close it immediately.
                telemetry.finish(row_id, "ok", resp_bytes=0, session=session)
            else:
                pending[str(rpc_id)] = _Pending(row_id, method, tool, rpc_id)
            CALL_COUNTER["n"] += 1
            if CALL_COUNTER["n"] % 500 == 0:
                threading.Thread(target=telemetry.prune, daemon=True).start()
        return pending

    def _forward(self, method, host, port, base_path, scheme, body, pending, session, svc):
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=UPSTREAM_CONNECT_TIMEOUT)
        headers = {}
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP and key.lower() != "authorization":
                headers[key] = value
        headers["Host"] = "%s:%d" % (host, port)
        if svc.get("kind") == "remote":
            upstream_auth = svc.get("upstreamAuthMode") or (
                "bearer" if svc.get("upstreamToken") else "none")
            if upstream_auth == "bearer" and svc.get("upstreamToken"):
                headers["Authorization"] = "Bearer %s" % svc["upstreamToken"]
            elif upstream_auth == "oauth":
                token = oauth.client_credentials(
                    svc.get("upstreamOAuth") or {}, verify_tls=bool(
                        (svc.get("upstreamOAuth") or {}).get("verifyTls", True)))
                headers["Authorization"] = "Bearer %s" % token
        if body:
            headers["Content-Length"] = str(len(body))

        conn.request(method, base_path, body=body or None, headers=headers)
        conn.sock.settimeout(None)       # streams may idle for minutes
        response = conn.getresponse()

        resp_session = response.getheader("mcp-session-id") or session
        content_type = (response.getheader("Content-Type") or "").lower()
        streaming = "text/event-stream" in content_type
        # http.client transparently decodes an upstream chunked body.  For a
        # non-SSE response we already buffer the whole payload, so frame it for
        # the downstream connection with a fresh Content-Length.  Otherwise an
        # upstream `Transfer-Encoding: chunked` header is removed below and the
        # HTTP/1.1 response has neither Transfer-Encoding nor Content-Length;
        # Caddy then waits for EOF on this persistent connection and the MCP
        # client appears to receive an empty result.
        payload = None if streaming else response.read()

        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
        else:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()

        if response.status >= 400 and pending:
            detail = "HTTP %d от MCP-сервера" % response.status
            self._close_all(pending, "error", detail, session=resp_session)

        if streaming:
            self._pump_stream(response, pending, resp_session)
        else:
            self._pump_body(payload, pending, resp_session)
        conn.close()

    # -- response pumps ---------------------------------------------------- #

    def _pump_body(self, payload, pending, session):
        if payload:
            self.wfile.write(payload)
            self.wfile.flush()
        self._consume(payload, pending, session, len(payload))
        self._close_all(pending, "ok", None, session=session)

    def _pump_stream(self, response, pending, session):
        scanner = SseScanner()
        total = 0
        try:
            while True:
                chunk = response.read1(STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                # Manual chunked framing keeps the connection reusable while
                # still flushing every event the instant it arrives.
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                for message in scanner.feed(chunk):
                    self._match(message, pending, session, total)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._close_all(pending, "cancelled", "Клиент закрыл соединение", session=session)
            return
        if pending:
            detail = ("Апстрим закрыл пустой SSE без JSON-RPC ответа"
                      if total == 0 else
                      "Апстрим закрыл SSE до ответа на JSON-RPC запрос")
            self._close_all(pending, "error", detail, session=session,
                            resp_bytes=total)

    def _consume(self, payload, pending, session, size):
        if not payload:
            return
        try:
            parsed = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            return
        for message in (parsed if isinstance(parsed, list) else [parsed]):
            self._match(message, pending, session, size)

    def _match(self, message, pending, session, size):
        if not isinstance(message, dict) or "id" not in message:
            return
        entry = pending.pop(str(message.get("id")), None)
        if entry is None:
            return
        status, error = classify(message)
        telemetry.finish(entry.row_id, status, error=error, resp_bytes=size, session=session)

    def _close_all(self, pending, status, error, session=None, resp_bytes=0):
        while pending:
            _key, entry = pending.popitem()
            telemetry.finish(entry.row_id, status, error=error,
                             resp_bytes=resp_bytes, session=session)

    def _upstream_problem(self, svc, detail):
        BUS.publish("service.unreachable", {
            "service": svc["id"], "label": svc.get("label"), "detail": detail,
            "at": time.time(),
        })


class InspectorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """MCP clients drop streams all the time; that is not an error."""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def serve(port, host="127.0.0.1"):
    """Start the inspector in a background thread and return (server, thread)."""
    server = InspectorServer((host, int(port)), InspectorHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4},
                             name="inspector", daemon=True)
    thread.start()
    return server, thread
