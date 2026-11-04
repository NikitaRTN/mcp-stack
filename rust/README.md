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

Windows UI Automation and ComfyUI are out of scope for the Rust port; they remain unchanged in the legacy application. All other existing features require subsequent implementation and parity/security testing before cutover. The bounded event bus, password and session primitives, Caddy configuration generation, inspector protocol parsing, monotonic request tracing, SQLite call storage, direct-child process management, and non-destructive configuration import are tested internal components and are not yet connected to the panel. Import only produces an in-memory candidate; it never modifies the old installation.

The experimental process manager only supervises direct children started in its current lifetime; it does not stop child process trees, recover processes after restart, or support existing shell command formats. It is not connected to service management and is not a production supervisor.

## Local proxy experiment

Set `MCP_HUB_UPSTREAM` to an MCP HTTP(S) endpoint and `MCP_HUB_TOKEN` to a strong random access token (at least 24 visible ASCII characters), then run `mcp-hub-rs serve`. It listens only on `127.0.0.1`, port 8765 by default; `MCP_HUB_PORT` can override the port. Connect to `/mcp` with the access token as a bearer token. Set `MCP_HUB_UPSTREAM_TOKEN` separately if the upstream requires one; upstream bearer credentials require HTTPS or a loopback address. Redirects are not followed.

Optional `MCP_HUB_DB` enables experimental SQLite call records at the explicitly specified path. Do not point it at an existing installation's database. Arguments are not recorded. The proxy supports bounded requests with Content-Length, JSON and streaming SSE, and has a 60-second upstream read timeout. It is HTTP-only, does not implement OAuth, service management, the admin panel or full compatibility; do not expose it publicly or use it for production.

For isolated compatibility testing only, `MCP_HUB_CONFIG` can explicitly point to a legacy JSON configuration. It is read without modification to create multiple local routes for enabled token-auth services; Windows UI Automation and ComfyUI are excluded. Stdio upstreams must already be running: this does not launch or supervise them. OAuth and unauthenticated service modes are rejected, not silently downgraded. The existing configuration's token is used unless `MCP_HUB_TOKEN` explicitly overrides it. Do not combine this mode with `MCP_HUB_UPSTREAM` or `MCP_HUB_UPSTREAM_TOKEN`. Use a copy of the configuration for tests and never run it as a replacement for the existing hub.
