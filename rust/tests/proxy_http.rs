use mcp_hub_rs::{
    server::{Proxy, Settings},
    Client,
};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Duration, Instant};
const TOKEN: &str = "test-proxy-token-0123456789";
const UPSTREAM_TOKEN: &str = "test-upstream-token-0123456789";
fn read_request(stream: &mut TcpStream) -> (String, Vec<u8>) {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut all = Vec::new();
    let mut buf = [0u8; 4096];
    let end = loop {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0);
        all.extend_from_slice(&buf[..n]);
        if let Some(pos) = all.windows(4).position(|x| x == b"\r\n\r\n") {
            break pos + 4;
        }
    };
    let head = String::from_utf8(all[..end].to_vec()).unwrap();
    let length = head
        .lines()
        .find_map(|s| {
            let (k, v) = s.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                Some(v.trim().parse::<usize>().unwrap())
            } else {
                None
            }
        })
        .unwrap_or(0);
    while all.len() - end < length {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0);
        all.extend_from_slice(&buf[..n]);
    }
    (head, all[end..end + length].to_vec())
}
fn start(
    upstream: String,
    db: Option<PathBuf>,
) -> (Arc<Proxy>, Arc<AtomicBool>, thread::JoinHandle<()>) {
    let proxy = Arc::new(
        Proxy::bind(Settings {
            port: 0,
            upstream,
            access_token: TOKEN.into(),
            upstream_token: Some(UPSTREAM_TOKEN.into()),
            telemetry_db: db,
        })
        .unwrap(),
    );
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let join = thread::spawn(move || p.run(s).unwrap());
    (proxy, stop, join)
}
fn stop(stop: Arc<AtomicBool>, join: thread::JoinHandle<()>) {
    stop.store(true, Ordering::Relaxed);
    join.join().unwrap();
}
#[test]
fn full_client_session_auth_sse_and_telemetry() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (proxy, shutdown, join) = start(
        format!("http://{addr}/mcp"),
        Some(PathBuf::from(":memory:")),
    );
    let endpoint = format!("http://{}/mcp", proxy.address());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build();
    assert!(matches!(
        agent.get(&endpoint).call(),
        Err(ureq::Error::Status(401, _))
    ));
    assert!(matches!(
        agent
            .get(&endpoint)
            .set("Authorization", &format!("Bearer {TOKEN}"))
            .set("Origin", "https://malicious.example")
            .call(),
        Err(ureq::Error::Status(403, _))
    ));
    let worker = thread::spawn(move || {
        for index in 0..4 {
            let (mut stream, _) = listener.accept().unwrap();
            let (head, body) = read_request(&mut stream);
            let lower = head.to_ascii_lowercase();
            assert!(lower.contains(&format!("authorization: bearer {UPSTREAM_TOKEN}\r\n")));
            assert!(!lower.contains(TOKEN));
            if index > 0 {
                assert!(lower.contains("mcp-session-id: session-123\r\n"));
            }
            if index != 3 {
                let request: Value = serde_json::from_slice(&body).unwrap();
                assert_eq!(
                    request["method"],
                    ["initialize", "notifications/initialized", "tools/list"][index]
                );
            } else {
                assert!(head.starts_with("DELETE "));
            }
            let (status, mime, payload) = match index {
                0 => ("200 OK", "application/json", json!({"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}).to_string()),
                1 | 3 => ("202 Accepted", "application/json", String::new()),
                _ => ("200 OK", "text/event-stream", "data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[{\"name\":\"echo\"}]}}\n\n".into()),
            };
            write!(stream, "HTTP/1.1 {status}\r\nContent-Type: {mime}\r\nMcp-Session-Id: session-123\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", payload.len(), payload).unwrap();
        }
    });
    let mut client = Client::new(&endpoint, Some(TOKEN.into())).unwrap();
    let tools = client.list_tools().unwrap();
    assert_eq!(tools[0]["name"], "echo");
    client.close();
    worker.join().unwrap();
    let store = proxy.telemetry().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    let calls = loop {
        let calls = store.list(Some("default"), 20).unwrap();
        if calls.len() == 3 && calls.iter().all(|c| c.status != "running") {
            break calls;
        }
        assert!(
            Instant::now() < deadline,
            "telemetry did not finish: {calls:?}"
        );
        thread::sleep(Duration::from_millis(20));
    };
    assert!(calls.iter().all(|c| c.status == "ok"), "{calls:?}");
    stop(shutdown, join);
}
#[test]
fn rejects_untrusted_configuration() {
    let settings = |url: &str, token: &str| Settings {
        port: 0,
        upstream: url.into(),
        access_token: token.into(),
        upstream_token: Some(UPSTREAM_TOKEN.into()),
        telemetry_db: None,
    };
    assert!(Proxy::bind(settings("http://example.com/mcp", TOKEN)).is_err());
    assert!(Proxy::bind(settings("http://secret@localhost/mcp", TOKEN)).is_err());
    assert!(Proxy::bind(settings("http://localhost/mcp", "short")).is_err());
}

#[test]
fn streams_sse_before_upstream_closes() {
    use std::io::{BufRead, BufReader};
    use std::sync::mpsc;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (proxy, shutdown, join) = start(format!("http://{addr}/mcp"), None);
    let (release, wait) = mpsc::channel::<()>();
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let _ = read_request(&mut stream);
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: first\n\n").unwrap();
        stream.flush().unwrap();
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
    });
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    let response = agent
        .get(&format!("http://{}/mcp", proxy.address()))
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .call()
        .unwrap();
    let mut reader = BufReader::new(response.into_reader());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert_eq!(line, "data: first\n");
    release.send(()).unwrap();
    worker.join().unwrap();
    drop(reader);
    stop(shutdown, join);
}

