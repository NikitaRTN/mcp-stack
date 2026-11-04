use base64::{engine::general_purpose::STANDARD, Engine};
use mcp_hub_rs::{
    oauth_client::{ClientCredentials, Settings as OAuthSettings},
    server::{Proxy, Settings},
};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};
const ACCESS: &str = "local-proxy-test-token-0123456789";
const ISSUED: &str = "issued-upstream-token-123";
fn read_request(stream: &mut TcpStream) -> (String, String) {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut all = Vec::new();
    let mut buf = [0u8; 4096];
    let end = loop {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0);
        all.extend_from_slice(&buf[..n]);
        if let Some(i) = all.windows(4).position(|x| x == b"\r\n\r\n") {
            break i + 4;
        }
    };
    let head = String::from_utf8(all[..end].to_vec()).unwrap();
    let len = head
        .lines()
        .find_map(|x| {
            let (k, v) = x.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                Some(v.trim().parse::<usize>().unwrap())
            } else {
                None
            }
        })
        .unwrap_or(0);
    while all.len() - end < len {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0);
        all.extend_from_slice(&buf[..n]);
    }
    (
        head,
        String::from_utf8(all[end..end + len].to_vec()).unwrap(),
    )
}
fn answer(stream: &mut TcpStream, status: &str, body: &str) {
    write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
}
fn settings(url: String, method: &str) -> OAuthSettings {
    OAuthSettings {
        token_url: url,
        client_id: "client:id".into(),
        client_secret: "secret space".into(),
        scope: Some("mcp:read".into()),
        audience: Some("test audience".into()),
        auth_method: method.into(),
    }
}

#[test]
fn client_credentials_are_cached_and_proxy_uses_issued_token() {
    let token_listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/token", token_listener.local_addr().unwrap());
    let upstream = TcpListener::bind("127.0.0.1:0").unwrap();
    let upstream_url = format!("http://{}/mcp", upstream.local_addr().unwrap());
    let token_worker = thread::spawn(move || {
        let (mut s, _) = token_listener.accept().unwrap();
        let (headers, body) = read_request(&mut s);
        let expected = STANDARD.encode("client%3Aid:secret+space");
        assert!(headers.to_ascii_lowercase().contains(&format!(
            "authorization: basic {}",
            expected.to_ascii_lowercase()
        )));
        assert!(
            body.contains("grant_type=client_credentials")
                && body.contains("scope=mcp%3Aread")
                && body.contains("audience=test+audience")
        );
        assert!(!body.contains("secret"));
        answer(
            &mut s,
            "200 OK",
            &json!({"access_token":ISSUED,"token_type":"Bearer","expires_in":120}).to_string(),
        );
    });
    let upstream_worker = thread::spawn(move || {
        for _ in 0..2 {
            let (mut s, _) = upstream.accept().unwrap();
            let (headers, _) = read_request(&mut s);
            assert!(headers.contains(&format!("Bearer {ISSUED}")));
            assert!(!headers.contains(ACCESS));
            answer(&mut s, "200 OK", r#"{"jsonrpc":"2.0","id":1,"result":{}}"#);
        }
    });
    let mut proxy = Proxy::bind(Settings {
        port: 0,
        upstream: upstream_url,
        access_token: ACCESS.into(),
        upstream_token: None,
        telemetry_db: None,
    })
    .unwrap();
    proxy
        .enable_upstream_oauth("default", settings(endpoint, "basic"))
        .unwrap();
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = thread::spawn(move || p.run(s).unwrap());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build();
    for _ in 0..2 {
        let value: Value = agent
            .post(&format!("http://{}/mcp", proxy.address()))
            .set("Authorization", &format!("Bearer {ACCESS}"))
            .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
            .unwrap()
            .into_json()
            .unwrap();
        assert_eq!(value["id"], 1);
    }
    token_worker.join().unwrap();
    upstream_worker.join().unwrap();
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
}

#[test]
fn rejects_insecure_or_invalid_oauth_configuration() {
    assert!(ClientCredentials::new(settings("http://example.com/token".into(), "basic")).is_err());
    assert!(ClientCredentials::new(settings(
        "https://user:password@example.com/token".into(),
        "basic"
    ))
    .is_err());
    assert!(ClientCredentials::new(settings(
        "https://example.com/token#fragment".into(),
        "basic"
    ))
    .is_err());
    assert!(ClientCredentials::new(settings("https://example.com/token".into(), "other")).is_err());
}

#[test]
fn body_auth_and_invalid_token_type_are_handled() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/token", listener.local_addr().unwrap());
    let worker = thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        let (headers, body) = read_request(&mut s);
        assert!(!headers.to_ascii_lowercase().contains("authorization:"));
        assert!(
            body.contains("client_id=client%3Aid") && body.contains("client_secret=secret+space")
        );
        answer(
            &mut s,
            "200 OK",
            r#"{"access_token":"secret","token_type":"mac","expires_in":120}"#,
        );
    });
    let client = ClientCredentials::new(settings(url, "body")).unwrap();
    assert!(client.token().is_err());
    worker.join().unwrap();
}

