# -*- coding: utf-8 -*-
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hub import api, caddyfile, config, firewall, inspector, installer, mcp_client, oauth, supervisor
from hub.events import Bus, encode


class ConfigTests(unittest.TestCase):
    def sample(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["token"] = "secret"
        return cfg

    def test_every_service_is_off_by_default(self):
        self.assertTrue(config.DEFAULT_SERVICES)
        self.assertTrue(all(not s["enabled"] for s in config.DEFAULT_SERVICES))

    def test_windows_automation_is_builtin_and_expands_command(self):
        service = next(s for s in config.DEFAULT_SERVICES if s["id"] == "windows")
        self.assertIn("windows", service["requires"])
        command = config.expand_command(service)
        self.assertIn("windows_mcp.cmd", command)
        self.assertNotIn("{windowsMcp}", command)

    def test_disabled_services_have_no_caddy_route(self):
        text = caddyfile.render(self.sample())
        self.assertNotIn("handle /mcp*", text)
        self.assertNotIn("handle /roblox*", text)
        self.assertNotIn("handle /real*", text)
        self.assertIn("handle /admin*", text)
        self.assertIn("handle /healthz", text)

    def test_only_enabled_service_is_exposed(self):
        cfg = self.sample()
        cfg["services"][0]["enabled"] = True
        text = caddyfile.render(cfg)
        self.assertIn("handle /mcp*", text)
        self.assertNotIn("handle /roblox*", text)
        self.assertNotIn("handle /real*", text)

    def test_local_public_url_has_scheme_and_port(self):
        cfg = self.sample()
        self.assertEqual(config.public_url(cfg["services"][0], cfg),
                         "http://localhost:8443/mcp")

    def test_wan_443_to_lan_8443_configuration(self):
        cfg = self.sample()
        cfg.update({"domain": "riseshield.ru", "httpsPort": 8443, "bind": ""})
        text = caddyfile.render(cfg)
        self.assertIn("https_port 8443", text)
        self.assertIn("riseshield.ru {", text)
        self.assertNotIn("bind 8443", text)

    def test_bind_rejects_a_port_number(self):
        with self.assertRaises(api.RpcError) as caught:
            api.settings_update({"bind": "8443"})
        self.assertIn("Bind", str(caught.exception))


class CaddyStatusTests(unittest.TestCase):
    def sample(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg.update({"domain": "riseshield.ru", "httpsPort": 443, "bind": ""})
        return cfg

    def test_foreign_listener_is_not_reported_as_caddy(self):
        raw = {"pid": None, "running": False, "listening": None,
               "startedAt": None, "command": None}
        with mock.patch.object(config, "load", return_value=self.sample()), \
                mock.patch.object(supervisor.installer, "caddy_path", return_value=Path("caddy")), \
                mock.patch.object(supervisor.processes, "status", return_value=raw), \
                mock.patch.object(supervisor.processes, "port_open", return_value=True):
            status = supervisor.caddy_status()
        self.assertFalse(status["listening"])
        self.assertTrue(status["portOpen"])
        self.assertTrue(status["portConflict"])

    def test_tracked_listener_is_reported_as_caddy(self):
        raw = {"pid": 42, "running": True, "listening": None,
               "startedAt": time.time(), "command": "caddy run"}
        with mock.patch.object(config, "load", return_value=self.sample()), \
                mock.patch.object(supervisor.installer, "caddy_path", return_value=Path("caddy")), \
                mock.patch.object(supervisor.processes, "status", return_value=raw), \
                mock.patch.object(supervisor.processes, "port_open", return_value=True):
            status = supervisor.caddy_status()
        self.assertTrue(status["listening"])
        self.assertFalse(status["portConflict"])


class InspectorTests(unittest.TestCase):
    def test_describe_tool_call(self):
        request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "read_file", "arguments": {"path": "a.txt"}}}
        method, tool, args = inspector.describe(request)
        self.assertEqual(method, "tools/call")
        self.assertEqual(tool, "read_file")
        self.assertEqual(args, {"path": "a.txt"})

    def test_classify_json_rpc_and_mcp_errors(self):
        self.assertEqual(inspector.classify({"id": 1, "result": {}}), ("ok", None))
        status, error = inspector.classify({"id": 1, "error": {"code": -1, "message": "bad"}})
        self.assertEqual(status, "error")
        self.assertIn("bad", error)
        status, error = inspector.classify({
            "id": 1, "result": {"isError": True,
                                  "content": [{"type": "text", "text": "tool failed"}]}})
        self.assertEqual(status, "error")
        self.assertIn("tool failed", error)

    def test_sse_scanner_handles_split_frames(self):
        scan = inspector.SseScanner()
        self.assertEqual(scan.feed(b"event: message\ndata: {\"id\":"), [])
        result = scan.feed(b"1,\"result\":{}}\n\n")
        self.assertEqual(result[0]["id"], 1)


