# -*- coding: utf-8 -*-
"""Small Streamable HTTP MCP client for the built-in developer workspace."""

import json
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import oauth


class McpClientError(ValueError):
    pass


MAX_RESPONSE = 4_000_000
PROTOCOL_VERSION = "2025-03-26"


def normalize_url(value):
    raw = str(value or "").strip()
    if not raw:
        raise McpClientError("Укажите URL MCP-сервера")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise McpClientError("MCP URL должен начинаться с http:// или https://")
    if parts.username or parts.password:
        raise McpClientError("Логин и пароль нельзя помещать в MCP URL")
    return raw


def _context(verify_tls=True):
    return ssl.create_default_context() if verify_tls else ssl._create_unverified_context()


def _auth_header(auth, timeout, verify_tls):
    auth = auth or {}
    mode = str(auth.get("mode") or "none")
    if mode in ("none", ""):
        return None, "none"
    if mode in ("bearer", "hub_token"):
        token = str(auth.get("token") or "").strip()
        if not token:
            raise McpClientError("Для Bearer-авторизации нужен токен")
        return "Bearer " + token, "bearer"
    if mode in ("oauth", "oauth_client_credentials"):
        try:
            token = oauth.client_credentials(auth, timeout=timeout, verify_tls=verify_tls)
        except oauth.OAuthError as exc:
            raise McpClientError(str(exc))
        return "Bearer " + token, "oauth_client_credentials"
    raise McpClientError("Неизвестный режим авторизации %s" % mode)


class StreamableClient:
    def __init__(self, url, auth=None, timeout=20, verify_tls=True):
        self.url = normalize_url(url)
        self.timeout = max(3, min(120, int(timeout or 20)))
        self.verify_tls = bool(verify_tls)
        self.authorization, self.auth_mode = _auth_header(
            auth or {}, self.timeout, self.verify_tls)
        self.session_id = None
        self.next_id = 1
        self.server_info = None
        self.protocol_version = None

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "MCP-Hub-Developer/1",
            "MCP-Protocol-Version": self.protocol_version or PROTOCOL_VERSION,
        }
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _request(self, payload, expected_id=None, method="POST"):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(
                    request, timeout=self.timeout, context=_context(self.verify_tls)) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                if response.status in (202, 204):
                    return {}
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in content_type:
                    return self._read_sse(response, expected_id)
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise McpClientError("MCP вернул ответ больше 4 МБ")
        except urllib.error.HTTPError as exc:
            raw = exc.read(12000) if exc.fp else b""
            detail = raw.decode("utf-8", "replace").strip()[:1800]
            if exc.code == 401:
                raise McpClientError("MCP отклонил авторизацию (HTTP 401). %s" % detail)
            raise McpClientError("MCP ответил HTTP %d: %s" %
                                 (exc.code, detail or exc.reason))
        except (urllib.error.URLError, OSError) as exc:
            raise McpClientError("Не удалось подключиться к MCP: %s" %
                                 getattr(exc, "reason", exc))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise McpClientError("MCP вернул не JSON и не SSE")

    def _read_sse(self, response, expected_id):
        data_lines = []
        total = 0
        while True:
            line = response.readline(MAX_RESPONSE + 1)
            if not line:
                break
            total += len(line)
            if total > MAX_RESPONSE:
                raise McpClientError("MCP SSE-ответ больше 4 МБ")
            if len(line) > MAX_RESPONSE:
                raise McpClientError("Слишком длинная строка в MCP SSE")
            stripped = line.rstrip(b"\r\n")
            if stripped.startswith(b"data:"):
                data_lines.append(stripped[5:].lstrip())
                continue
            if stripped or not data_lines:
                continue
            raw = b"\n".join(data_lines)
            data_lines = []
            try:
                message = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            if expected_id is None or str(message.get("id")) == str(expected_id):
                return message
        raise McpClientError("MCP закрыл SSE до ответа JSON-RPC")

    def rpc(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        response = self._request({
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": params or {},
        }, expected_id=request_id)
        if isinstance(response, dict) and response.get("error"):
            error = response["error"]
            raise McpClientError("MCP JSON-RPC: %s" %
                                 (error.get("message") if isinstance(error, dict) else error))
        if not isinstance(response, dict) or "result" not in response:
            raise McpClientError("MCP не вернул result для %s" % method)
        return response["result"]

    def notify(self, method, params=None):
        return self._request({"jsonrpc": "2.0", "method": method,
                              "params": params or {}}, expected_id=None)

    def initialize(self):
        result = self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-hub-developer", "version": "3.3"},
        })
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self):
        self.initialize()
        tools = []
        cursor = None
        for _page in range(20):
            params = {"cursor": cursor} if cursor else {}
            result = self.rpc("tools/list", params)
            batch = result.get("tools") or []
            if not isinstance(batch, list):
                raise McpClientError("tools/list вернул некорректный список")
            tools.extend(item for item in batch if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            raise McpClientError("tools/list превысил 20 страниц")
        return tools

    def call_tool(self, name, arguments=None):
        self.initialize()
        return self.rpc("tools/call", {"name": str(name),
                                       "arguments": arguments or {}})

    def close(self):
        if not self.session_id:
            return
        try:
            self._request(None, expected_id=None, method="DELETE")
        except Exception:  # noqa: BLE001
            pass


def list_tools(url, auth=None, timeout=20, verify_tls=True):
    started = time.time()
    client = StreamableClient(url, auth=auth, timeout=timeout, verify_tls=verify_tls)
    try:
        tools = client.list_tools()
        return {
            "url": client.url,
            "authMode": client.auth_mode,
            "protocolVersion": client.protocol_version,
            "serverInfo": client.server_info or {},
            "tools": tools,
            "count": len(tools),
            "elapsedMs": round((time.time() - started) * 1000, 1),
        }
    finally:
        client.close()


def call_tool(url, name, arguments=None, auth=None, timeout=30, verify_tls=True):
    started = time.time()
    client = StreamableClient(url, auth=auth, timeout=timeout, verify_tls=verify_tls)
    try:
        result = client.call_tool(name, arguments or {})
        return {
            "url": client.url,
            "tool": str(name),
            "result": result,
            "elapsedMs": round((time.time() - started) * 1000, 1),
            "serverInfo": client.server_info or {},
        }
    finally:
        client.close()
