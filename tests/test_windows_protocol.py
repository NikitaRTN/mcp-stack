"""Windows stdio protocol regression: error codes must be JSON numbers."""
import json
import os
from pathlib import Path
import subprocess
import unittest

@unittest.skipUnless(os.name == "nt", "Windows PowerShell required")
class WindowsProtocolTests(unittest.TestCase):
    def test_discovery_errors_are_numeric_and_server_survives(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "windows_mcp.ps1"
        methods = ["initialize", "tools/list", "resources/list",
                   "resources/templates/list", "prompts/list", "unknown/method", "ping"]
        requests = [{"jsonrpc": "2.0", "id": i, "method": method, "params": {}}
                    for i, method in enumerate(methods)]
        process = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script)],
            input=("\n".join(map(json.dumps, requests)) + "\n").encode("utf-8"),
            capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        replies = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
        self.assertEqual([r["id"] for r in replies], list(range(len(methods))))
        self.assertEqual(len(replies[1]["result"]["tools"]), 10)
        for reply in replies[2:6]:
            self.assertIs(type(reply["error"]["code"]), int)
            self.assertEqual(reply["error"]["code"], -32601)
        self.assertEqual(replies[-1]["result"], {})
