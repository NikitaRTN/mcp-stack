//! Experimental stdio-to-Streamable-HTTP adapter; one child per MCP session.
use crate::{process::ProcessSpec, stdio::StdioTransport};
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::Value;
use std::{
    collections::HashMap,
    io::{self, Read},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tiny_http::{Header, Method, Request, Response};

const MAX_BODY: u64 = 4_000_000;
const MAX_SESSIONS: usize = 16;
const IDLE: Duration = Duration::from_secs(900);
struct Session {
    owner: [u8; 32],
    transport: StdioTransport,
    busy: AtomicBool,
    closed: AtomicBool,
    touched: Mutex<Instant>,
}
pub struct Bridge {
    stopped: AtomicBool,
    stream_budget: Arc<AtomicUsize>,
    spec: ProcessSpec,
    sessions: Mutex<HashMap<String, Arc<Session>>>,
}
fn error(req: Request, code: u16, msg: &'static str) {
    let _ = req.respond(Response::from_string(msg).with_status_code(code));
}
fn header(req: &Request, name: &'static str) -> Result<Option<String>, ()> {
    let mut result = None;
    for h in req.headers() {
        if h.field.equiv(name) {
            if result.is_some() {
                return Err(());
            }
            result = Some(h.value.as_str().to_owned());
        }
    }
    Ok(result)
}
fn id() -> Result<String, &'static str> {
    let mut bytes = [0u8; 32];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| "random generator unavailable")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
