use mcp_hub_rs::{
    managed,
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
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
const TOKEN: &str = "test-proxy-token-0123456789";
const ADMIN: &str = "test-admin-token-9876543210";

#[test]
fn managed_service_helper() {
    let Ok(port) = std::env::var("MCP_TEST_MANAGED_PORT") else {
        return;
    };
    let listener = TcpListener::bind(format!("127.0.0.1:{port}")).unwrap();
    listener.set_nonblocking(true).unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut buffer = [0u8; 4096];
            let _ = stream.read(&mut buffer);
            let body = "{\"ok\":true}";
            let _ = write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body);
        } else {
            thread::sleep(Duration::from_millis(10));
        }
    }
}

#[test]
fn management_starts_proxies_and_stops_owned_service() {
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    let log = std::env::temp_dir().join(format!(
        "mcp-managed-{}-{}.log",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let config = json!({"services":[{"id":"default", "program":std::env::current_exe().unwrap(), "args":["--exact", "managed_service_helper", "--nocapture"], "env":{"MCP_TEST_MANAGED_PORT":port.to_string()}, "log":log}]});
    let specs = managed::read_config(config.to_string().as_bytes()).unwrap();
    let mut proxy = Proxy::bind(Settings {
        port: 0,
        upstream: format!("http://127.0.0.1:{port}/mcp"),
        access_token: TOKEN.into(),
        upstream_token: None,
        telemetry_db: None,
    })
    .unwrap();
    assert!(proxy
        .enable_management(TOKEN.into(), specs.clone())
        .is_err());
    proxy.enable_management(ADMIN.into(), specs).unwrap();
    let proxy = Arc::new(proxy);
    let stop = Arc::new(AtomicBool::new(false));
    let (p, s) = (proxy.clone(), stop.clone());
    let join = thread::spawn(move || p.run(s).unwrap());
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    let admin_url = format!("http://{}/_hub/services/default", proxy.address());
    let auth = format!("Bearer {ADMIN}");
    assert!(matches!(
        agent
            .post(&admin_url)
            .set("Authorization", &format!("Bearer {TOKEN}"))
            .send_string(""),
        Err(ureq::Error::Status(401, _))
    ));
    assert!(matches!(
        agent
            .post(&admin_url)
            .set("Authorization", &auth)
            .set("Origin", "https://attacker.test")
            .send_string(""),
        Err(ureq::Error::Status(403, _))
    ));
    let state: Value = agent
        .get(&admin_url)
        .set("Authorization", &auth)
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(state["running"], false);
    drop(reservation);
    let first: Value = agent
        .post(&admin_url)
        .set("Authorization", &auth)
        .send_string("")
        .unwrap()
        .into_json()
        .unwrap();
    let second: Value = agent
        .post(&admin_url)
        .set("Authorization", &auth)
        .send_string("")
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(first["pid"], second["pid"]);
    let deadline = Instant::now() + Duration::from_secs(8);
    while TcpStream::connect(("127.0.0.1", port)).is_err() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(20));
    }
    let data: Value = agent
        .get(&format!("http://{}/mcp", proxy.address()))
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(data["ok"], true);
    let state: Value = agent
        .delete(&admin_url)
        .set("Authorization", &auth)
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(state["stopped"], true);
    let state: Value = agent
        .get(&admin_url)
        .set("Authorization", &auth)
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(state["running"], false);
    assert!(TcpStream::connect(("127.0.0.1", port)).is_err());
    stop.store(true, Ordering::Relaxed);
    join.join().unwrap();
    drop(proxy);
    let _ = std::fs::remove_file(log);
}

#[test]
fn executable_enables_explicit_management_config() {
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Command, Stdio};
    struct Guard(Child);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    let dir = std::env::temp_dir().join(format!(
        "mcp-exe-management-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&dir).unwrap();
    let config = dir.join("processes.json");
    let contents = json!({"services":[{"id":"default","program":std::env::current_exe().unwrap(),"args":["--exact","managed_service_helper","--nocapture"],"env":{"MCP_TEST_MANAGED_PORT":port.to_string()},"log":dir.join("service.log")}]}).to_string();
    std::fs::write(&config, &contents).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_mcp-hub-rs"))
        .arg("serve")
        .env("MCP_HUB_PORT", "0")
        .env("MCP_HUB_TOKEN", TOKEN)
        .env("MCP_HUB_ADMIN_TOKEN", ADMIN)
        .env("MCP_HUB_PROCESS_CONFIG", &config)
        .env("MCP_HUB_UPSTREAM", format!("http://127.0.0.1:{port}/mcp"))
        .env_remove("MCP_HUB_UPSTREAM_TOKEN")
        .env_remove("MCP_HUB_CONFIG")
        .env_remove("MCP_HUB_DB")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut guard = Guard(child);
    let mut line = String::new();
    BufReader::new(guard.0.stderr.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(!line.contains(ADMIN) && !line.contains(TOKEN));
    let base = line
        .trim()
        .split_once(" at ")
        .expect("startup failed")
        .1
        .trim_end_matches("/mcp");
    let admin_url = format!("{base}/_hub/services/default");
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    let auth = format!("Bearer {ADMIN}");
    drop(reservation);
    let value: Value = agent
        .post(&admin_url)
        .set("Authorization", &auth)
        .send_string("")
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(value["running"], true);
    let deadline = Instant::now() + Duration::from_secs(8);
    while TcpStream::connect(("127.0.0.1", port)).is_err() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(20));
    }
    let value: Value = agent
        .get(&format!("{base}/mcp"))
        .set("Authorization", &format!("Bearer {TOKEN}"))
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(value["ok"], true);
    let value: Value = agent
        .delete(&admin_url)
        .set("Authorization", &auth)
        .call()
        .unwrap()
        .into_json()
        .unwrap();
    assert_eq!(value["stopped"], true);
    drop(guard);
    assert_eq!(std::fs::read_to_string(config).unwrap(), contents);
    std::fs::remove_dir_all(dir).unwrap();
}
