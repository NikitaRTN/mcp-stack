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
                proxy_key: None,
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
        "MCP_HUB_TRUSTED_PROXY_KEY",
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

#[test]
fn trusted_https_proxy_boundary_and_secure_cookie() {
    let upstream = Server::http("127.0.0.1:0").unwrap();
    let target = format!("http://{}/mcp", upstream.server_addr());
    let upstream_thread = thread::spawn(move || {
        let req = upstream
            .recv_timeout(Duration::from_secs(20))
            .unwrap()
            .unwrap();
        assert!(!req
            .headers()
            .iter()
            .any(|h| h.field.equiv("Authorization") || h.field.equiv("X-Mcp-Hub-Proxy-Key")));
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
    let local = format!("http://{}", proxy.address());
    let base = "https://mcp.example.test";
    let key = "proxy-key-abcdefghijklmnopqrstuvwxyz-123456789";
    let db = std::env::temp_dir().join(format!("mcp-public-oauth-{}.db", std::process::id()));
    let _ = std::fs::remove_file(&db);
    proxy
        .enable_builtin_oauth(
            "default",
            Config {
                database: db.clone(),
                signing_key: "test-key-for-oauth".into(),
                auth: create_password("admin", "correct-password").unwrap(),
                base: base.into(),
                path: String::new(),
                scopes: vec!["mcp:tools".into()],
                proxy_key: Some(key.into()),
            },
        )
        .unwrap();
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = thread::spawn(move || p.run(s).unwrap());
    let agent = ureq::AgentBuilder::new()
        .redirects(0)
        .timeout(Duration::from_secs(10))
        .build();
    let metadata = format!("{local}/.well-known/oauth-authorization-server");
    assert!(matches!(
        agent.get(&metadata).call(),
        Err(ureq::Error::Status(403, _))
    ));
    assert!(matches!(
        agent.get(&metadata).set("Host", "mcp.example.test").call(),
        Err(ureq::Error::Status(403, _))
    ));
    assert!(matches!(
        agent
            .get(&metadata)
            .set("Host", "mcp.example.test")
            .set("X-Mcp-Hub-Proxy-Key", "wrong")
            .call(),
        Err(ureq::Error::Status(403, _))
    ));
    assert!(matches!(
        agent
            .get(&metadata)
            .set("Host", "attacker.test")
            .set("X-Mcp-Hub-Proxy-Key", key)
            .call(),
        Err(ureq::Error::Status(403, _))
    ));
    assert!(matches!(
        agent
            .get(&metadata)
            .set("Host", "mcp.example.test")
            .set("X-Mcp-Hub-Proxy-Key", key)
            .set("Origin", "https://evil.test")
            .call(),
        Err(ureq::Error::Status(403, _))
    ));
    let info: Value = agent
        .get(&metadata)
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(info["issuer"], base);
    let client: Value = agent
        .post(&format!("{local}/oauth/register"))
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .send_json(json!({"redirect_uris":["https://client.example/callback"]}))
        .unwrap()
        .into_json()
        .unwrap();
    let id = client["client_id"].as_str().unwrap();
    let verifier = "v".repeat(64);
    let challenge =
        URL_SAFE_NO_PAD.encode(digest::digest(&digest::SHA256, verifier.as_bytes()).as_ref());
    let mut authorize = Url::parse(&format!("{local}/oauth/authorize")).unwrap();
    authorize
        .query_pairs_mut()
        .append_pair("response_type", "code")
        .append_pair("client_id", id)
        .append_pair("redirect_uri", "https://client.example/callback")
        .append_pair("resource", &format!("{base}/mcp"))
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");
    let response = agent
        .get(authorize.as_str())
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .call()
        .unwrap();
    let set_cookie = response.header("Set-Cookie").unwrap();
    assert!(set_cookie.contains("; Secure"));
    assert!(set_cookie.contains("SameSite=Strict"));
    let cookie = set_cookie.split(';').next().unwrap().to_string();
    let nonce = cookie.strip_prefix("mcp_oauth_nonce=").unwrap();
    let consent = agent
        .post(&format!("{local}/oauth/authorize"))
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .set("Origin", base)
        .set("Cookie", &cookie)
        .send_form(&[
            ("nonce", nonce),
            ("action", "allow"),
            ("password", "correct-password"),
        ]);
    let consent = match consent {
        Err(ureq::Error::Status(303, r)) => r,
        Ok(r) if r.status() == 303 => r,
        _ => panic!("expected redirect"),
    };
    assert!(consent.header("Set-Cookie").unwrap().contains("; Secure"));
    let redirect = Url::parse(consent.header("Location").unwrap()).unwrap();
    let code = redirect
        .query_pairs()
        .find(|(k, _)| k == "code")
        .unwrap()
        .1
        .into_owned();
    let fields = [
        ("grant_type", "authorization_code"),
        ("client_id", id),
        ("redirect_uri", "https://client.example/callback"),
        ("code", code.as_str()),
        ("code_verifier", verifier.as_str()),
    ];
    let token: Value = agent
        .post(&format!("{local}/oauth/token"))
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .send_form(&fields)
        .unwrap()
        .into_json()
        .unwrap();
    let access = token["access_token"].as_str().unwrap();
    let response = agent
        .post(&format!("{local}/mcp"))
        .set("Host", "mcp.example.test")
        .set("X-Mcp-Hub-Proxy-Key", key)
        .set("Authorization", &format!("Bearer {access}"))
        .send_json(json!({"jsonrpc":"2.0","id":1,"method":"ping"}))
        .unwrap();
    assert_eq!(response.status(), 200);
    upstream_thread.join().unwrap();
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    drop(proxy);
    let _ = std::fs::remove_file(db);
}

#[test]
fn caddy_tls_overwrites_untrusted_header_when_available() {
    use std::{
        net::TcpListener,
        process::{Command, Stdio},
    };
    let Ok(binary) = std::env::var("CADDY_TEST_BIN") else {
        return;
    };
    let dir = std::env::temp_dir().join(format!("mcp-caddy-oauth-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let tls_port = listener.local_addr().unwrap().port();
    drop(listener);
    let base = format!("https://localhost:{tls_port}");
    let key = "test-proxy-key-abcdefghijklmnopqrstuvwxyz-123456789";
    let mut proxy = Proxy::bind(Settings {
        port: 0,
        upstream: "http://127.0.0.1:1/mcp".into(),
        access_token: "static-token-0123456789-abcdef".into(),
        upstream_token: None,
        telemetry_db: None,
    })
    .unwrap();
    proxy
        .enable_builtin_oauth(
            "default",
            Config {
                database: dir.join("oauth.db"),
                signing_key: "test-key-for-oauth".into(),
                auth: create_password("admin", "correct-password").unwrap(),
                base: base.clone(),
                path: String::new(),
                scopes: vec!["mcp:tools".into()],
                proxy_key: Some(key.into()),
            },
        )
        .unwrap();
    let target = proxy.address();
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let worker = thread::spawn(move || p.run(s).unwrap());
    let file = dir.join("Caddyfile");
    std::fs::write(&file,format!("{{\n admin off\n auto_https disable_redirects\n skip_install_trust\n}}\n{base} {{\n tls internal\n @hub path /mcp /oauth/* /.well-known/oauth-authorization-server /.well-known/openid-configuration /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/mcp\n handle @hub {{\n  reverse_proxy {target} {{\n   header_up X-Mcp-Hub-Proxy-Key {key}\n  }}\n }}\n handle {{\n  respond 404\n }}\n}}\n")).unwrap();
    struct Guard(std::process::Child);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let child = Command::new(binary)
        .args(["run", "--adapter", "caddyfile", "--config"])
        .arg(&file)
        .env("XDG_DATA_HOME", &dir)
        .env("XDG_CONFIG_HOME", &dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut child = Guard(child);
    let mut passed = false;
    for _ in 0..100 {
        if child.0.try_wait().unwrap().is_some() {
            break;
        }
        let out = Command::new("curl")
            .args([
                "--silent",
                "--insecure",
                "--max-time",
                "1",
                "-H",
                "X-Mcp-Hub-Proxy-Key: attacker",
                &format!("{base}/.well-known/oauth-authorization-server"),
            ])
            .output()
            .unwrap();
        if out.status.success() {
            if let Ok(v) = serde_json::from_slice::<Value>(&out.stdout) {
                if v["issuer"] == base {
                    passed = true;
                    break;
                }
            }
        }
        thread::sleep(Duration::from_millis(150));
    }
    drop(child);
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    drop(proxy);
    let _ = std::fs::remove_dir_all(&dir);
    assert!(passed, "Caddy HTTPS forwarding failed");
}

#[test]
fn rejects_misconfigured_public_oauth_without_touching_old_installation() {
    for (base, key) in [
        ("https://mcp.example.test", None),
        ("http://mcp.example.test", Some("a".repeat(43))),
        ("https://mcp.example.test", Some("short".into())),
        ("https://mcp.example.test", Some("{".repeat(43))),
        (
            "https://user:password@mcp.example.test",
            Some("a".repeat(43)),
        ),
        ("https://mcp.example.test/path", Some("a".repeat(43))),
    ] {
        let mut proxy = Proxy::bind(Settings {
            port: 0,
            upstream: "http://127.0.0.1:1/mcp".into(),
            access_token: "static-token-0123456789-abcdef".into(),
            upstream_token: None,
            telemetry_db: None,
        })
        .unwrap();
        let db = std::env::temp_dir().join(format!("mcp-bad-oauth-{}.db", std::process::id()));
        let _ = std::fs::remove_file(&db);
        let result = proxy.enable_builtin_oauth(
            "default",
            Config {
                database: db.clone(),
                signing_key: "test-signing-key".into(),
                auth: create_password("admin", "correct-password").unwrap(),
                base: base.into(),
                path: String::new(),
                scopes: vec!["mcp:tools".into()],
                proxy_key: key,
            },
        );
        assert!(result.is_err());
        assert!(!db.exists());
    }
}
