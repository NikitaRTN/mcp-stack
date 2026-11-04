use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use mcp_hub_rs::{
    auth::create_password,
    oauth_builtin_http::Config,
    server::{Proxy, Settings},
};
use ring::digest;
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};
use tiny_http::{Header, Response, Server};
use url::Url;

#[test]
fn builtin_oauth_http_flow_and_fail_closed_access() {
    let upstream = Server::http("127.0.0.1:0").unwrap();
    let target = format!("http://{}/mcp", upstream.server_addr());
    let upstream_thread = thread::spawn(move || {
        let req = upstream
            .recv_timeout(Duration::from_secs(15))
            .unwrap()
            .unwrap();
        assert!(!req.headers().iter().any(|h| h.field.equiv("Authorization")));
        req.respond(
            Response::from_string("{\"ok\":true}")
                .with_header(Header::from_bytes("Content-Type", "application/json").unwrap()),
        )
        .unwrap();
    });
    let mut proxy = Proxy::bind(Settings {
        port: 0,
        upstream: target,
        access_token: "static-token-0123456789-abcdef".into(),
        upstream_token: None,
        telemetry_db: None,
    })
    .unwrap();
    let base = format!("http://{}", proxy.address());
    let resource = format!("{base}/mcp");
    let db = std::env::temp_dir().join(format!("mcp-http-oauth-{}.db", std::process::id()));
    let _ = std::fs::remove_file(&db);
    proxy
        .enable_builtin_oauth(
            "default",
            Config {
                database: db.clone(),
                signing_key: "test-key-for-oauth".into(),
                auth: create_password("admin", "correct-password").unwrap(),
                base: base.clone(),
                path: String::new(),
                scopes: vec!["mcp:tools".into()],
            },
        )
        .unwrap();
    let proxy = Arc::new(proxy);
    let shutdown = Arc::new(AtomicBool::new(false));
    let p = proxy.clone();
    let stop = shutdown.clone();
    let server = thread::spawn(move || p.run(stop).unwrap());
    let agent = ureq::AgentBuilder::new()
        .redirects(0)
        .timeout(Duration::from_secs(10))
        .build();
    let metadata: Value = agent
        .get(&format!("{base}/.well-known/oauth-authorization-server"))
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(metadata["issuer"], base);
    let protected: Value = agent
        .get(&format!("{base}/.well-known/oauth-protected-resource/mcp"))
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(protected["resource"], resource);
    let unauthorized = agent.get(&format!("{base}/mcp")).call();
    match unauthorized {
        Err(ureq::Error::Status(401, response)) => assert!(response
            .header("WWW-Authenticate")
            .unwrap()
            .contains(&format!("{base}/.well-known/oauth-protected-resource/mcp"))),
        _ => panic!("expected discoverable bearer challenge"),
    }
    let code_only: Value = agent.post(&format!("{base}/oauth/register")).send_json(json!({"redirect_uris":["https://client.example/callback"],"grant_types":["authorization_code"]})).unwrap().into_json().unwrap();
    assert_eq!(code_only["grant_types"], json!(["authorization_code"]));
    let r = agent
        .post(&format!("{base}/mcp"))
        .set("Authorization", "Bearer static-token-0123456789-abcdef")
        .send_json(json!({}));
    assert!(matches!(r, Err(ureq::Error::Status(401, _))));
    let client:Value=agent.post(&format!("{base}/oauth/register")).send_json(json!({"client_name":"test","redirect_uris":["https://client.example/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"})).unwrap().into_json().unwrap();
    let id = client["client_id"].as_str().unwrap();
    let verifier = "x".repeat(64);
    let challenge =
        URL_SAFE_NO_PAD.encode(digest::digest(&digest::SHA256, verifier.as_bytes()).as_ref());
    let mut auth = Url::parse(&format!("{base}/oauth/authorize")).unwrap();
    auth.query_pairs_mut()
        .append_pair("response_type", "code")
        .append_pair("client_id", id)
        .append_pair("redirect_uri", "https://client.example/callback")
        .append_pair("resource", &resource)
        .append_pair("scope", "mcp:tools")
        .append_pair("state", "abc")
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");
    let consent = agent.get(auth.as_str()).call().unwrap();
    let cookie = consent
        .header("Set-Cookie")
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let nonce = cookie.strip_prefix("mcp_oauth_nonce=").unwrap();
    let html = consent.into_string().unwrap();
    assert!(html.contains("Owner password"));
    assert!(html.contains(nonce));
    let invalid = agent
        .post(&format!("{base}/oauth/authorize"))
        .set("Cookie", "mcp_oauth_nonce=wrong")
        .send_form(&[
            ("nonce", nonce),
            ("action", "allow"),
            ("password", "correct-password"),
        ]);
    assert!(matches!(invalid, Err(ureq::Error::Status(400, _))));
    let result = agent
        .post(&format!("{base}/oauth/authorize"))
        .set("Cookie", &cookie)
        .send_form(&[
            ("nonce", nonce),
            ("action", "allow"),
            ("password", "correct-password"),
        ]);
    let response = match result {
        Err(ureq::Error::Status(303, r)) => r,
        Ok(r) if r.status() == 303 => r,
        _ => panic!("expected consent redirect"),
    };
    let reused_consent = agent
        .post(&format!("{base}/oauth/authorize"))
        .set("Cookie", &cookie)
        .send_form(&[
            ("nonce", nonce),
            ("action", "allow"),
            ("password", "correct-password"),
        ]);
    assert!(matches!(reused_consent, Err(ureq::Error::Status(400, _))));
    let location = Url::parse(response.header("Location").unwrap()).unwrap();
    let params: std::collections::HashMap<String, String> =
        location.query_pairs().into_owned().collect();
    assert_eq!(params["state"], "abc");
    let code = &params["code"];
    let fields = [
        ("grant_type", "authorization_code"),
        ("client_id", id),
        ("redirect_uri", "https://client.example/callback"),
        ("resource", resource.as_str()),
        ("code", code.as_str()),
        ("code_verifier", verifier.as_str()),
    ];
    let token: Value = agent
        .post(&format!("{base}/oauth/token"))
        .send_form(&fields)
        .unwrap()
        .into_json()
        .unwrap();
    let replay = agent
        .post(&format!("{base}/oauth/token"))
        .send_form(&fields);
    assert!(matches!(replay, Err(ureq::Error::Status(400, _))));
    let access = token["access_token"].as_str().unwrap();
    let refresh = token["refresh_token"].as_str().unwrap();
    let result = agent
        .post(&format!("{base}/mcp"))
        .set("Authorization", &format!("Bearer {access}"))
        .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
        .unwrap();
    assert_eq!(result.status(), 200);
    let refresh_fields = [
        ("grant_type", "refresh_token"),
        ("client_id", id),
        ("resource", resource.as_str()),
        ("refresh_token", refresh),
    ];
    let rotated: Value = agent
        .post(&format!("{base}/oauth/token"))
        .send_form(&refresh_fields)
        .unwrap()
        .into_json()
        .unwrap();
    assert_ne!(rotated["refresh_token"], refresh);
    let replay = agent
        .post(&format!("{base}/oauth/token"))
        .send_form(&refresh_fields);
    assert!(matches!(replay, Err(ureq::Error::Status(400, _))));
    upstream_thread.join().unwrap();
    shutdown.store(true, Ordering::Relaxed);
    server.join().unwrap();
    drop(proxy);
    let _ = std::fs::remove_file(db);
}

#[test]
fn executable_accepts_explicit_builtin_configuration() {
    use std::net::TcpListener;
    use std::process::{Command, Stdio};
    let dir = std::env::temp_dir().join(format!("mcp-oauth-exe-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let base = format!("http://127.0.0.1:{port}");
    let config = dir.join("config.json");
    let db = dir.join("oauth.db");
    std::fs::write(&config,serde_json::to_vec(&json!({"database":db.to_str().unwrap(),"base":base,"scopes":["mcp:tools"],"auth":create_password("admin","correct-password").unwrap()})).unwrap()).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"));
    command
        .arg("serve")
        .env("MCP_HUB_PORT", port.to_string())
        .env("MCP_HUB_UPSTREAM", "http://127.0.0.1:1/mcp")
        .env("MCP_HUB_TOKEN", "static-token-0123456789-abcdef")
        .env("MCP_HUB_BUILTIN_CONFIG", &config)
        .env(
            "MCP_HUB_BUILTIN_SIGNING_KEY",
            "0123456789-abcdefghijklmnopqrstuvwxyz",
        )
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for k in [
        "MCP_HUB_CONFIG",
        "MCP_HUB_STDIO_CONFIG",
        "MCP_HUB_PROCESS_CONFIG",
        "MCP_HUB_ADMIN_TOKEN",
        "MCP_HUB_UPSTREAM_TOKEN",
        "MCP_HUB_DB",
        "MCP_HUB_INTROSPECTION_URL",
        "MCP_HUB_INTROSPECTION_CLIENT_ID",
        "MCP_HUB_INTROSPECTION_CLIENT_SECRET",
        "MCP_HUB_OAUTH_TOKEN_URL",
        "MCP_HUB_OAUTH_CLIENT_ID",
        "MCP_HUB_OAUTH_CLIENT_SECRET",
    ] {
        command.env_remove(k);
    }
    struct Guard(std::process::Child);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let mut child = Guard(command.spawn().unwrap());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(300))
        .build();
    let mut ok = false;
    for _ in 0..80 {
        if child.0.try_wait().unwrap().is_some() {
            break;
        }
        if let Ok(r) = agent
            .get(&format!("{base}/.well-known/oauth-authorization-server"))
            .call()
        {
            let data: Value = r.into_json().unwrap();
            assert_eq!(data["issuer"], base);
            ok = true;
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    drop(child);
    let _ = std::fs::remove_dir_all(&dir);
    assert!(ok, "executable did not serve built-in OAuth metadata");
}
