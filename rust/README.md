# Rust incremental port

This is an **experimental MCP client**, not a replacement for MCP Hub. The existing Python Hub remains the supported application. The Rust port does not yet provide the panel, service management, OAuth, telemetry, Caddy integration or ComfyUI. Do not use this build to replace an existing installation or migrate its configuration.

The current slice supports Streamable HTTP MCP initialization, session IDs, paginated `tools/list`, `tools/call`, JSON and SSE responses, and optional bearer authentication. Redirects are disabled to avoid forwarding credentials to a different server. Only pass bearer tokens to a trusted HTTPS server or a trusted local endpoint.

```sh
cargo test --locked --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path rust/Cargo.toml
./rust/target/release/mcp-hub-rs list http://127.0.0.1:8000/mcp
./rust/target/release/mcp-hub-rs call http://127.0.0.1:8000/mcp tool_name '{}'
```

On Windows use `rust\target\release\mcp-hub-rs.exe`. Set `MCP_HUB_TOKEN` in the environment for bearer authentication; never put a token in the URL or command line. Tool arguments and responses may contain sensitive data.

For a Linux container, run `docker build -f rust/Dockerfile -t mcp-hub-rs-experimental .` from the repository root. For example: `docker run --rm mcp-hub-rs-experimental --version`. The Docker build currently packages only the experimental client. A container's `127.0.0.1` is the container itself, not the host.

Windows UI Automation and ComfyUI are out of scope for the Rust port; they remain unchanged in the legacy application. All other existing features require subsequent implementation and parity/security testing before cutover. The bounded event bus, password and session primitives, Caddy configuration generation, inspector protocol parsing, and non-destructive configuration import are tested internal components and are not yet connected to the panel. Import only produces an in-memory candidate; it never modifies the old installation.
