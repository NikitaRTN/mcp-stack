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

The experimental process manager tracks only processes it starts in its current lifetime, using Windows job objects or Unix process groups to terminate descendants. On Windows a child can spawn descendants before it is assigned to its job; on Unix a child can leave its process group. It does not recover processes after restart or support existing shell command formats. The experimental proxy polls managed processes for exit and cleans them up; direct library use must call the monitor or query the manager. Normal Ctrl-C and termination request shutdown; forced termination and crashes can leave Unix processes behind. It is not a production supervisor.

## Local proxy experiment

Set `MCP_HUB_UPSTREAM` to an MCP HTTP(S) endpoint and `MCP_HUB_TOKEN` to a strong random access token (at least 24 visible ASCII characters), then run `mcp-hub-rs serve`. It listens only on `127.0.0.1`, port 8765 by default; `MCP_HUB_PORT` can override the port. Connect to `/mcp` with the access token as a bearer token. Set `MCP_HUB_UPSTREAM_TOKEN` separately if the upstream requires one; upstream bearer credentials require HTTPS or a loopback address. Redirects are not followed.

Optional `MCP_HUB_DB` enables experimental SQLite call records at the explicitly specified path. Do not point it at an existing installation's database. Arguments are not recorded. The proxy supports bounded requests with Content-Length, JSON and streaming SSE, and has a 60-second upstream read timeout. It is HTTP-only and does not implement inbound OAuth, the admin panel or full compatibility; do not expose it publicly or use it for production.

For isolated compatibility testing only, `MCP_HUB_CONFIG` can explicitly point to a legacy JSON configuration. It is read without modification to create multiple local routes for enabled token-auth services; Windows UI Automation and ComfyUI are excluded. Stdio upstreams must already be running: this does not launch or supervise them. OAuth and unauthenticated service modes are rejected, not silently downgraded. The existing configuration's token is used unless `MCP_HUB_TOKEN` explicitly overrides it. Do not combine this mode with `MCP_HUB_UPSTREAM` or `MCP_HUB_UPSTREAM_TOKEN`. Use a copy of the configuration for tests and never run it as a replacement for the existing hub.


### Experimental process control

For an isolated test, provide `MCP_HUB_PROCESS_CONFIG` pointing to a trusted JSON file and a separate strong `MCP_HUB_ADMIN_TOKEN` (24–1024 visible ASCII characters, different from `MCP_HUB_TOKEN`). Loading the file does not start processes. With the admin bearer token, `GET /_hub/services/{id}` reports status, `POST` starts, and `DELETE` stops an explicitly configured process. The ordinary proxy token cannot control processes. Start reports a spawned process, not readiness; the configured process must itself serve the configured HTTP upstream. Requests have no body. These endpoints are experimental and have no panel.

```json
{"services":[{"id":"default","program":"/absolute/path/to/server","args":["--port","9000"],"cwd":"/absolute/path/to/workdir","env":{},"log":"/absolute/path/to/test-server.log"}]}
```

IDs must match proxy route IDs (`default` for `MCP_HUB_UPSTREAM`); log and optional working-directory paths must be absolute and their directories must exist. Windows paths use JSON-escaped backslashes. No shell parsing or legacy command conversion is performed. Commands and environment variables are trusted executable configuration; protect the file, admin token, and logs. Child processes inherit the host environment except the Hub bearer-token variables and `MCP_HUB_OAUTH_CLIENT_SECRET`, unless explicitly re-added in the process config. Logs may contain secrets. Do not use real production services for experiments. Readiness checks, automatic restart, and full legacy service management are not implemented. Native stdio is a separate experiment described below, not a conversion of managed HTTP processes.

### Experimental upstream OAuth client credentials

For a trusted OAuth-enabled upstream, set `MCP_HUB_OAUTH_TOKEN_URL`, `MCP_HUB_OAUTH_CLIENT_ID`, and `MCP_HUB_OAUTH_CLIENT_SECRET`. Optional `MCP_HUB_OAUTH_SCOPE` and `MCP_HUB_OAUTH_AUDIENCE` are sent in the token request; `MCP_HUB_OAUTH_AUTH_METHOD` is `basic` (default) or `body`. The target route ID is `default` unless `MCP_HUB_OAUTH_ROUTE` names another already-configured route. The OAuth token endpoint and the upstream must use HTTPS or loopback HTTP. OAuth and a static upstream token cannot be combined on the same route. Redirects are disabled and credentials are not logged; protect the environment from other local processes. Access tokens are cached only when `expires_in` is provided, with a safety margin. This is outbound client-credentials only, not an OAuth authorization server, discovery, DCR, PKCE or inbound token introspection. Legacy OAuth configuration is not migrated or enabled by this option.

### Experimental native stdio routes

Set `MCP_HUB_STDIO_CONFIG` to a trusted JSON file using the same explicit `services` / `id` / `program` / `args` / `cwd` / `env` / `log` format above, set `MCP_HUB_TOKEN`, and run `mcp-hub-rs serve`. No HTTP upstream is needed. A service named `default` is exposed at `/mcp`; other IDs use `/mcp/{id}`. This mode cannot be combined with legacy config, HTTP upstream overrides, or managed-process config. Configuration loading never starts a process: each authenticated MCP initialize request starts a separate owned child, with a random session ID. The shell is not invoked and legacy shell commands are not converted.

The adapter supports newline-delimited JSON-RPC stdio, POST JSON/SSE, bounded batches (32 messages), server requests and notifications within the active POST stream, concurrent POST replies/notifications, and DELETE session termination. Each session permits one active request POST (which can be a batch); a second request POST receives 409. GET returns 405; there is no independent idle notification stream, replay or reconnection/resumption. Clients must send `Content-Type: application/json` and accept both `application/json` and `text/event-stream`. The supported protocol version is 2025-03-26. Idle server messages are bounded and can only be delivered on the next request stream; this is not full compatibility with every MCP server/client.

Limits: 16 sessions per route, 15-minute idle expiry, 4 MB JSON messages, 32 queued incoming/outgoing messages, 5-second stdio write acknowledgement and 60-second total response window. Queue overflow, malformed output, failed writes, interrupted streams and request timeouts fail closed when observed; DELETE and normal proxy shutdown terminate the session's process tree. Stderr appends to the configured log; do not put logs in a public directory. Existing process-group/job limitations still apply, and forced termination on Unix can leave children behind. The HTTP worker pool and socket I/O are not hardened for untrusted slow peers. Do not expose this experiment publicly or replace the supported Python Hub.