class EventBusTests(unittest.TestCase):
    def test_replay_and_sse_id(self):
        bus = Bus()
        first = bus.publish("one", {"x": 1})
        second = bus.publish("two", {"x": 2})
        queue, backlog = bus.subscribe(last_seq=first["seq"])
        try:
            self.assertEqual([e["kind"] for e in backlog], ["two"])
            frame = encode(second).decode("utf-8")
            self.assertIn("id: %d" % second["seq"], frame)
            self.assertIn("event: two", frame)
        finally:
            bus.unsubscribe(queue)


class InstallerTests(unittest.TestCase):
    def test_desktop_commander_is_a_separate_component(self):
        def package_info(name):
            if name == "@wonderwhy-er/desktop-commander":
                return {"path": "/cache/desktop-commander", "version": "1.2.3"}
            return None

        with mock.patch.object(installer, "caddy_path", return_value=None), \
                mock.patch.object(installer, "_npm_package_info", side_effect=package_info), \
                mock.patch.object(installer.processes, "which", side_effect=lambda name: "/bin/" + name), \
                mock.patch.object(installer, "_version", return_value="v1"):
            items = {item["id"]: item for item in installer.detect()}

        self.assertIn("desktop-commander", items)
        self.assertTrue(items["desktop-commander"]["found"])
        self.assertEqual(items["desktop-commander"]["version"], "1.2.3")
        self.assertIn("supergateway", items["desktop-commander"]["dependsOn"])

    def test_scoped_npm_package_manifest_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "@wonderwhy-er" / "desktop-commander"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"name": "@wonderwhy-er/desktop-commander", "version": "9.8.7"}),
                encoding="utf-8")
            result = installer._read_npm_package(
                str(package), "@wonderwhy-er/desktop-commander")
        self.assertEqual(result["version"], "9.8.7")

    def test_job_snapshot_contains_live_transfer_progress(self):
        job = installer.Job("caddy")
        with mock.patch.object(installer.BUS, "publish") as publish:
            job.log("Скачано 1.0 / 4.0 МБ", 28, phase="download",
                    indeterminate=False, downloadedBytes=1048576,
                    totalBytes=4194304, speedBps=524288)
            job.pulse("Скачивание файла…", phase="download",
                      indeterminate=False)

        snapshot = job.snapshot()
        self.assertEqual(snapshot["percent"], 28)
        self.assertEqual(snapshot["downloadedBytes"], 1048576)
        self.assertEqual(snapshot["totalBytes"], 4194304)
        self.assertEqual(snapshot["speedBps"], 524288)
        self.assertEqual(snapshot["detail"], "Скачивание файла…")
        self.assertIsNone(publish.call_args.args[1]["line"])