#[test]
fn records_batch_results_and_notifications_without_arguments() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (proxy, shutdown, join) = start(
        format!("http://{addr}/mcp"),
        Some(PathBuf::from(":memory:")),
    );
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let (_, body) = read_request(&mut stream);
        let value: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value.as_array().unwrap().len(), 3);
        let payload = json!([
            {"jsonrpc":"2.0","id":"two","result":{"isError":true}},
            {"jsonrpc":"2.0","id":1,"result":{"content":[]}}
        ])
        .to_string();
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", payload.len(), payload).unwrap();
    });
    let body = json!([
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"good","arguments":{"secret":"dont-store"}}},
        {"jsonrpc":"2.0","id":"two","method":"tools/call","params":{"name":"bad"}},
        {"jsonrpc":"2.0","method":"notifications/initialized"}
    ]);
    let url = format!("http://{}/mcp", proxy.address());
    let response = ureq::post(&url)
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .send_json(body)
        .unwrap()
        .into_json::<Value>()
        .unwrap();
    assert_eq!(response.as_array().unwrap().len(), 2);
    worker.join().unwrap();
    let store = proxy.telemetry().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    let calls = loop {
        let calls = store.list(Some("default"), 20).unwrap();
        if calls.len() == 3 && calls.iter().all(|c| c.status != "running") {
            break calls;
        }
        assert!(
            Instant::now() < deadline,
            "telemetry did not finish: {calls:?}"
        );
        thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(
        calls
            .iter()
            .find(|c| c.tool.as_deref() == Some("good"))
            .unwrap()
            .status,
        "ok"
    );
    assert_eq!(
        calls
            .iter()
            .find(|c| c.tool.as_deref() == Some("bad"))
            .unwrap()
            .status,
        "error"
    );
    assert_eq!(
        calls
            .iter()
            .find(|c| c.method == "notifications/initialized")
            .unwrap()
            .status,
        "ok"
    );
    assert!(calls.iter().all(|c| c.args.is_none()));
    stop(shutdown, join);
}

#[test]
fn compiled_executable_serves_mcp_over_http() {
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Command, Stdio};
    struct ChildGuard(Child);
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let upstream = listener.local_addr().unwrap();
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let (_, request) = read_request(&mut stream);
        let value: Value = serde_json::from_slice(&request).unwrap();
        assert_eq!(value["method"], "ping");
        let payload = json!({"jsonrpc":"2.0","id":7,"result":{}}).to_string();
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", payload.len(), payload).unwrap();
    });
    let child = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"))
        .arg("serve")
        .env("MCP_HUB_PORT", "0")
        .env("MCP_HUB_TOKEN", TOKEN)
        .env("MCP_HUB_UPSTREAM", format!("http://{upstream}/mcp"))
        .env_remove("MCP_HUB_UPSTREAM_TOKEN")
        .env_remove("MCP_HUB_DB")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut guard = ChildGuard(child);
    let mut line = String::new();
    BufReader::new(guard.0.stderr.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(!line.contains(TOKEN));
    let url = line.trim().split_once(" at ").expect("startup failed").1;
    let value = ureq::post(url)
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .send_json(json!({"jsonrpc":"2.0","id":7,"method":"ping"}))
        .unwrap()
        .into_json::<Value>()
        .unwrap();
    assert_eq!(value["id"], 7);
    worker.join().unwrap();
}
