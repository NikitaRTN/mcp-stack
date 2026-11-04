//! Experimental loopback-only authenticated MCP proxy, not the legacy Hub.
use crate::events::EventBus;
use crate::inspector::{self, Outcome, SseScanner};
use crate::process::{ProcessManager, ProcessSpec};
use crate::telemetry::{CallStart, TelemetryStore};
use ring::{hmac, rand::SystemRandom};
use serde_json::Value;
use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use url::Url;
const MAX_BODY: usize = 4_000_000;
const WORKERS: usize = 8;
pub struct Settings {
    pub port: u16,
    pub upstream: String,
    pub access_token: String,
    pub upstream_token: Option<String>,
    /// Explicit separate DB; never automatically opens legacy data.
    pub telemetry_db: Option<PathBuf>,
}
pub struct RouteSpec {
    pub id: String,
    pub path: String,
    pub upstream: String,
    pub upstream_token: Option<String>,
}
struct Route {
    id: String,
    upstream: Url,
    upstream_token: Option<String>,
    oauth: Option<crate::oauth_client::ClientCredentials>,
}
struct Management {
    key: hmac::Key,
    digest: hmac::Tag,
    specs: HashMap<String, ProcessSpec>,
    processes: ProcessManager,
}
pub struct Proxy {
    management: Option<Management>,
    server: Arc<Server>,
    address: SocketAddr,
    agent: ureq::Agent,
    routes: HashMap<String, Route>,
    auth_key: hmac::Key,
    auth_digest: hmac::Tag,
    telemetry: Option<Arc<TelemetryStore>>,
}
fn valid_token(value: &str) -> bool {
    (24..=1024).contains(&value.len()) && value.bytes().all(|c| (0x21..=0x7e).contains(&c))
}
fn loopback_host(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost") || host.parse::<IpAddr>().is_ok_and(|v| v.is_loopback())
}
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
}
fn valid_path(path: &str) -> bool {
    path.starts_with('/')
        && path != "/_hub"
        && !path.starts_with("/_hub/")
        && path.len() > 1
        && path.len() <= 128
        && path
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/._~-".contains(&c))
        && !path.split('/').any(|p| p == "." || p == "..")
}
fn header(req: &Request, name: &'static str) -> Result<Option<String>, ()> {
    let mut found = None;
    for h in req.headers() {
        if h.field.equiv(name) {
            if found.is_some() {
                return Err(());
            }
            found = Some(h.value.as_str().to_owned());
        }
    }
    Ok(found)
}
fn reply(req: Request, status: u16, message: &'static str) {
    let body = serde_json::json!({"error":message}).to_string();
    let response = Response::from_string(body)
        .with_status_code(status)
        .with_header(Header::from_bytes("Content-Type", "application/json").unwrap())
        .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
    let _ = req.respond(response);
}
impl Proxy {
    pub fn bind(settings: Settings) -> Result<Self, String> {
        let route = RouteSpec {
            id: "default".into(),
            path: "/mcp".into(),
            upstream: settings.upstream,
            upstream_token: settings.upstream_token,
        };
        Self::bind_routes(
            settings.port,
            settings.access_token,
            vec![route],
            settings.telemetry_db,
        )
    }
    pub fn bind_routes(
        port: u16,
        access_token: String,
        specs: Vec<RouteSpec>,
        telemetry_db: Option<PathBuf>,
    ) -> Result<Self, String> {
        if !valid_token(&access_token) {
            return Err("MCP_HUB_TOKEN must contain 24-1024 visible ASCII characters".into());
        }
        if specs.is_empty() || specs.len() > 100 {
            return Err("invalid number of routes".into());
        }
        let mut routes = HashMap::new();
        let mut ids = std::collections::HashSet::new();
        for spec in specs {
            if !valid_id(&spec.id) || !valid_path(&spec.path) || !ids.insert(spec.id.clone()) {
                return Err("invalid or duplicate route identity".into());
            }
            if spec.upstream_token.as_ref().is_some_and(|v| {
                v.is_empty() || v.len() > 1024 || !v.bytes().all(|c| (0x21..=0x7e).contains(&c))
            }) {
                return Err("invalid upstream token".into());
            }
            let upstream = Url::parse(&spec.upstream).map_err(|_| "invalid upstream URL")?;
            if !matches!(upstream.scheme(), "http" | "https")
                || upstream.host_str().is_none()
                || !upstream.username().is_empty()
                || upstream.password().is_some()
                || upstream.fragment().is_some()
            {
                return Err(
                    "upstream must be an HTTP(S) URL without credentials or fragment".into(),
                );
            }
            if spec.upstream_token.is_some()
                && upstream.scheme() != "https"
                && !loopback_host(upstream.host_str().unwrap_or_default())
            {
                return Err("upstream bearer token requires HTTPS or loopback".into());
            }
            if routes
                .insert(
                    spec.path,
                    Route {
                        id: spec.id,
                        upstream,
                        upstream_token: spec.upstream_token,
                        oauth: None,
                    },
                )
                .is_some()
            {
                return Err("duplicate route path".into());
            }
        }
        let key = hmac::Key::generate(hmac::HMAC_SHA256, &SystemRandom::new())
            .map_err(|_| "random generator unavailable")?;
        let auth_digest = hmac::sign(&key, format!("Bearer {access_token}").as_bytes());
        let telemetry = telemetry_db
            .as_deref()
            .map(|path| TelemetryStore::open(path, EventBus::default()).map(Arc::new))
            .transpose()?;
        let server = Server::http(SocketAddr::from((Ipv4Addr::LOCALHOST, port)))
            .map_err(|_| "cannot bind loopback server")?;
        let address = server
            .server_addr()
            .to_ip()
            .ok_or("invalid server address")?;
        if routes.values().any(|route| {
            route.upstream.host_str().is_some_and(loopback_host)
                && route.upstream.port_or_known_default() == Some(address.port())
        }) {
            return Err("upstream points to proxy listener".into());
        }
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(60))
            .build();
        Ok(Self {
            management: None,
            server: Arc::new(server),
            address,
            agent,
            routes,
            auth_key: key,
            auth_digest,
            telemetry,
        })
    }
    /// Enable client-credentials auth for one preconfigured route before running the proxy.
    pub fn enable_upstream_oauth(
        &mut self,
        id: &str,
        settings: crate::oauth_client::Settings,
    ) -> Result<(), String> {
        let route = self
            .routes
            .values_mut()
            .find(|r| r.id == id)
            .ok_or("unknown OAuth route")?;
        if route.upstream_token.is_some()
            || route.oauth.is_some()
            || (route.upstream.scheme() != "https"
                && !route.upstream.host_str().is_some_and(loopback_host))
        {
            return Err("OAuth upstream must use HTTPS or loopback and no other credential".into());
        }
        route.oauth = Some(crate::oauth_client::ClientCredentials::new(settings)?);
        Ok(())
    }
    /// Explicit opt-in. The management credential must differ from the proxy credential.
    pub fn enable_management(
        &mut self,
        token: String,
        specs: HashMap<String, ProcessSpec>,
    ) -> Result<(), String> {
        if self.management.is_some()
            || !valid_token(&token)
            || hmac::verify(
                &self.auth_key,
                format!("Bearer {token}").as_bytes(),
                self.auth_digest.as_ref(),
            )
            .is_ok()
        {
            return Err("invalid or duplicate management configuration".into());
        }
        if specs.is_empty()
            || specs.len() > 100
            || specs
                .keys()
                .any(|id| !valid_id(id) || !self.routes.values().any(|r| &r.id == id))
        {
            return Err("process IDs must match configured routes".into());
        }
        let key = hmac::Key::generate(hmac::HMAC_SHA256, &SystemRandom::new())
            .map_err(|_| "random generator unavailable")?;
        let digest = hmac::sign(&key, format!("Bearer {token}").as_bytes());
        self.management = Some(Management {
            key,
            digest,
            specs,
            processes: ProcessManager::new(EventBus::default()),
        });
        Ok(())
    }
    pub fn address(&self) -> SocketAddr {
        self.address
    }
    pub fn telemetry(&self) -> Option<Arc<TelemetryStore>> {
        self.telemetry.clone()
    }
    pub fn run(self: Arc<Self>, shutdown: Arc<AtomicBool>) -> Result<(), String> {
        let mut workers = Vec::new();
        if self.management.is_some() {
            let proxy = self.clone();
            let stop = shutdown.clone();
            workers.push(std::thread::spawn(move || {
                let mut reported = false;
                while !stop.load(Ordering::Relaxed) {
                    let failed = proxy
                        .management
                        .as_ref()
                        .unwrap()
                        .processes
                        .reap_exited()
                        .is_err();
                    if failed && !reported {
                        eprintln!("process monitor failed; retrying");
                    }
                    reported = failed;
                    std::thread::sleep(Duration::from_millis(200));
                }
            }));
        }
        for _ in 0..WORKERS {
            let proxy = self.clone();
            let stop = shutdown.clone();
            workers.push(std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    match proxy.server.recv_timeout(Duration::from_millis(100)) {
                        Ok(Some(req)) => proxy.handle(req),
                        Ok(None) => {}
                        Err(_) => break,
                    }
                }
            }));
        }
        for worker in workers {
            worker.join().map_err(|_| "proxy worker failed")?;
        }
        Ok(())
    }
    fn handle_management(&self, req: Request, authorization: &str) {
        let Some(m) = &self.management else {
            return reply(req, 404, "not found");
        };
        if hmac::verify(&m.key, authorization.as_bytes(), m.digest.as_ref()).is_err() {
            return reply(req, 401, "unauthorized");
        }
        if !matches!(header(&req, "Transfer-Encoding"), Ok(None))
            || req.body_length().unwrap_or(0) != 0
        {
            return reply(req, 400, "request body not supported");
        }
        let Some(id) = req.url().strip_prefix("/_hub/services/") else {
            return reply(req, 404, "not found");
        };
        let Some(spec) = m.specs.get(id) else {
            return reply(req, 404, "not found");
        };
        let result = match req.method() {
            Method::Get => m
                .processes
                .status(id)
                .map(|s| serde_json::json!({"running": s.is_some(), "pid": s.map(|s| s.pid)})),
            Method::Post => m
                .processes
                .start(id, spec.clone())
                .map(|s| serde_json::json!({"running": true, "pid": s.pid})),
            Method::Delete => m
                .processes
                .stop(id)
                .map(|stopped| serde_json::json!({"running": false, "stopped": stopped})),
            _ => return reply(req, 405, "method not allowed"),
        };
        match result {
            Ok(value) => {
                let response = Response::from_string(value.to_string())
                    .with_header(Header::from_bytes("Content-Type", "application/json").unwrap())
                    .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
                let _ = req.respond(response);
            }
            Err(_) => reply(req, 500, "process operation failed"),
        }
    }
    fn handle(&self, mut req: Request) {
        let host = match header(&req, "Host") {
            Ok(Some(v)) => v,
            _ => return reply(req, 400, "invalid host"),
        };
        let local_host = format!("localhost:{}", self.address.port());
        let ip_host = format!("127.0.0.1:{}", self.address.port());
        if !host.eq_ignore_ascii_case(&local_host) && !host.eq_ignore_ascii_case(&ip_host) {
            return reply(req, 403, "invalid host");
        }
        match header(&req, "Origin") {
            Ok(Some(origin))
                if origin != format!("http://{ip_host}")
                    && origin != format!("http://{local_host}") =>
            {
                return reply(req, 403, "invalid origin")
            }
            Err(_) => return reply(req, 400, "invalid origin"),
            _ => {}
        }
        let authorization = match header(&req, "Authorization") {
            Ok(Some(v)) => v,
            _ => return reply(req, 401, "unauthorized"),
        };
        if req.url().starts_with("/_hub/") || req.url() == "/_hub" {
            return self.handle_management(req, &authorization);
        }
        if hmac::verify(
            &self.auth_key,
            authorization.as_bytes(),
            self.auth_digest.as_ref(),
        )
        .is_err()
        {
            return reply(req, 401, "unauthorized");
        }
        let Some(route) = self.routes.get(req.url()) else {
            return reply(req, 404, "not found");
        };
        let method = match req.method() {
            Method::Post => "POST",
            Method::Get => "GET",
            Method::Delete => "DELETE",
            _ => return reply(req, 405, "method not allowed"),
        };
        let mut forwarded = Vec::new();
        for name in [
            "Accept",
            "Content-Type",
            "Mcp-Session-Id",
            "Mcp-Protocol-Version",
            "Last-Event-ID",
        ] {
            match header(&req, name) {
                Ok(Some(value)) => forwarded.push((name, value)),
                Ok(None) => {}
                Err(_) => return reply(req, 400, "duplicate header"),
            }
        }
        if !matches!(header(&req, "Transfer-Encoding"), Ok(None)) {
            return reply(req, 400, "transfer encoding is not supported");
        }
        let length = req.body_length();
        if length.is_some_and(|n| n > MAX_BODY) {
            return reply(req, 413, "request body too large");
        }
        if method == "POST" && length.is_none() {
            return reply(req, 411, "content length required");
        }
        if method != "POST" && length.unwrap_or(0) != 0 {
            return reply(req, 400, "body not supported for this method");
        }
        let mut body = Vec::new();
        if length.unwrap_or(0) > 0
            && req
                .as_reader()
                .take((MAX_BODY + 1) as u64)
                .read_to_end(&mut body)
                .is_err()
        {
            return reply(req, 400, "cannot read request");
        }
        if body.len() > MAX_BODY {
            return reply(req, 413, "request body too large");
        }
        if body.len() != length.unwrap_or(0) {
            return reply(req, 400, "incomplete request body");
        }
        let mut pending = Vec::new();
        if let Some(store) = &self.telemetry {
            for call in inspector::requests(&body) {
                let method = call["method"].as_str().unwrap_or("");
                let rpc_id = call.get("id").cloned();
                let id_text = rpc_id.as_ref().map(Value::to_string);
                let tool = call
                    .pointer("/params/name")
                    .and_then(Value::as_str)
                    .filter(|_| method == "tools/call");
                if let Ok(row) = store.begin(CallStart {
                    service: &route.id,
                    method,
                    tool,
                    rpc_id: id_text.as_deref(),
                    session: None,
                    args: None,
                    request_bytes: body.len() as u64,
                    client: None,
                }) {
                    pending.push(Pending { id: rpc_id, row });
                }
            }
        }
        let mut upstream = self.agent.request(method, route.upstream.as_str());
        for (name, value) in &forwarded {
            upstream = upstream.set(name, value);
        }
        if let Some(oauth) = &route.oauth {
            match oauth.token() {
                Ok(token) => upstream = upstream.set("Authorization", &format!("Bearer {token}")),
                Err(_) => {
                    if let Some(store) = &self.telemetry {
                        for p in pending {
                            let _ = store.finish(
                                p.row,
                                "error",
                                Some("OAuth token request failed"),
                                0,
                                None,
                            );
                        }
                    }
                    return reply(req, 502, "OAuth token request failed");
                }
            }
        } else if let Some(token) = &route.upstream_token {
            upstream = upstream.set("Authorization", &format!("Bearer {token}"));
        }
        let response = if method == "POST" {
            upstream.send_bytes(&body)
        } else {
            upstream.call()
        };
        let response = match response {
            Ok(value) | Err(ureq::Error::Status(_, value)) => value,
            Err(_) => {
                if let Some(store) = &self.telemetry {
                    for p in pending {
                        let _ = store.finish(p.row, "error", Some("upstream unavailable"), 0, None);
                    }
                }
                return reply(req, 502, "upstream unavailable");
            }
        };
        let status = response.status();
        let is_sse = response
            .header("Content-Type")
            .unwrap_or("")
            .to_ascii_lowercase()
            .contains("text/event-stream");
        let response_length = response
            .header("Content-Length")
            .and_then(|s| s.parse::<usize>().ok());
        let mut headers = Vec::new();
        for name in [
            "Content-Type",
            "Content-Encoding",
            "Mcp-Session-Id",
            "Mcp-Protocol-Version",
            "Cache-Control",
            "Retry-After",
        ] {
            if let Some(value) = response.header(name) {
                if let Ok(h) = Header::from_bytes(name, value) {
                    headers.push(h);
                }
            }
        }
        let reader = Observed {
            inner: response.into_reader(),
            store: self.telemetry.clone(),
            pending,
            scanner: SseScanner::default(),
            json: Vec::new(),
            is_sse,
            bytes: 0,
            expected: response_length,
            complete: false,
            failed: false,
            status,
        };
        if is_sse {
            if req.http_version().0 != 1 || req.http_version().1 != 1 {
                return reply(req, 505, "SSE requires HTTP/1.1");
            }
            let mut reader = reader;
            let mut writer = req.into_writer();
            if send_sse(&mut writer, status, &headers, &mut reader).is_err() {
                reader.failed = true;
            }
        } else {
            let response =
                Response::new(StatusCode(status), headers, reader, response_length, None);
            let _ = req.respond(response);
        }
    }
}
struct Pending {
    id: Option<Value>,
    row: i64,
}
struct Observed<R: Read> {
    inner: R,
    store: Option<Arc<TelemetryStore>>,
    pending: Vec<Pending>,
    scanner: SseScanner,
    json: Vec<u8>,
    is_sse: bool,
    bytes: u64,
    expected: Option<usize>,
    complete: bool,
    failed: bool,
    status: u16,
}
impl<R: Read> Observed<R> {
    fn consume(&mut self, message: Value) {
        if let Value::Array(messages) = message {
            for m in messages {
                self.consume(m);
            }
            return;
        }
        let Some(id) = message.get("id") else {
            return;
        };
        let Some(index) = self.pending.iter().position(|p| p.id.as_ref() == Some(id)) else {
            return;
        };
        let pending = self.pending.remove(index);
        let status = if !(200..300).contains(&self.status)
            || inspector::classify(&message) == Outcome::Error
        {
            "error"
        } else {
            "ok"
        };
        if let Some(store) = &self.store {
            let _ = store.finish(pending.row, status, None, self.bytes, None);
        }
    }
    fn finish(&mut self) {
        if !self.is_sse && self.complete && !self.failed && !self.json.is_empty() {
            if let Ok(value) = serde_json::from_slice::<Value>(&self.json) {
                self.consume(value);
            }
            self.json.clear();
        }
        if let Some(store) = &self.store {
            for pending in self.pending.drain(..) {
                let ok = self.complete
                    && !self.failed
                    && (200..300).contains(&self.status)
                    && pending.id.is_none();
                let status = if ok {
                    "ok"
                } else if !self.complete || self.failed {
                    "cancelled"
                } else {
                    "error"
                };
                let _ = store.finish(
                    pending.row,
                    status,
                    if ok {
                        None
                    } else {
                        Some("response not observed")
                    },
                    self.bytes,
                    None,
                );
            }
        }
    }
}
impl<R: Read> Read for Observed<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        match self.inner.read(buffer) {
            Ok(n) => {
                if n == 0 {
                    if buffer.is_empty() {
                        return Ok(0);
                    }
                    if self.expected.is_some_and(|len| self.bytes < len as u64) {
                        self.failed = true;
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "incomplete upstream response",
                        ));
                    }
                    self.complete = true;
                    return Ok(0);
                }
                self.bytes = self.bytes.saturating_add(n as u64);
                if self.is_sse {
                    if let Ok(values) = self.scanner.feed(&buffer[..n]) {
                        for v in values {
                            self.consume(v);
                        }
                    }
                } else if self.json.len() + n <= MAX_BODY {
                    self.json.extend_from_slice(&buffer[..n]);
                }
                if self.expected.is_some_and(|len| self.bytes >= len as u64) {
                    self.complete = true;
                }
                Ok(n)
            }
            Err(e) => {
                self.failed = true;
                Err(e)
            }
        }
    }
}
impl<R: Read> Drop for Observed<R> {
    fn drop(&mut self) {
        if self.expected == Some(0) {
            self.complete = true;
        }
        self.finish();
    }
}