class DeveloperWorkspaceTests(unittest.TestCase):
    def sample(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["token"] = "secret"
        return cfg

    def test_caddy_renders_per_service_auth_modes(self):
        cfg = self.sample()
        for service in cfg["services"]:
            service["enabled"] = False
        service = cfg["services"][0]
        service["enabled"] = True

        service["authMode"] = "token"
        self.assertIn("@unauthorized", caddyfile.render(cfg))
        service["authMode"] = "none"
        self.assertNotIn("@unauthorized", caddyfile.render(cfg))
        service["authMode"] = "oauth"
        self.assertIn("OAuth access token is validated by the inspector", caddyfile.render(cfg))

    def test_stdio_port_cannot_conflict_with_hub_ports(self):
        cfg = self.sample()
        payload = {"id": "conflict", "label": "Conflict", "kind": "stdio",
                   "path": "/conflict", "port": cfg["httpsPort"],
                   "command": "demo --port {port}"}
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "save"):
            with self.assertRaises(ValueError):
                config.add_service(payload)

    def test_oauth_route_requires_introspection_url(self):
        cfg = self.sample()
        payload = {"id": "secure", "label": "Secure", "kind": "remote",
                   "path": "/secure", "upstream": "http://127.0.0.1:9000/mcp",
                   "authMode": "oauth", "oauth": {"mode": "introspection"}}
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "save"):
            with self.assertRaises(ValueError):
                config.add_service(payload)

    def test_public_state_never_contains_oauth_secrets(self):
        result = supervisor._public_oauth({
            "clientId": "client", "clientSecret": "top-secret",
            "tokenUrl": "https://auth.example/token", "verifyTls": True,
        })
        self.assertNotIn("clientSecret", result)
        self.assertTrue(result["hasClientSecret"])

    def test_incoming_oauth_requires_bearer_and_scopes(self):
        settings = {"requiredScopes": "mcp:tools"}
        self.assertEqual(oauth.validate_incoming(settings, None)[:2], (False, 401))
        with mock.patch.object(oauth, "introspect", return_value={
                "active": True, "detail": "OAuth token активен"}):
            result = oauth.validate_incoming(settings, "Bearer access-token")
        self.assertEqual(result[:2], (True, 200))

    def test_client_log_accepts_duplicate_event_metadata(self):
        payload = {"entries": [{"level": "error", "message": "client.log failed",
                    "event": "rpc.failed", "fields": {"event": "nested.event",
                    "method": "client.log", "source": "duplicate",
                    "message": "duplicate", "level": "debug"}}]}
        with mock.patch.object(api.LOG, "record") as record:
            result = api.client_log(payload)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(record.call_args.kwargs["event"], "rpc.failed")
        self.assertEqual(record.call_args.kwargs["method"], "client.log")
        for reserved in ("source", "message", "level"):
            self.assertNotIn(reserved, record.call_args.kwargs)

    def test_firewall_is_explicitly_unsupported_off_windows(self):
        with mock.patch.object(firewall.processes, "IS_WINDOWS", False):
            status = firewall.status(8443)
            self.assertFalse(status["supported"])
            with self.assertRaises(firewall.FirewallError):
                firewall.authorize(8443)

    def test_settings_reports_restart_for_admin_and_inspector_ports(self):
        cfg = self.sample()
        updated = dict(cfg)
        updated.update({"httpsPort": 9443, "adminPort": 9765, "inspectorPort": 9770})
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "update", return_value=updated), \
                mock.patch.object(caddyfile, "write"), \
                mock.patch.object(supervisor, "validate_caddyfile", return_value={"ok": True}), \
                mock.patch.object(supervisor, "reload_caddy", return_value={"ok": True}), \
                mock.patch.object(api.BUS, "publish"):
            result = api.settings_update({"httpsPort": 9443, "adminPort": 9765,
                                          "inspectorPort": 9770})
        self.assertEqual(set(result["restartRequired"]), {"adminPort", "inspectorPort"})


