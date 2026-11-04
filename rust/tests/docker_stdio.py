"""Linux-container stdio integration fixture. Python is only the host-side test driver."""
import json
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TOKEN = "ci-stdio-token-0123456789"
PORT = 18767
URL = f"http://127.0.0.1:{PORT}/mcp"
SCRIPT = r'''#!/bin/sh
while IFS= read -r line; do
  case "$line" in
    *'"initialize"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"container-fixture","version":"1"}}}' ;;
    *'"notifications/initialized"'*) : ;;
    *'"tools/list"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo","inputSchema":{"type":"object"}}]}}' ;;
    *'"tools/call"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":"server-root","method":"roots/list"}'
      IFS= read -r reply || exit 1
      case "$reply" in
        *'"server-root"'*) printf '%s\n' '{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"docker-stdio-ok"}]}}' ;;
        *) exit 1 ;;
      esac ;;
    *) exit 1 ;;
  esac
done
'''

def request(method, body=None, session=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if session:
        headers["Mcp-Session-Id"] = session
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    return urllib.request.urlopen(urllib.request.Request(URL, data=data, headers=headers, method=method), timeout=10)

def events(response):
    return [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]

def main():
    name = "mcp-stdio-ci-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="mcp-stdio-") as folder:
        root = Path(folder)
        root.chmod(0o755)  # The container deliberately runs as an unprivileged UID.
        (root / "server.sh").write_text(SCRIPT, encoding="utf-8")
        (root / "config.json").write_text(json.dumps({"services": [{"id": "default", "program": "/bin/sh", "args": ["/fixtures/server.sh"], "log": "/tmp/stdio-fixture.log"}]}), encoding="utf-8")
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--network", "host", "-v", f"{root}:/fixtures:ro", "-e", f"MCP_HUB_PORT={PORT}", "-e", f"MCP_HUB_TOKEN={TOKEN}", "-e", "MCP_HUB_STDIO_CONFIG=/fixtures/config.json", "mcp-hub-rs-experimental", "serve"], check=True, stdout=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("container listener did not start")
                    time.sleep(0.1)
            with request("POST", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "ci", "version": "1"}}}) as response:
                session = response.headers["Mcp-Session-Id"]
                assert len(session) == 64
                assert events(response)[0]["result"]["serverInfo"]["name"] == "container-fixture"
            with request("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, session) as response:
                assert response.status == 202
            with request("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session) as response:
                assert events(response)[0]["result"]["tools"][0]["name"] == "echo"
            with request("POST", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo"}}, session) as response:
                line = response.readline().decode()
                assert json.loads(line.removeprefix("data: "))["method"] == "roots/list"
                with request("POST", {"jsonrpc": "2.0", "id": "server-root", "result": {"roots": []}}, session) as reply:
                    assert reply.status == 202
                assert events(response)[0]["result"]["content"][0]["text"] == "docker-stdio-ok"
            with request("DELETE", session=session) as response:
                assert response.status == 200
            try:
                request("DELETE", session=session)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("deleted session still accessible")
            print("container native stdio and bidirectional server request: ok")
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)

if __name__ == "__main__":
    main()
