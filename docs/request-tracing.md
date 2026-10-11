# Request tracing

The task panel displays eight measured stages: routing, request body, RPC decoding, upstream connection, request forwarding, waiting for response headers, receiving the response, and sending the response to the client.

## Measurement boundary

A trace begins when the HTTP inspector starts handling a request and ends when it writes and flushes the response. These are monotonic `perf_counter` measurements, not separate measurements of Notion model/UI processing, client network delivery, or operations inside an MCP tool. The receive phase can include waiting for an MCP tool to finish. The UI explicitly describes this scope.

Completed requests can be replayed with an animated signal, scrubber, playback speed, zoom, and pan. Short requests are slowed down for visibility; displayed timings remain the measured timings. Live values are provisional until completion. Reduced-motion users can use the scrubber without animation.

Older journal entries remain available but do not receive invented stage data. SQLite migration adds a nullable trace column. Request and response bodies and authorization headers are not stored in the trace. The existing journal arguments preview is unchanged; journal exports can contain tool arguments and should be reviewed before sharing.

## Resource limits

Transition events are bounded to 128 per trace, retaining the first and latest transitions. Aggregate phase durations remain available. Live progress events are rate-limited, and the task list displays at most 300 entries. Completed idle diagrams do not run a continuous animation loop.

## Tests

Run `python tests/test_tracing.py` from the source checkout. The tests use loopback-only temporary HTTP servers, a temporary SQLite database seeded with the pre-tracing schema, and a separate import namespace with test configuration. They do not use production configuration, credentials, or data.

Coverage includes JSON and fragmented UTF-8 SSE forwarding, notification completion, upstream errors, live phase snapshots, bounded transition history, old journal entries, schema migration, malformed SSE data, trace payload exclusion, and an actual-shaped tools/call request.

## Upgrade

Back up the changed source files and take a consistent SQLite backup. Restart the panel/inspector controller only; do not stop or recursively kill the MCP backend processes. Existing panel sessions are in memory and require signing in again after restart. In-flight inspector requests may be interrupted. Do not launch a second controller on the same ports. Existing backend configuration, authentication, launch scripts, and optimized gateways are not changed by this feature.