// tiny_http buffers its response writer, so SSE must be flushed after every chunk.
fn send_sse<R: Read, W: Write>(
    writer: &mut W,
    status: u16,
    headers: &[Header],
    reader: &mut R,
) -> io::Result<()> {
    write!(writer, "HTTP/1.1 {status} Upstream\r\n")?;
    for h in headers {
        let value = h.value.as_str();
        if value.contains(['\r', '\n']) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid upstream header",
            ));
        }
        write!(writer, "{}: {}\r\n", h.field, value)?;
    }
    write!(writer, "Transfer-Encoding: chunked\r\n\r\n")?;
    writer.flush()?;
    let mut buffer = [0u8; 8192];
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        write!(writer, "{n:X}\r\n")?;
        writer.write_all(&buffer[..n])?;
        writer.write_all(b"\r\n")?;
        writer.flush()?;
    }
    writer.write_all(b"0\r\n\r\n")?;
    writer.flush()
}

/// Convert an explicitly supplied legacy config into local-only routes without modifying it.
/// Unsupported authentication fails closed; this does not start stdio services.
pub fn legacy_routes(reader: impl Read) -> Result<(String, Vec<RouteSpec>), String> {
    let config = crate::migration::ImportCandidate::read(reader)
        .map_err(str::to_owned)?
        .into_value();
    let token = config
        .get("token")
        .and_then(Value::as_str)
        .ok_or("missing legacy token")?
        .to_owned();
    let services = config
        .get("services")
        .and_then(Value::as_array)
        .ok_or("missing legacy services")?;
    let mut routes = Vec::new();
    for service in services {
        if service.get("enabled").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let get = |key: &str| -> Result<Option<&str>, String> {
            match service.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(value)) => Ok(Some(value)),
                _ => Err("invalid legacy service field".into()),
            }
        };
        if get("authMode")?
            .filter(|v| !v.is_empty())
            .unwrap_or("token")
            != "token"
        {
            return Err("legacy service authentication is not supported yet".into());
        }
        let id = get("id")?.ok_or("missing service id")?.to_owned();
        let path = get("path")?.ok_or("missing service path")?.to_owned();
        let kind = get("kind")?.unwrap_or("remote");
        let (upstream, upstream_token) = match kind {
            "stdio" => {
                let port = service
                    .get("port")
                    .and_then(Value::as_u64)
                    .and_then(|n| u16::try_from(n).ok())
                    .filter(|n| *n > 0)
                    .ok_or("invalid stdio service port")?;
                let path = get("upstreamPath")?
                    .filter(|s| !s.is_empty())
                    .unwrap_or("/mcp");
                if !path.starts_with('/') || path.contains(['\r', '\n', '#']) {
                    return Err("invalid upstream path".into());
                }
                (format!("http://127.0.0.1:{port}{path}"), None)
            }
            "remote" => {
                let raw = get("upstream")?
                    .filter(|s| !s.trim().is_empty())
                    .ok_or("missing remote upstream")?
                    .trim();
                let mut upstream = if raw.contains("://") {
                    raw.to_owned()
                } else {
                    format!("http://{raw}")
                };
                let authority = upstream
                    .split_once("://")
                    .map(|(_, rest)| rest)
                    .unwrap_or("");
                if !authority
                    .split(['?', '#'])
                    .next()
                    .unwrap_or("")
                    .contains('/')
                {
                    let mut url = Url::parse(&upstream).map_err(|_| "invalid upstream URL")?;
                    url.set_path("/mcp");
                    upstream = url.to_string();
                }
                let secret = get("upstreamToken")?.filter(|s| !s.is_empty());
                let mode = get("upstreamAuthMode")?
                    .filter(|v| !v.is_empty())
                    .unwrap_or(if secret.is_some() { "bearer" } else { "none" });
                let token = match mode {
                    "none" => None,
                    "bearer" => Some(secret.ok_or("missing upstream bearer token")?.to_owned()),
                    _ => return Err("legacy upstream authentication is not supported yet".into()),
                };
                (upstream, token)
            }
            _ => return Err("legacy service kind is not supported".into()),
        };
        routes.push(RouteSpec {
            id,
            path,
            upstream,
            upstream_token,
        });
    }
    if routes.is_empty() {
        return Err("no supported enabled legacy services".into());
    }
    Ok((token, routes))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn truncated_upstream_body_is_not_completed() {
        let mut observed = Observed {
            inner: io::Cursor::new(b"short"),
            store: None,
            pending: Vec::new(),
            scanner: SseScanner::default(),
            json: Vec::new(),
            is_sse: false,
            bytes: 0,
            expected: Some(10),
            complete: false,
            failed: false,
            status: 200,
        };
        let mut buffer = Vec::new();
        assert_eq!(
            observed.read_to_end(&mut buffer).unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
        assert!(!observed.complete);
        assert!(observed.failed);
    }
}