class CertificateTests(unittest.TestCase):
    def sample(self, port=443):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg.update({"domain": "mcp.riseshield.ru", "httpsPort": port,
                    "email": "admin@riseshield.ru", "token": "secret"})
        return cfg

    def test_caddy_enables_automatic_https_on_standard_port(self):
        text = caddyfile.render(self.sample(443))
        self.assertIn("automatic Let's Encrypt certificate", text)
        self.assertIn("email admin@riseshield.ru", text)
        self.assertNotIn("auto_https off", text)
        self.assertNotIn("tls internal", text)

    def test_caddy_uses_forwarded_tls_alpn_on_custom_port(self):
        text = caddyfile.render(self.sample(8443))
        self.assertIn("disable_http_challenge", text)
        self.assertIn("alt_tlsalpn_port 8443", text)
        self.assertIn("Requires port forwarding 443 -> 8443", text)

    def test_certificate_summary_distinguishes_trusted_certificate(self):
        cert = {"issuer": ((('organizationName', "Let's Encrypt"),),),
                "subject": ((('commonName', "mcp.riseshield.ru"),),),
                "subjectAltName": (("DNS", "mcp.riseshield.ru"),),
                "notAfter": "Aug 21 12:00:00 2030 GMT"}
        trusted = supervisor._certificate_summary(cert, "mcp.riseshield.ru", True)
        untrusted = supervisor._certificate_summary(cert, "mcp.riseshield.ru", False,
                                                    verification_error="unknown CA")
        self.assertTrue(trusted["ok"])
        self.assertEqual(trusted["issuer"], "Let's Encrypt")
        self.assertGreater(trusted["daysRemaining"], 0)
        self.assertFalse(untrusted["ok"])

    def test_certificate_issue_starts_acme_and_waits_for_trust(self):
        pending = {"applicable": True, "ok": False, "detail": "pending"}
        ready = {"applicable": True, "ok": True, "trusted": True,
                 "issuer": "Let's Encrypt", "detail": "ready"}
        running = {"running": True, "listening": True}
        with mock.patch.object(config, "load", return_value=self.sample(8443)), \
                mock.patch.object(caddyfile, "write"), \
                mock.patch.object(supervisor, "validate_caddyfile", return_value={"ok": True}), \
                mock.patch.object(supervisor, "cert_info", side_effect=[pending, pending, ready]), \
                mock.patch.object(supervisor, "caddy_status", return_value=running), \
                mock.patch.object(supervisor, "reload_caddy", return_value={"ok": True}), \
                mock.patch.object(supervisor, "caddy_log", return_value=""), \
                mock.patch.object(supervisor.time, "sleep"):
            result = supervisor.ensure_certificate(wait=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cert"]["issuer"], "Let's Encrypt")


class McpClientIntegrationTests(unittest.TestCase):
    def test_lists_and_calls_only_target_server_tools(self):
        tools = [{"name": "sum", "description": "Add", "inputSchema": {
            "type": "object", "properties": {"a": {"type": "number"},
                                                "b": {"type": "number"}},
            "required": ["a", "b"]}}]

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_DELETE(self):
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = json.loads(raw or b"{}")
                method = request.get("method")
                if method == "notifications/initialized":
                    self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
                if method == "initialize":
                    result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "unit-mcp", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": tools}
                else:
                    args = (request.get("params") or {}).get("arguments") or {}
                    value = args.get("a", 0) + args.get("b", 0)
                    result = {"content": [{"type": "text", "text": str(value)}],
                              "structuredContent": {"value": value}}
                body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                                   "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if method == "initialize": self.send_header("Mcp-Session-Id", "unit-session")
                self.end_headers(); self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/mcp" % server.server_address[1]
        try:
            listed = mcp_client.list_tools(url)
            called = mcp_client.call_tool(url, "sum", {"a": 3, "b": 4})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual([item["name"] for item in listed["tools"]], ["sum"])
        self.assertEqual(called["result"]["structuredContent"]["value"], 7)


if __name__ == "__main__":
    unittest.main()
