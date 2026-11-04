use mcp_hub_rs::{process::ProcessSpec, stdio::StdioTransport};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, BufRead, Write},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
fn send(value: Value) {
    let mut out = io::stdout().lock();
    writeln!(out, "{value}").unwrap();
    out.flush().unwrap();
}
fn fixture() {
    if let Some(path) = std::env::var_os("MCP_TEST_PID_FILE") {
        std::fs::write(path, std::process::id().to_string()).unwrap();
    }
    for name in [
        "MCP_HUB_TOKEN",
        "MCP_HUB_ADMIN_TOKEN",
        "MCP_HUB_UPSTREAM_TOKEN",
        "MCP_HUB_OAUTH_CLIENT_SECRET",
        "MCP_HUB_INTROSPECTION_CLIENT_SECRET",
    ] {
        assert!(
            std::env::var_os(name).is_none(),
            "hub credential leaked to child"
        );
    }
    match std::env::var("MCP_TEST_STDIO_FIXTURE").unwrap().as_str() {
        "blocked" => {
            std::thread::sleep(Duration::from_secs(30));
            return;
        }
        "malformed" => {
            println!("not json");
            return;
        }
        "oversized" => {
            println!("{}", "x".repeat(4_000_001));
            return;
        }
        "exit" => return,
        _ => {}
    }
    let input = io::stdin();
    let mut lines = input.lock().lines();
    while let Some(line) = lines.next() {
        let message: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let messages = message.as_array().cloned().unwrap_or_else(|| vec![message]);
        for request in messages {
            match request["method"].as_str() {
                Some("initialize") => {
                    send(
                        json!({"jsonrpc":"2.0","method":"notifications/message","params":{"data":"hello"}}),
                    );
                    send(
                        json!({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}}),
                    );
                }
                Some("tools/list") => send(
                    json!({"jsonrpc":"2.0","id":request["id"],"result":{"tools":[{"name":"echo","description":"test","inputSchema":{"type":"object"}}]}}),
                ),
                Some("tools/call") => {
                    send(json!({"jsonrpc":"2.0","id":"server-1","method":"roots/list"}));
                    let reply: Value =
                        serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
                    assert_eq!(reply["id"], "server-1");
                    send(
                        json!({"jsonrpc":"2.0","id":request["id"],"result":{"content":[{"type":"text","text":"ok"}]}}),
                    );
                }
                Some("ping") => send(json!({"jsonrpc":"2.0","id":request["id"],"result":{}})),
                Some("notifications/initialized") => {}
                _ => panic!("unexpected fixture message"),
            }
        }
    }
}
fn main() {
    if std::env::var_os("MCP_TEST_STDIO_FIXTURE").is_some() {
        fixture();
        return;
    }
    let mut env = BTreeMap::new();
    env.insert("MCP_TEST_STDIO_FIXTURE".into(), "1".into());
    let log = std::env::temp_dir().join(format!(
        "mcp-stdio-test-{}-{}.log",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let spec = ProcessSpec {
        program: std::env::current_exe().unwrap().into_os_string(),
        args: vec![],
        cwd: None,
        env,
        log: log.clone(),
    };
    let session = StdioTransport::spawn(&spec).unwrap();
    assert!(session.send(&json!({"not":"jsonrpc"})).is_err());
    session
        .send(&json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}))
        .unwrap();
    assert_eq!(
        session.receive(Duration::from_secs(5)).unwrap()["method"],
        "notifications/message"
    );
    assert_eq!(session.receive(Duration::from_secs(5)).unwrap()["id"], 1);
    session
        .send(&json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo"}}))
        .unwrap();
    let request = session.receive(Duration::from_secs(5)).unwrap();
    assert_eq!(request["method"], "roots/list");
    session
        .send(&json!({"jsonrpc":"2.0","id":"server-1","result":{"roots":[]}}))
        .unwrap();
    let result = session.receive(Duration::from_secs(5)).unwrap();
    assert_eq!(result["id"], 2);
    assert_eq!(result["result"]["content"][0]["text"], "ok");
    assert!(session.receive(Duration::from_millis(50)).is_err());
    session.shutdown().unwrap();
    session.shutdown().unwrap();
    drop(session);
    failure_tests(&spec);
    bridge_tests(&spec);
    oauth_session_isolation(&spec);
    executable_test(&spec);
    let _ = std::fs::remove_file(log);
    println!("stdio transport: ok");
}

fn failure_tests(spec: &ProcessSpec) {
    for mode in ["malformed", "oversized", "exit"] {
        let mut fixture = spec.clone();
        fixture
            .env
            .insert("MCP_TEST_STDIO_FIXTURE".into(), mode.into());
        let transport = StdioTransport::spawn(&fixture).unwrap();
        assert!(transport.receive(Duration::from_secs(5)).is_err(), "{mode}");
        transport.shutdown().unwrap();
        transport.shutdown().unwrap();
    }
    let mut blocked = spec.clone();
    blocked
        .env
        .insert("MCP_TEST_STDIO_FIXTURE".into(), "blocked".into());
    let transport = StdioTransport::spawn(&blocked).unwrap();
    let start = std::time::Instant::now();
    assert!(transport
        .send(&json!({"jsonrpc":"2.0","method":"test","params":{"data":"x".repeat(1_000_000)}}))
        .is_err());
    assert!(start.elapsed() < Duration::from_secs(9));
    transport.shutdown().unwrap();
    transport.shutdown().unwrap();
}
const TOKEN: &str = "stdio-test-token-0123456789";
fn post(agent: &ureq::Agent, endpoint: &str, session: Option<&str>) -> ureq::Request {
    let mut request = agent
        .post(endpoint)
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .set("Content-Type", "application/json")
        .set("Accept", "application/json, text/event-stream");
    if let Some(id) = session {
        request = request.set("Mcp-Session-Id", id);
    }
    request
}
fn status(result: Result<ureq::Response, ureq::Error>) -> u16 {
    match result {
        Ok(r) | Err(ureq::Error::Status(_, r)) => r.status(),
        Err(e) => panic!("{e}"),
    }
}
fn events(response: ureq::Response) -> Vec<Value> {
    let mut text = String::new();
    std::io::Read::read_to_string(&mut response.into_reader(), &mut text).unwrap();
    text.lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn initialize(agent: &ureq::Agent, endpoint: &str) -> String {
    initialize_as(agent, endpoint, TOKEN)
}
fn initialize_as(agent: &ureq::Agent, endpoint: &str, token: &str) -> String {
    let response = post(agent, endpoint, None).set("Authorization", &format!("Bearer {token}")).send_json(json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}}})).unwrap();
    let session = response.header("Mcp-Session-Id").unwrap().to_owned();
    assert_eq!(session.len(), 64);
    let messages = events(response);
    assert_eq!(messages.len(), 2);
    assert_eq!(messages[0]["method"], "notifications/message");
    assert_eq!(messages[1]["id"], 1);
    assert_eq!(
        status(
            post(agent, endpoint, Some(&session))
                .set("Authorization", &format!("Bearer {token}"))
                .send_json(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))
        ),
        202
    );
    session
}
fn delete(agent: &ureq::Agent, endpoint: &str, session: &str) -> u16 {
    status(
        agent
            .delete(endpoint)
            .set("Authorization", &format!("Bearer {TOKEN}"))
            .set("Mcp-Session-Id", session)
            .call(),
    )
}
fn bridge_tests(spec: &ProcessSpec) {
    let mut owned_spec = spec.clone();
    let pid_file = spec.log.with_extension("pid");
    owned_spec.env.insert(
        "MCP_TEST_PID_FILE".into(),
        pid_file.clone().into_os_string(),
    );
    let spec = &owned_spec;
    use mcp_hub_rs::server::Proxy;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    let proxy = Arc::new(
        Proxy::bind_stdio(
            0,
            TOKEN.into(),
            std::collections::HashMap::from([("default".into(), spec.clone())]),
            None,
        )
        .unwrap(),
    );
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = std::thread::spawn(move || p.run(s).unwrap());
    let endpoint = format!("http://{}/mcp", proxy.address());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(8))
        .build();
    assert_eq!(status(agent.post(&endpoint).send_json(json!({}))), 401);
    assert_eq!(
        status(
            post(&agent, &endpoint, None)
                .set("Origin", "https://evil.example")
                .send_json(json!({}))
        ),
        403
    );
    assert_eq!(
        status(
            post(&agent, &endpoint, None)
                .send_json(json!({"jsonrpc":"2.0","id":1,"method":"initialize","result":{}}))
        ),
        400
    );
    assert_eq!(
        status(
            post(&agent, &endpoint, None)
                .set("Content-Type", "text/plain")
                .send_string("{}")
        ),
        415
    );
    assert_eq!(
        status(
            post(&agent, &endpoint, None)
                .set("Accept", "application/json")
                .send_json(json!({}))
        ),
        406
    );
    let first = initialize(&agent, &endpoint);
    let first_pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    let second = initialize(&agent, &endpoint);
    let second_pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    assert_ne!(first, second);
    assert_eq!(
        status(
            post(&agent, &endpoint, Some("unknown"))
                .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
        ),
        404
    );
    assert_eq!(
        status(
            post(&agent, &endpoint, Some(&first))
                .send_json(json!({"jsonrpc":"2.0","id":1,"method":"initialize"}))
        ),
        400
    );
    let response = post(&agent, &endpoint, Some(&first))
        .send_json(json!([
            {"jsonrpc":"2.0","id":2,"method":"tools/list"},
            {"jsonrpc":"2.0","id":3,"method":"ping"}
        ]))
        .unwrap();
    let messages = events(response);
    assert_eq!(messages.len(), 2);
    assert_eq!(messages[0]["result"]["tools"][0]["name"], "echo");
    assert_eq!(messages[1]["id"], 3);
    // A server request must be flushed before waiting for the concurrent client reply.
    let response = post(&agent, &endpoint, Some(&first))
        .send_json(json!({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"echo"}}))
        .unwrap();
    let mut stream = io::BufReader::new(response.into_reader());
    let mut line = String::new();
    stream.read_line(&mut line).unwrap();
    let request: Value = serde_json::from_str(line.strip_prefix("data: ").unwrap()).unwrap();
    assert_eq!(request["method"], "roots/list");
    assert_eq!(
        status(
            post(&agent, &endpoint, Some(&first))
                .send_json(json!({"jsonrpc":"2.0","id":5,"method":"ping"}))
        ),
        409
    );
    // The second session is independent of the first session's active request.
    assert_eq!(
        events(
            post(&agent, &endpoint, Some(&second))
                .send_json(json!({"jsonrpc":"2.0","id":8,"method":"ping"}))
                .unwrap()
        )[0]["id"],
        8
    );
    assert_eq!(
        status(
            post(&agent, &endpoint, Some(&first))
                .send_json(json!({"jsonrpc":"2.0","id":"server-1","result":{"roots":[]}}))
        ),
        202
    );
    let mut tail = String::new();
    std::io::Read::read_to_string(&mut stream, &mut tail).unwrap();
    assert!(tail.contains("ok"));
    assert_eq!(delete(&agent, &endpoint, &first), 200);
    assert!(!alive(first_pid));
    assert_eq!(delete(&agent, &endpoint, &first), 404);
    // DELETE interrupts an active server request and terminates its child.
    let response = post(&agent, &endpoint, Some(&second))
        .send_json(json!({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"echo"}}))
        .unwrap();
    let mut stream = io::BufReader::new(response.into_reader());
    line.clear();
    stream.read_line(&mut line).unwrap();
    assert!(line.contains("roots/list"));
    assert_eq!(delete(&agent, &endpoint, &second), 200);
    assert!(!alive(second_pid));
    let mut tail = String::new();
    assert!(std::io::Read::read_to_string(&mut stream, &mut tail).is_err());
    let sessions: Vec<String> = (0..5).map(|_| initialize(&agent, &endpoint)).collect();
    let mut streams = Vec::new();
    for id in &sessions[..4] {
        let response = post(&agent, &endpoint, Some(id))
            .send_json(
                json!({"jsonrpc":"2.0","id":20,"method":"tools/call","params":{"name":"echo"}}),
            )
            .unwrap();
        let mut stream = io::BufReader::new(response.into_reader());
        line.clear();
        stream.read_line(&mut line).unwrap();
        assert!(line.contains("roots/list"));
        streams.push(stream);
    }
    assert_eq!(
        status(
            post(&agent, &endpoint, Some(&sessions[4]))
                .send_json(json!({"jsonrpc":"2.0","id":21,"method":"ping"}))
        ),
        503
    );
    for (id, mut stream) in sessions.iter().zip(streams) {
        assert_eq!(
            status(
                post(&agent, &endpoint, Some(id))
                    .send_json(json!({"jsonrpc":"2.0","id":"server-1","result":{"roots":[]}}))
            ),
            202
        );
        let mut tail = String::new();
        std::io::Read::read_to_string(&mut stream, &mut tail).unwrap();
        assert!(tail.contains("ok"));
    }
    for id in &sessions {
        assert_eq!(delete(&agent, &endpoint, id), 200);
    }
    let active = initialize(&agent, &endpoint);
    let active_pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    let response = post(&agent, &endpoint, Some(&active))
        .send_json(json!({"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"echo"}}))
        .unwrap();
    let mut stream = io::BufReader::new(response.into_reader());
    line.clear();
    stream.read_line(&mut line).unwrap();
    assert!(line.contains("roots/list"));
    let shutdown_start = std::time::Instant::now();
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    assert!(shutdown_start.elapsed() < Duration::from_secs(3));
    assert!(!alive(active_pid));
    let _ = std::fs::remove_file(pid_file);
}
fn executable_test(spec: &ProcessSpec) {
    use std::process::{Command, Stdio};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let config = spec.log.with_extension("json");
    let env: BTreeMap<String, String> = spec
        .env
        .iter()
        .map(|(k, v)| {
            (
                k.to_string_lossy().into_owned(),
                v.to_string_lossy().into_owned(),
            )
        })
        .collect();
    std::fs::write(&config,json!({"services":[{"id":"default","program":spec.program.to_string_lossy(),"args":[],"env":env,"log":spec.log}]}).to_string()).unwrap();
    let mut process = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"));
    for name in [
        "MCP_HUB_CONFIG",
        "MCP_HUB_UPSTREAM",
        "MCP_HUB_UPSTREAM_TOKEN",
        "MCP_HUB_PROCESS_CONFIG",
        "MCP_HUB_ADMIN_TOKEN",
        "MCP_HUB_DB",
        "MCP_HUB_OAUTH_TOKEN_URL",
        "MCP_HUB_OAUTH_CLIENT_ID",
        "MCP_HUB_OAUTH_CLIENT_SECRET",
        "MCP_HUB_OAUTH_SCOPE",
        "MCP_HUB_OAUTH_AUDIENCE",
        "MCP_HUB_OAUTH_AUTH_METHOD",
        "MCP_HUB_OAUTH_ROUTE",
        "MCP_HUB_INTROSPECTION_URL",
        "MCP_HUB_INTROSPECTION_CLIENT_ID",
        "MCP_HUB_INTROSPECTION_CLIENT_SECRET",
        "MCP_HUB_INTROSPECTION_AUTH_METHOD",
        "MCP_HUB_INTROSPECTION_REQUIRED_SCOPES",
        "MCP_HUB_INTROSPECTION_AUDIENCE",
        "MCP_HUB_INTROSPECTION_ISSUER",
        "MCP_HUB_INTROSPECTION_ROUTE",
    ] {
        process.env_remove(name);
    }
    let mut child = process
        .arg("serve")
        .env("MCP_HUB_STDIO_CONFIG", &config)
        .env("MCP_HUB_TOKEN", TOKEN)
        .env("MCP_HUB_PORT", port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(
            std::fs::File::create(spec.log.with_extension("hub.log")).unwrap(),
        ))
        .spawn()
        .unwrap();
    let endpoint = format!("http://127.0.0.1:{port}/mcp");
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let start = std::time::Instant::now();
        while std::net::TcpStream::connect(("127.0.0.1", port)).is_err() {
            assert!(start.elapsed() < Duration::from_secs(5));
            std::thread::sleep(Duration::from_millis(20));
        }
        let session = initialize(&agent, &endpoint);
        assert_eq!(
            events(
                post(&agent, &endpoint, Some(&session))
                    .send_json(json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}))
                    .unwrap()
            )[0]["result"]["tools"][0]["name"],
            "echo"
        );
        assert_eq!(delete(&agent, &endpoint, &session), 200);
    }));
    let _ = child.kill();
    let _ = child.wait();
    let _ = std::fs::remove_file(config);
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}

