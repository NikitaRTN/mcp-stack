//! Experimental stdio-to-Streamable-HTTP adapter; one child per MCP session.
use crate::{process::ProcessSpec, stdio::StdioTransport};
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::Value;
use std::{
    collections::{HashMap, VecDeque},
    io::{self, Read},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc::{self, Receiver, SyncSender},
        Arc, Mutex,
    },
    thread,
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
    routing: Mutex<Routing>,
}
struct Active {
    tx: SyncSender<Value>,
    ids: Vec<Value>,
}
#[derive(Default)]
struct Routing {
    active: Option<Active>,
    idle: Option<SyncSender<Value>>,
    backlog: VecDeque<Value>,
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
            return self.get(req, owner, session_id);
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
                routing: Mutex::new(Routing::default()),
            });
            let dispatcher = session.clone();
            thread::spawn(move || dispatch(dispatcher));
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
        let receiver = if is_request {
            let (tx, rx) = mpsc::sync_channel(32);
            let Ok(mut routing) = session.routing.lock() else {
                session.closed.store(true, Ordering::Release);
                let _ = session.transport.shutdown();
                return error(req, 503, "session routing unavailable");
            };
            if routing.idle.is_none() {
                while let Some(value) = routing.backlog.pop_front() {
                    if tx.try_send(value).is_err() {
                        session.closed.store(true, Ordering::Release);
                        let _ = session.transport.shutdown();
                        return error(req, 503, "session queue full");
                    }
                }
            }
            routing.active = Some(Active {
                tx,
                ids: request_ids.clone(),
            });
            Some(rx)
        } else {
            None
        };
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
            receiver: receiver.expect("request receiver"),
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
    fn get(&self, req: Request, owner: [u8; 32], id: Option<String>) {
        if !matches!(header(&req, "Accept"), Ok(Some(ref v)) if v.split(',').any(|t| t.split(';').next().is_some_and(|t| t.trim().eq_ignore_ascii_case("text/event-stream"))))
        {
            return error(req, 406, "SSE accept type required");
        }
        if !matches!(header(&req, "Mcp-Protocol-Version"), Ok(None))
            && !matches!(header(&req, "Mcp-Protocol-Version"), Ok(Some(ref v)) if v == "2025-03-26")
        {
            return error(req, 400, "unsupported protocol version");
        }
        if !matches!(header(&req, "Last-Event-ID"), Ok(None)) {
            return error(req, 400, "event replay unsupported");
        }
        if !matches!(header(&req, "Transfer-Encoding"), Ok(None))
            || req.body_length().is_some_and(|n| n != 0)
        {
            return error(req, 400, "GET body unsupported");
        }
        if req.http_version().0 != 1 || req.http_version().1 != 1 {
            return error(req, 505, "SSE requires HTTP/1.1");
        }
        let Some(id) = id else {
            return error(req, 400, "session required");
        };
        let session = self
            .sessions
            .lock()
            .ok()
            .and_then(|map| map.get(&id).cloned());
        let Some(session) =
            session.filter(|s| s.owner == owner && !s.closed.load(Ordering::Acquire))
        else {
            return error(req, 404, "session not found");
        };
        let Some(permit) = StreamPermit::acquire_idle(self.stream_budget.clone()) else {
            return error(req, 503, "active stream limit reached");
        };
        let (tx, receiver) = mpsc::sync_channel(32);
        {
            let Ok(mut routing) = session.routing.lock() else {
                return error(req, 503, "session routing unavailable");
            };
            if routing.idle.is_some() {
                return error(req, 409, "idle stream already active");
            }
            while let Some(value) = routing.backlog.pop_front() {
                if tx.try_send(value).is_err() {
                    session.closed.store(true, Ordering::Release);
                    let _ = session.transport.shutdown();
                    return error(req, 503, "session queue full");
                }
            }
            routing.idle = Some(tx);
        }
        let mut stream = IdleEvents {
            _permit: permit,
            session,
            receiver,
            buffer: b": connected\n\n".to_vec(),
            offset: 0,
        };
        let headers = vec![
            Header::from_bytes("Content-Type", "text/event-stream").unwrap(),
            Header::from_bytes("Cache-Control", "no-store").unwrap(),
            Header::from_bytes("Mcp-Session-Id", id).unwrap(),
        ];
        let mut writer = req.into_writer();
        let _ = crate::server::send_sse(&mut writer, 200, &headers, &mut stream);
    }
}
fn dispatch(session: Arc<Session>) {
    while !session.closed.load(Ordering::Acquire) {
        let value = match session.transport.poll(Duration::from_millis(200)) {
            Ok(None) => continue,
            Ok(Some(value)) => value,
            Err(_) => break,
        };
        let Ok(mut routing) = session.routing.lock() else {
            break;
        };
        let items: Vec<&Value> = value
            .as_array()
            .map(|a| a.iter().collect())
            .unwrap_or_else(|| vec![&value]);
        let responses: Vec<&Value> = items
            .into_iter()
            .filter(|v| v.get("method").is_none())
            .collect();
        if !responses.is_empty() {
            let Some(active) = routing.active.as_mut() else {
                break;
            };
            let mut valid = true;
            for response in responses {
                if let Some(i) = active
                    .ids
                    .iter()
                    .position(|id| response.get("id") == Some(id))
                {
                    active.ids.remove(i);
                } else {
                    valid = false;
                    break;
                }
            }
            if !valid || active.tx.try_send(value).is_err() {
                break;
            }
            if active.ids.is_empty() {
                routing.active = None;
            }
        } else if let Some(idle) = &routing.idle {
            if idle.try_send(value).is_err() {
                break;
            }
        } else if let Some(active) = &routing.active {
            if active.tx.try_send(value).is_err() {
                break;
            }
        } else if routing.backlog.len() < 32 {
            routing.backlog.push_back(value);
        } else {
            break;
        }
    }
    session.closed.store(true, Ordering::Release);
    let _ = session.transport.shutdown();
    if let Ok(mut routing) = session.routing.lock() {
        routing.active = None;
        routing.idle = None;
    }
}
struct IdleEvents {
    _permit: StreamPermit,
    session: Arc<Session>,
    receiver: Receiver<Value>,
    buffer: Vec<u8>,
    offset: usize,
}
impl Read for IdleEvents {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        if out.is_empty() {
            return Ok(0);
        }
        if self.offset == self.buffer.len() {
            if self.session.closed.load(Ordering::Acquire) {
                return Err(io::Error::other("session closed"));
            }
            self.buffer = match self.receiver.recv_timeout(Duration::from_secs(2)) {
                Ok(value) => format!("data: {}\n\n", value).into_bytes(),
                Err(mpsc::RecvTimeoutError::Timeout) => b": keepalive\n\n".to_vec(),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(io::Error::other("session closed"))
                }
            };
            if let Ok(mut touched) = self.session.touched.lock() {
                *touched = Instant::now();
            }
            self.offset = 0;
        }
        let count = out.len().min(self.buffer.len() - self.offset);
        out[..count].copy_from_slice(&self.buffer[self.offset..self.offset + count]);
        self.offset += count;
        Ok(count)
    }
}
impl Drop for IdleEvents {
    fn drop(&mut self) {
        self.session.closed.store(true, Ordering::Release);
        let _ = self.session.transport.shutdown();
        if let Ok(mut routing) = self.session.routing.lock() {
            routing.idle = None;
        }
    }
}
struct StreamPermit(Arc<AtomicUsize>);
impl StreamPermit {
    fn acquire_idle(budget: Arc<AtomicUsize>) -> Option<Self> {
        budget
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                (n < 2).then_some(n + 1)
            })
            .ok()?;
        Some(Self(budget))
    }
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
    receiver: Receiver<Value>,
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
                .receiver
                .recv_timeout(remaining)
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
        if let Ok(mut routing) = self.session.routing.lock() {
            routing.active = None;
        }
        self.session.busy.store(false, Ordering::Release);
    }
}