#[test]
fn token_endpoint_redirect_is_not_followed() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/token", listener.local_addr().unwrap());
    let worker = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let _ = read_request(&mut stream);
        write!(stream,"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/steal\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
    });
    assert!(ClientCredentials::new(settings(url, "basic"))
        .unwrap()
        .token()
        .is_err());
    worker.join().unwrap();
}

#[test]
fn zero_expiry_is_not_cached() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/token", listener.local_addr().unwrap());
    let worker = thread::spawn(move || {
        for i in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_request(&mut stream);
            answer(
                &mut stream,
                "200 OK",
                &json!({"access_token":format!("issued-{i}"),"token_type":"Bearer","expires_in":0})
                    .to_string(),
            );
        }
    });
    let client = ClientCredentials::new(settings(url, "basic")).unwrap();
    assert_eq!(client.token().unwrap(), "issued-0");
    assert_eq!(client.token().unwrap(), "issued-1");
    worker.join().unwrap();
}

#[test]
fn executable_configures_outbound_oauth_from_environment() {
    use std::{
        io::{BufRead, BufReader},
        process::{Child, Command, Stdio},
    };
    struct Guard(Child);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let token_server = TcpListener::bind("127.0.0.1:0").unwrap();
    let upstream = TcpListener::bind("127.0.0.1:0").unwrap();
    let token_url = format!("http://{}/token", token_server.local_addr().unwrap());
    let upstream_url = format!("http://{}/mcp", upstream.local_addr().unwrap());
    let token_worker = thread::spawn(move || {
        let (mut stream, _) = token_server.accept().unwrap();
        let (_, body) = read_request(&mut stream);
        assert!(
            body.contains("client_id=client%3Aid") && body.contains("client_secret=secret+space")
        );
        answer(
            &mut stream,
            "200 OK",
            &json!({"access_token":ISSUED,"token_type":"Bearer","expires_in":120}).to_string(),
        );
    });
    let upstream_worker = thread::spawn(move || {
        let (mut stream, _) = upstream.accept().unwrap();
        let (headers, _) = read_request(&mut stream);
        assert!(headers.contains(&format!("Bearer {ISSUED}")));
        answer(
            &mut stream,
            "200 OK",
            r#"{"jsonrpc":"2.0","id":1,"result":{}}"#,
        );
    });
    let child = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"))
        .arg("serve")
        .env("MCP_HUB_PORT", "0")
        .env("MCP_HUB_UPSTREAM", upstream_url)
        .env("MCP_HUB_TOKEN", ACCESS)
        .env("MCP_HUB_OAUTH_TOKEN_URL", token_url)
        .env("MCP_HUB_OAUTH_CLIENT_ID", "client:id")
        .env("MCP_HUB_OAUTH_CLIENT_SECRET", "secret space")
        .env("MCP_HUB_OAUTH_AUTH_METHOD", "body")
        .env_remove("MCP_HUB_OAUTH_ROUTE")
        .env_remove("MCP_HUB_OAUTH_SCOPE")
        .env_remove("MCP_HUB_OAUTH_AUDIENCE")
        .env_remove("MCP_HUB_UPSTREAM_TOKEN")
        .env_remove("MCP_HUB_CONFIG")
        .env_remove("MCP_HUB_DB")
        .env_remove("MCP_HUB_PROCESS_CONFIG")
        .env_remove("MCP_HUB_ADMIN_TOKEN")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut guard = Guard(child);
    let mut line = String::new();
    BufReader::new(guard.0.stderr.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(!line.contains("secret space") && !line.contains(ACCESS));
    let url = line.trim().split_once(" at ").expect("server startup").1;
    let value: Value = ureq::post(url)
        .set("Authorization", &format!("Bearer {ACCESS}"))
        .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(value["id"], 1);
    token_worker.join().unwrap();
    upstream_worker.join().unwrap();
}
