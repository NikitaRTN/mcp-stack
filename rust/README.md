# Rust incremental port

This is an **experimental MCP client and local proxy**, not a replacement for MCP Hub. The existing Python Hub remains the supported application. The Rust port does not yet provide the panel, full service management, OAuth, production telemetry, Caddy integration or ComfyUI. Do not use this build to replace an existing installation or migrate its configuration.

The current slice supports Streamable HTTP MCP initialization, session IDs, paginated `tools/list`, `tools/call`, JSON and SSE responses, and optional bearer authentication. Redirects are disabled to avoid forwarding credentials to a different server. Only pass bearer tokens to a trusted HTTPS server or a trusted local endpoint.

```sh
cargo test --locked --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path rust/Cargo.toml
./rust/target/release/mcp-hub-rs list http://127.0.0.1:8000/mcp
./rust/target/release/mcp-hub-rs call http://127.0.0.1:8000/mcp tool_name '{}'
```

On Windows use `rust\target\release\mcp-hub-rs.exe`. Set `MCP_HUB_TOKEN` in the environment for bearer authentication; never put a token in the URL or command line. Tool arguments and responses may contain sensitive data.

For a Linux container, run `docker build -f rust/Dockerfile -t mcp-hub-rs-experimental .` from the repository root. For example: `docker run --rm mcp-hub-rs-experimental --version`. The container packages the experimental client and local proxy, not the full hub. A container's `127.0.0.1` is the container itself, not the host; the CI end-to-end test uses Linux host networking.

Windows UI Automation and ComfyUI are out of scope for the Rust port; they remain unchanged in the legacy application. All other existing features require subsequent implementation and parity/security testing before cutover. The bounded event bus, password and session primitives, Caddy configuration generation, inspector protocol parsing, monotonic request tracing, SQLite call storage, process management, and non-destructive configuration import are experimental components and are not yet connected to a panel. Import only produces an in-memory candidate; it never modifies the old installation.

The experimental process manager tracks only processes it starts in its current lifetime, using Windows job objects or Unix process groups to terminate descendants. On Windows a child can spawn descendants before it is assigned to its job; on Unix a child can leave its process group. It does not recover processes after restart or support existing shell command formats. Exit cleanup occurs when the manager is queried or dropped. Normal Ctrl-C and termination request shutdown; forced termination and crashes can leave Unix processes behind. It is not a production supervisor.

## Local proxy experiment

Set `MCP_HUB_UPSTREAM` to an MCP HTTP(S) endpoint and `MCP_HUB_TOKEN` to a strong random access token (at least 24 visible ASCII characters), then run `mcp-hub-rs serve`. It listens only on `127.0.0.1`, port 8765 by default; `MCP_HUB_PORT` can override the port. Connect to `/mcp` with the access token as a bearer token. Set `MCP_HUB_UPSTREAM_TOKEN` separately if the upstream requires one; upstream bearer credentials require HTTPS or a loopback address. Redirects are not followed.

Optional `MCP_HUB_DB` enables experimental SQLite call records at the explicitly specified path. Do not point it at an existing installation's database. Arguments are not recorded. The proxy supports bounded requests with Content-Length, JSON and streaming SSE, and has a 60-second upstream read timeout. It is HTTP-only, does not implement OAuth, the admin panel or full compatibility; do not expose it publicly or use it for production.

For isolated compatibility testing only, `MCP_HUB_CONFIG` can explicitly point to a legacy JSON configuration. It is read without modification to create multiple local routes for enabled token-auth services; Windows UI Automation and ComfyUI are excluded. Stdio upstreams must already be running: this does not launch or supervise them. OAuth and unauthenticated service modes are rejected, not silently downgraded. The existing configuration's token is used unless `MCP_HUB_TOKEN` explicitly overrides it. Do not combine this mode with `MCP_HUB_UPSTREAM` or `MCP_HUB_UPSTREAM_TOKEN`. Use a copy of the configuration for tests and never run it as a replacement for the existing hub.


### Experimental process control

For an isolated test, provide `MCP_HUB_PROCESS_CONFIG` pointing to a trusted JSON file and a separate strong `MCP_HUB_ADMIN_TOKEN` (24–1024 visible ASCII characters, different from `MCP_HUB_TOKEN`). Loading the file does not start processes. With the admin bearer token, `GET /_hub/services/{id}` reports status, `POST` starts, and `DELETE` stops an explicitly configured process. The ordinary proxy token cannot control processes. Start reports a spawned process, not readiness; the configured process must itself serve the configured HTTP upstream. Requests have no body. These endpoints are experimental and have no panel.

```json
{"services":[{"id":"default","program":"/absolute/path/to/server","args":["--port","9000"],"cwd":"/absolute/path/to/workdir","env":{},"log":"/absolute/path/to/test-server.log"}]}
```

IDs must match proxy route IDs (`default` for `MCP_HUB_UPSTREAM`); log and optional working-directory paths must be absolute and their directories must exist. Windows paths use JSON-escaped backslashes. No shell parsing or legacy command conversion is performed. Commands and environment variables are trusted executable configuration; protect the file, admin token, and logs. Child processes inherit the host environment except the three Hub bearer-token variables, unless explicitly re-added in the process config. Logs may contain secrets. Do not use real production services for experiments. Readiness checks, stdio bridging, automatic restart, background exit monitoring, and full legacy service management are not implemented.