#[cfg(windows)]
fn alive(pid: u32) -> bool {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, STILL_ACTIVE},
        System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
    };
    // SAFETY: query-only handle for the fixture PID; always closed after use.
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        CloseHandle(handle);
        ok != 0 && code == STILL_ACTIVE as u32
    }
}
#[cfg(unix)]
fn alive(pid: u32) -> bool {
    // SAFETY: signal zero only queries existence of the fixture PID.
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

fn oauth_session_isolation(spec: &ProcessSpec) {
    use mcp_hub_rs::{oauth_introspection::Settings, server::Proxy};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    let introspector = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://{}/introspect", introspector.server_addr());
    let provider = std::thread::spawn(move || {
        for _ in 0..2 {
            let req = introspector
                .recv_timeout(Duration::from_secs(5))
                .unwrap()
                .unwrap();
            req.respond(tiny_http::Response::from_string(json!({"active":true,"scope":"mcp:use","exp":SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs()+120}).to_string())).unwrap();
        }
    });
    let mut proxy = Proxy::bind_stdio(
        0,
        TOKEN.into(),
        std::collections::HashMap::from([("default".into(), spec.clone())]),
        None,
    )
    .unwrap();
    proxy
        .enable_inbound_oauth(
            "default",
            Settings {
                endpoint: url,
                client_id: "fixture".into(),
                client_secret: "fixture-secret".into(),
                auth_method: "basic".into(),
                required_scopes: vec!["mcp:use".into()],
                audience: None,
                issuer: None,
            },
        )
        .unwrap();
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = std::thread::spawn(move || p.run(s).unwrap());
    let endpoint = format!("http://{}/mcp", proxy.address());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(8))
        .build();
    let first = initialize_as(&agent, &endpoint, "oauth-principal-a");
    let second = initialize_as(&agent, &endpoint, "oauth-principal-b");
    // A valid token must not read, inject into or terminate another token's session.
    assert_eq!(
        status(
            post(&agent, &endpoint, Some(&first))
                .set("Authorization", "Bearer oauth-principal-b")
                .send_json(json!({"jsonrpc":"2.0","id":10,"method":"ping"}))
        ),
        404
    );
    assert_eq!(
        status(
            agent
                .delete(&endpoint)
                .set("Authorization", "Bearer oauth-principal-b")
                .set("Mcp-Session-Id", &first)
                .call()
        ),
        404
    );
    assert_eq!(
        events(
            post(&agent, &endpoint, Some(&first))
                .set("Authorization", "Bearer oauth-principal-a")
                .send_json(json!({"jsonrpc":"2.0","id":11,"method":"ping"}))
                .unwrap()
        )[0]["id"],
        11
    );
    for (session, token) in [
        (&first, "oauth-principal-a"),
        (&second, "oauth-principal-b"),
    ] {
        assert_eq!(
            status(
                agent
                    .delete(&endpoint)
                    .set("Authorization", &format!("Bearer {token}"))
                    .set("Mcp-Session-Id", session)
                    .call()
            ),
            200
        );
    }
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    provider.join().unwrap();
}
