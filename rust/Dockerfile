FROM rust:1.92.0-bookworm AS build
WORKDIR /src
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim
COPY --from=build /src/target/release/mcp-hub-rs /usr/local/bin/mcp-hub-rs
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/mcp-hub-rs"]