impl Bridge {
    pub(crate) fn new(spec: ProcessSpec, stream_budget: Arc<AtomicUsize>) -> Self {
        Self {
            stopped: AtomicBool::new(false),
            stream_budget,
            spec,
            sessions: Mutex::new(HashMap::new()),
        }
    }
    pub fn shutdown(&self) {
        self.stopped.store(true, Ordering::Release);
        if let Ok(mut sessions) = self.sessions.lock() {
            for session in sessions.values() {
                session.closed.store(true, Ordering::Release);
                let _ = session.transport.shutdown();
            }
            sessions.clear();
        }
    }
    pub fn reap(&self) {
        if let Ok(mut sessions) = self.sessions.lock() {
            sessions.retain(|_, s| {
                let keep = !s.closed.load(Ordering::Acquire)
                    && (s.busy.load(Ordering::Acquire)
                        || s.touched
                            .lock()
                            .map(|t| t.elapsed() < IDLE)
                            .unwrap_or(false));
                if !keep {
                    s.closed.store(true, Ordering::Release);
                    let _ = s.transport.shutdown();
                }
                keep
            });
        }
    }
    pub fn handle(&self, mut req: Request, owner: [u8; 32]) {
        if self.stopped.load(Ordering::Acquire) {
            return error(req, 503, "bridge shutting down");
        }
        let session_id = match header(&req, "Mcp-Session-Id") {
            Ok(value) => value,
            Err(_) => return error(req, 400, "duplicate session header"),
        };
        if req.method() == &Method::Get {
            return error(req, 405, "GET stream is not supported");
        }
        if req.method() == &Method::Delete {
            if !matches!(header(&req, "Transfer-Encoding"), Ok(None))
                || req.body_length().is_some_and(|n| n != 0)
            {
                return error(req, 400, "DELETE body unsupported");
            }
            let Some(id) = session_id else {
                return error(req, 400, "session required");
            };
            let removed = self.sessions.lock().ok().and_then(|mut map| {
                if map.get(&id).is_some_and(|s| s.owner == owner) {
                    map.remove(&id)
                } else {
                    None
                }
            });
            return if let Some(session) = removed {
                session.closed.store(true, Ordering::Release);
                let _ = session.transport.shutdown();
                let _ = req.respond(Response::empty(200));
            } else {
                error(req, 404, "session not found")
            };
        }
        if req.method() != &Method::Post {
            return error(req, 405, "method not allowed");
        }
        if !matches!(header(&req, "Content-Type"), Ok(Some(ref v)) if v.split(';').next().is_some_and(|t| t.trim().eq_ignore_ascii_case("application/json")))
        {
            return error(req, 415, "application/json required");
        }
        if !matches!(header(&req, "Accept"), Ok(Some(ref v)) if ["application/json", "text/event-stream"].iter().all(|kind| v.split(',').any(|t| t.split(';').next().is_some_and(|t| t.trim().eq_ignore_ascii_case(kind)))))
        {
            return error(req, 406, "JSON and SSE accept types required");
        }
        if !matches!(header(&req, "Mcp-Protocol-Version"), Ok(None))
            && !matches!(header(&req, "Mcp-Protocol-Version"), Ok(Some(ref v)) if v == "2025-03-26")
        {
            return error(req, 400, "unsupported protocol version");
        }
        if !matches!(header(&req, "Transfer-Encoding"), Ok(None)) {
            return error(req, 400, "transfer encoding unsupported");
        }
        let Some(length) = req.body_length() else {
            return error(req, 411, "content length required");
        };
        if length as u64 > MAX_BODY {
            return error(req, 413, "body too large");
        }
        let mut data = Vec::new();
        if req
            .as_reader()
            .take(MAX_BODY + 1)
            .read_to_end(&mut data)
            .is_err()
            || data.len() != length
        {
            return error(req, 400, "incomplete body");
        }
        let Ok(message) = serde_json::from_slice::<Value>(&data) else {
            return error(req, 400, "invalid JSON");
        };
        if !crate::stdio::valid_message(&message) {
            return error(req, 400, "invalid JSON-RPC");
        }
        let messages: Vec<&Value> = match message.as_array() {
            Some(items) if items.len() <= 32 => items.iter().collect(),
            Some(_) => return error(req, 400, "batch too large"),
            None => vec![&message],
        };
        let mut request_ids = Vec::new();
        for item in &messages {
            if item.get("method").is_some() {
                if let Some(id) = item.get("id") {
                    if request_ids.contains(id) {
                        return error(req, 400, "duplicate request ID");
                    }
                    request_ids.push(id.clone());
                }
            }
        }
        let is_request = !request_ids.is_empty();
        let initializing = messages
            .iter()
            .any(|m| m.get("method").and_then(Value::as_str) == Some("initialize"));
        if initializing && (message.is_array() || !is_request) {
            return error(req, 400, "initialize must be a single request");
        }
        let creating = session_id.is_none();
        // Keep HTTP workers available for server-request replies and DELETE even under load.
        let permit = if is_request {
            match StreamPermit::acquire(self.stream_budget.clone()) {
                Some(permit) => Some(permit),
                None => return error(req, 503, "active stream limit reached"),
            }
        } else {
            None
        };
        let (session_id, session) = if let Some(id) = session_id {
            let session = self
                .sessions
                .lock()
                .ok()
                .and_then(|map| map.get(&id).cloned());
            let Some(session) = session else {
                return error(req, 404, "session not found");
            };
            if session.owner != owner {
                return error(req, 404, "session not found");
            }
            if session.closed.load(Ordering::Acquire) {
                return error(req, 404, "session closed");
            }
            if initializing {
                return error(req, 400, "session already initialized");
            }
            (id, session)
        } else {
            if !initializing {
                return error(req, 400, "initialize required");
            }
            let Ok(mut sessions) = self.sessions.lock() else {
                return error(req, 503, "session store unavailable");
            };
            if self.stopped.load(Ordering::Acquire) {
                return error(req, 503, "bridge shutting down");
            }
            if sessions.len() >= MAX_SESSIONS {
                return error(req, 503, "session limit reached");
            }
            let Ok(id) = id() else {
                return error(req, 503, "random generator unavailable");
            };
            if sessions.contains_key(&id) {
                return error(req, 503, "session collision");
            }
            let Ok(transport) = StdioTransport::spawn(&self.spec) else {
                return error(req, 502, "cannot start stdio process");
            };
            let session = Arc::new(Session {
                owner,
                transport,
                busy: AtomicBool::new(true),
                closed: AtomicBool::new(false),
                touched: Mutex::new(Instant::now()),
            });
            sessions.insert(id.clone(), session.clone());
            (id, session)
        };
        if let Ok(mut touched) = session.touched.lock() {
            *touched = Instant::now();
        }
        if is_request
            && !creating
            && session
                .busy
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return error(req, 409, "another request is active");
        }
        if session.transport.send(&message).is_err() {
            session.closed.store(true, Ordering::Release);
            session.busy.store(false, Ordering::Release);
            return error(req, 502, "stdio write failed");
        }
        if !is_request {
            let _ = req.respond(Response::empty(202));
            return;
        }
        if req.http_version().0 != 1 || req.http_version().1 != 1 {
            session.closed.store(true, Ordering::Release);
            let _ = session.transport.shutdown();
            session.busy.store(false, Ordering::Release);
            return error(req, 505, "SSE requires HTTP/1.1");
        }
        let mut stream = Events {
            _permit: permit,
            session,
            request_ids,
            deadline: Instant::now() + Duration::from_secs(60),
            buffer: Vec::new(),
            offset: 0,
            finished: false,
        };
        let headers = vec![
            Header::from_bytes("Content-Type", "text/event-stream").unwrap(),
            Header::from_bytes("Cache-Control", "no-store").unwrap(),
            Header::from_bytes("Mcp-Session-Id", session_id).unwrap(),
        ];
        let mut writer = req.into_writer();
        if crate::server::send_sse(&mut writer, 200, &headers, &mut stream).is_err() {
            stream.session.closed.store(true, Ordering::Release);
            let _ = stream.session.transport.shutdown();
        }
    }
}
struct StreamPermit(Arc<AtomicUsize>);
impl StreamPermit {
    fn acquire(budget: Arc<AtomicUsize>) -> Option<Self> {
        budget
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                (n < 4).then_some(n + 1)
            })
            .ok()?;
        Some(Self(budget))
    }
}
impl Drop for StreamPermit {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}
struct Events {
    _permit: Option<StreamPermit>,
    session: Arc<Session>,
    request_ids: Vec<Value>,
    deadline: Instant,
    buffer: Vec<u8>,
    offset: usize,
    finished: bool,
}
impl Read for Events {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        if out.is_empty() {
            return Ok(0);
        }
        if self.offset == self.buffer.len() {
            if self.finished {
                return Ok(0);
            }
            if self.session.closed.load(Ordering::Acquire) {
                return Err(io::Error::other("session closed"));
            }
            let remaining = self
                .deadline
                .checked_duration_since(Instant::now())
                .ok_or_else(|| io::Error::other("request timed out"))?;
            let value = self
                .session
                .transport
                .receive(remaining)
                .map_err(io::Error::other)?;
            let values: Vec<&Value> = match value.as_array() {
                Some(items) => items.iter().collect(),
                None => vec![&value],
            };
            for item in values {
                if item.get("method").is_none() {
                    let Some(index) = self
                        .request_ids
                        .iter()
                        .position(|id| item.get("id") == Some(id))
                    else {
                        return Err(io::Error::other("unexpected response ID"));
                    };
                    self.request_ids.remove(index);
                }
            }
            self.finished = self.request_ids.is_empty();
            self.buffer = format!("data: {}\n\n", value).into_bytes();
            self.offset = 0;
        }
        let count = out.len().min(self.buffer.len() - self.offset);
        out[..count].copy_from_slice(&self.buffer[self.offset..self.offset + count]);
        self.offset += count;
        Ok(count)
    }
}
impl Drop for Events {
    fn drop(&mut self) {
        if !self.finished || self.offset != self.buffer.len() {
            self.session.closed.store(true, Ordering::Release);
            let _ = self.session.transport.shutdown();
        }
        self.session.busy.store(false, Ordering::Release);
    }
}
