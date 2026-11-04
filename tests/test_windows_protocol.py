"""Windows stdio protocol regression: error codes must be JSON numbers."""
import json
import os
from pathlib import Path
import subprocess
import unittest

@unittest.skipUnless(os.name == "nt", "Windows desktop required")
class WindowsProtocolTests(unittest.TestCase):
    def test_discovery_errors_are_numeric_and_server_survives(self):
        executable = Path(__file__).resolve().parents[1] / "tools" / "windows_mcp.exe"
        methods = ["initialize", "tools/list", "resources/list",
                   "resources/templates/list", "prompts/list", "unknown/method", "ping"]
        requests = [{"jsonrpc": "2.0", "id": i, "method": method, "params": {}}
                    for i, method in enumerate(methods)]
        process = subprocess.run(
            [str(executable)],
            input=("\n".join(map(json.dumps, requests)) + "\n").encode("utf-8"),
            capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        replies = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
        self.assertEqual([r["id"] for r in replies], list(range(len(methods))))
        tools = replies[1]["result"]["tools"]
        self.assertEqual(len(tools), 16)
        self.assertIn("type_text", [tool["name"] for tool in tools])
        self.assertIn("activate_window", [tool["name"] for tool in tools])
        set_value = next(tool for tool in tools if tool["name"] == "set_control_value")
        self.assertIn("submit", set_value["inputSchema"]["properties"])
        self.assertIn("processName", set_value["inputSchema"]["properties"])
        for reply in replies[2:6]:
            self.assertIs(type(reply["error"]["code"]), int)
            self.assertEqual(reply["error"]["code"], -32601)
        self.assertEqual(replies[-1]["result"], {})
