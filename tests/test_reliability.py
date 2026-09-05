import io
import json
import unittest
from unittest import mock
from hub import supervisor, caddyfile, config, mcp_client

class ReliabilityTests(unittest.TestCase):
    def test_start_reuses_running_process(self):
        svc = {"id":"windows", "enabled":True, "kind":"stdio"}
        with mock.patch.object(config,"service",return_value=svc), mock.patch.object(config,"expand_command",return_value="same"), mock.patch.object(supervisor.processes,"status",return_value={"running":True,"pid":42,"command":"same"}), mock.patch.object(supervisor.processes,"spawn") as spawn:
            self.assertEqual(supervisor.start_service("windows"),42)
            spawn.assert_not_called()

    def test_changed_command_needs_explicit_restart(self):
        svc = {"id":"windows", "enabled":True, "kind":"stdio"}
        with mock.patch.object(config,"service",return_value=svc), mock.patch.object(config,"expand_command",return_value="new"), mock.patch.object(supervisor.processes,"status",return_value={"running":True,"pid":42,"command":"old"}), mock.patch.object(supervisor.processes,"spawn") as spawn:
            with self.assertRaises(ValueError): supervisor.start_service("windows")
            spawn.assert_not_called()

    def test_duplicate_enable_does_not_reload_routes(self):
        svc={"id":"windows","enabled":True,"kind":"stdio"}
        with mock.patch.object(config,"service",return_value=svc), mock.patch.object(supervisor,"start_service",return_value=42) as start, mock.patch.object(config,"set_service") as save, mock.patch.object(supervisor,"reload_caddy") as reload:
            supervisor.set_enabled("windows",True)
            start.assert_called_once_with("windows")
            save.assert_not_called(); reload.assert_not_called()

    def test_failed_reload_does_not_kill_proxy(self):
        with mock.patch.object(supervisor.installer,"caddy_path",return_value="caddy"), mock.patch.object(caddyfile,"write"), mock.patch.object(supervisor,"_caddy_runtime_status",return_value={"running":True}), mock.patch.object(supervisor.processes,"run",return_value=(1,"bad config")), mock.patch.object(supervisor,"restart_caddy") as restart:
            self.assertFalse(supervisor.reload_caddy()["ok"])
            restart.assert_not_called()

    def test_route_reload_preserves_streams(self):
        cfg=json.loads(json.dumps(config.DEFAULTS)); cfg["services"][0]["enabled"]=True
        self.assertIn("stream_close_delay 5m",caddyfile.render(cfg))

    def test_sse_accepts_large_image_line_within_limit(self):
        client=mcp_client.StreamableClient("http://127.0.0.1:1/mcp")
        msg={"jsonrpc":"2.0","id":1,"result":{"data":"a"*100000}}
        stream=io.BytesIO(b"data: "+json.dumps(msg).encode()+b"\n\n")
        self.assertEqual(client._read_sse(stream,1),msg)
