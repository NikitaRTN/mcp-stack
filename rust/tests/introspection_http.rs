use mcp_hub_rs::{
    oauth_introspection::{Denied, Introspection, Settings},
    server::{Proxy, Settings as ProxySettings},
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tiny_http::{Header, Response, Server};
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}
fn settings(endpoint: String) -> Settings {
    Settings {
        endpoint,
        client_id: "client:id".into(),
        client_secret: "secret+value".into(),
        auth_method: "basic".into(),
        required_scopes: vec!["mcp:read".into()],
        audience: Some("urn:test:mcp".into()),
        issuer: Some("https://issuer.example".into()),
    }
}
fn active() -> Value {
    json!({"active":true,"exp":now()+120,"scope":"mcp:read mcp:other","aud":["urn:test:mcp"],"iss":"https://issuer.example"})
}
fn fixture(responses: Vec<Value>, method: &'static str) -> (String, thread::JoinHandle<()>) {
    let server = Server::http("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/introspect", server.server_addr());
    let worker = thread::spawn(move || {
        for response in responses {
            let mut req = server
                .recv_timeout(Duration::from_secs(10))
                .unwrap()
                .expect("introspection request");
            assert_eq!(req.url(), "/introspect");
            assert_eq!(req.method().as_str(), "POST");
            let mut body = String::new();
            req.as_reader().read_to_string(&mut body).unwrap();
            let fields: std::collections::HashMap<String, String> =
                url::form_urlencoded::parse(body.as_bytes())
                    .into_owned()
                    .collect();
            assert_eq!(fields["token_type_hint"], "access_token");
            assert!(!fields["token"].is_empty());
            if method == "body" {
                assert_eq!(fields["client_id"], "client:id");
                assert_eq!(fields["client_secret"], "secret+value");
                assert!(!req.headers().iter().any(|h| h.field.equiv("Authorization")));
            } else {
                use base64::Engine;
                let auth = req
                    .headers()
                    .iter()
                    .find(|h| h.field.equiv("Authorization"))
                    .unwrap()
                    .value
                    .as_str();
                let credentials = base64::engine::general_purpose::STANDARD
                    .decode(auth.strip_prefix("Basic ").unwrap())
                    .unwrap();
                assert_eq!(
                    String::from_utf8(credentials).unwrap(),
                    "client%3Aid:secret%2Bvalue"
                );
                assert!(!fields.contains_key("client_secret"));
            }
            req.respond(
                Response::from_string(response.to_string())
                    .with_header(Header::from_bytes("Content-Type", "application/json").unwrap()),
            )
            .unwrap();
        }
    });
    (endpoint, worker)
}
#[test]
fn positive_cache_and_body_credentials() {
    for method in ["basic", "body"] {
        let (url, worker) = fixture(vec![active()], method);
        let mut config = settings(url);
        config.auth_method = method.into();
        let validator = Introspection::new(config).unwrap();
        assert_eq!(validator.validate("Bearer valid-token"), Ok(()));
        assert_eq!(validator.validate("bearer valid-token"), Ok(()));
        worker.join().unwrap();
    }
}
#[test]
fn validates_activity_expiration_scope_issuer_and_audience() {
    let mut cases = vec![
        (json!({"active":false}), Denied::Invalid),
        (json!({"active":"true"}), Denied::Invalid),
        (json!([]), Denied::Unavailable),
    ];
    for (key, value, denied) in [
        ("exp", json!(now() - 1), Denied::Invalid),
        ("exp", json!(now()), Denied::Invalid),
        ("exp", json!("9999999999"), Denied::Invalid),
        ("nbf", json!(now() + 120), Denied::Invalid),
        ("scope", json!("mcp:other"), Denied::Scope),
        ("aud", json!("other-resource"), Denied::Invalid),
        ("iss", json!("other-issuer"), Denied::Invalid),
        ("aud", json!(["urn:test:mcp", 12]), Denied::Invalid),
    ] {
        let mut v = active();
        v[key] = value;
        cases.push((v, denied));
    }
    let (url, worker) = fixture(cases.iter().map(|(v, _)| v.clone()).collect(), "basic");
    let validator = Introspection::new(settings(url)).unwrap();
    for (index, (_, expected)) in cases.into_iter().enumerate() {
        assert_eq!(
            validator.validate(&format!("Bearer token-{index}")),
            Err(expected)
        );
    }
    worker.join().unwrap();
}
#[test]
fn no_expiration_means_no_cache_and_invalid_headers_never_call_endpoint() {
    let mut response = active();
    response.as_object_mut().unwrap().remove("exp");
    let (url, worker) = fixture(vec![response.clone(), response], "basic");
    let validator = Introspection::new(settings(url)).unwrap();
    for invalid in [
        "",
        "Basic foo",
        "Bearer ",
        "Bearer bad token",
        "Bearer bad\nvalue",
    ] {
        assert_eq!(validator.validate(invalid), Err(Denied::Invalid));
    }
    assert_eq!(validator.validate("Bearer valid"), Ok(()));
    assert_eq!(validator.validate("Bearer valid"), Ok(()));
    worker.join().unwrap();
}
#[test]
fn rejects_unsafe_endpoints_and_bad_configuration() {
    for endpoint in [
        "http://public.example/introspect",
        "https://user:secret@example.com/introspect",
        "https://example.com/introspect#fragment",
        "file:///tmp/token",
    ] {
        assert!(Introspection::new(settings(endpoint.into())).is_err());
    }
    let mut config = settings("https://example.com/introspect".into());
    config.required_scopes = vec!["bad scope".into()];
    assert!(Introspection::new(config).is_err());
}
#[test]
fn does_not_follow_redirects_or_accept_malformed_or_oversized_responses() {
    for body in [
        "redirect".to_owned(),
        "not json".to_owned(),
        "x".repeat(1_000_001),
    ] {
        let server = Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://{}/introspect", server.server_addr());
        let worker = thread::spawn(move || {
            let req = server
                .recv_timeout(Duration::from_secs(5))
                .unwrap()
                .unwrap();
            let response = if body == "redirect" {
                Response::from_string("private-error")
                    .with_status_code(302)
                    .with_header(Header::from_bytes("Location", "http://127.0.0.1:1/leak").unwrap())
            } else {
                Response::from_string(body)
            };
            let _ = req.respond(response);
        });
        let validator = Introspection::new(settings(url)).unwrap();
        assert_eq!(validator.validate("Bearer token"), Err(Denied::Unavailable));
        worker.join().unwrap();
    }
}
#[test]
fn proxy_enforces_introspection_without_static_fallback_or_token_forwarding() {
    let mut denied = active();
    denied["scope"] = json!("other");
    let (url, introspector) = fixture(vec![active(), json!({"active":false}), denied], "basic");
    let upstream = Server::http("127.0.0.1:0").unwrap();
    let target = format!("http://{}/mcp", upstream.server_addr());
    let upstream_worker = thread::spawn(move || {
        let mut req = upstream
            .recv_timeout(Duration::from_secs(8))
            .unwrap()
            .unwrap();
        assert!(!req.headers().iter().any(|h| h.field.equiv("Authorization")));
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let request: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(request["id"], 7);
        req.respond(Response::from_string(
            json!({"jsonrpc":"2.0","id":7,"result":{}}).to_string(),
        ))
        .unwrap();
    });
    let static_token = "static-test-token-0123456789";
    let mut proxy = Proxy::bind(ProxySettings {
        port: 0,
        upstream: target,
        access_token: static_token.into(),
        upstream_token: None,
        telemetry_db: None,
    })
    .unwrap();
    proxy
        .enable_inbound_oauth("default", settings(url))
        .unwrap();
    let endpoint = format!("http://{}/mcp", proxy.address());
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = thread::spawn(move || p.run(s).unwrap());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(10))
        .build();
    let valid = agent
        .post(&endpoint)
        .set("Authorization", "Bearer external-valid")
        .send_json(json!({"jsonrpc":"2.0","id":7,"method":"ping"}))
        .unwrap();
    assert_eq!(valid.status(), 200);
    for (token, expected) in [(static_token, 401), ("insufficient", 403)] {
        let error = agent
            .post(&endpoint)
            .set("Authorization", &format!("Bearer {token}"))
            .send_json(json!({"jsonrpc":"2.0","id":8,"method":"ping"}))
            .unwrap_err();
        let ureq::Error::Status(status, response) = error else {
            panic!("expected status");
        };
        assert_eq!(status, expected);
        assert!(response
            .header("WWW-Authenticate")
            .unwrap()
            .contains(if status == 401 {
                "invalid_token"
            } else {
                "insufficient_scope"
            }));
    }
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    introspector.join().unwrap();
    upstream_worker.join().unwrap();
}

#[test]
fn executable_applies_inbound_configuration_and_fails_closed_when_provider_disappears() {
    use std::process::{Command, Stdio};
    let (url, introspector) = fixture(vec![active()], "body");
    let upstream = Server::http("127.0.0.1:0").unwrap();
    let target = format!("http://{}/mcp", upstream.server_addr());
    let fixture = thread::spawn(move || {
        let req = upstream
            .recv_timeout(Duration::from_secs(8))
            .unwrap()
            .unwrap();
        assert!(!req.headers().iter().any(|h| h.field.equiv("Authorization")));
        let _ = req.respond(Response::from_string(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}",
        ));
    });
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"));
    for name in [
        "MCP_HUB_CONFIG",
        "MCP_HUB_STDIO_CONFIG",
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
        "MCP_HUB_INTROSPECTION_ROUTE",
    ] {
        command.env_remove(name);
    }
    let mut child = command
        .arg("serve")
        .env("MCP_HUB_PORT", port.to_string())
        .env("MCP_HUB_UPSTREAM", target)
        .env("MCP_HUB_TOKEN", "unused-static-token-0123456789")
        .env("MCP_HUB_INTROSPECTION_URL", url)
        .env("MCP_HUB_INTROSPECTION_CLIENT_ID", "client:id")
        .env("MCP_HUB_INTROSPECTION_CLIENT_SECRET", "secret+value")
        .env("MCP_HUB_INTROSPECTION_AUTH_METHOD", "body")
        .env("MCP_HUB_INTROSPECTION_REQUIRED_SCOPES", "mcp:read")
        .env("MCP_HUB_INTROSPECTION_AUDIENCE", "urn:test:mcp")
        .env("MCP_HUB_INTROSPECTION_ISSUER", "https://issuer.example")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let result = std::panic::catch_unwind(|| {
        let start = std::time::Instant::now();
        while std::net::TcpStream::connect(("127.0.0.1", port)).is_err() {
            assert!(start.elapsed() < Duration::from_secs(5));
            thread::sleep(Duration::from_millis(20));
        }
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(10))
            .build();
        let endpoint = format!("http://127.0.0.1:{port}/mcp");
        let response = agent
            .post(&endpoint)
            .set("Authorization", "Bearer active-token")
            .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
            .unwrap();
        assert_eq!(response.status(), 200);
        let error = agent
            .post(&endpoint)
            .set("Authorization", "Bearer different-token")
            .send_json(json!({"jsonrpc":"2.0","id":2,"method":"ping"}))
            .unwrap_err();
        let ureq::Error::Status(status, response) = error else {
            panic!("expected failure status");
        };
        assert_eq!(status, 503);
        let body = response.into_string().unwrap();
        assert!(!body.contains("secret+value"));
        assert!(!body.contains("different-token"));
    });
    let _ = child.kill();
    let _ = child.wait();
    introspector.join().unwrap();
    fixture.join().unwrap();
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}
