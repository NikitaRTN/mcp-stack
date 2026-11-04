//! Bounded monotonic request timing; never stores headers or payloads.
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::{json, Value};
use std::fmt::Write as _;
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const MAX_EVENTS: usize = 128;
const STAGES: [(&str, &str, &str); 8] = [
    (
        "route",
        "Маршрутизация",
        "Маршрут, настройки и проверка доступа",
    ),
    ("body", "Приём запроса", "Чтение HTTP-тела в инспекторе"),
    ("decode", "Разбор RPC", "JSON-RPC и регистрация задачи"),
    ("connect", "Соединение", "Соединение с upstream MCP"),
    ("send", "Передача в MCP", "Отправка HTTP-запроса upstream"),
    (
        "headers",
        "Ожидание заголовков",
        "До HTTP-заголовков upstream",
    ),
    (
        "receive",
        "Получение ответа",
        "Чтение ответа; включает ожидание данных MCP",
    ),
    (
        "forward",
        "Отправка ответа",
        "Запись и flush ответа в сторону клиента",
    ),
];
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(usize)]
pub enum Stage {
    Route,
    Body,
    Decode,
    Connect,
    Send,
    Headers,
    Receive,
    Forward,
}
#[derive(Clone)]
struct Segment {
    stage: usize,
    start_ms: f64,
    end_ms: f64,
}
struct State {
    start: Instant,
    phase_start: Instant,
    phase: Stage,
    ended: Option<Instant>,
    totals: [f64; 8],
    first: [Option<f64>; 8],
    last: [Option<f64>; 8],
    segments: Vec<Segment>,
    omitted: u64,
    request_bytes: u64,
    response_bytes: u64,
    status: Option<u16>,
    transport: Option<&'static str>,
}
pub struct RequestTrace {
    id: String,
    wall: f64,
    state: Mutex<State>,
}
fn millis(start: Instant, end: Instant) -> f64 {
    end.saturating_duration_since(start).as_secs_f64() * 1000.0
}
fn rounded(value: f64) -> f64 {
    (value * 10000.0).round() / 10000.0
}
impl RequestTrace {
    pub fn new() -> Result<Self, &'static str> {
        let mut bytes = [0u8; 16];
        SystemRandom::new()
            .fill(&mut bytes)
            .map_err(|_| "random generator unavailable")?;
        let mut id = String::with_capacity(32);
        for byte in bytes {
            write!(&mut id, "{byte:02x}").map_err(|_| "cannot generate trace id")?;
        }
        let start = Instant::now();
        let mut first = [None; 8];
        first[0] = Some(0.0);
        Ok(Self {
            id,
            wall: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64(),
            state: Mutex::new(State {
                start,
                phase_start: start,
                phase: Stage::Route,
                ended: None,
                totals: [0.0; 8],
                first,
                last: [None; 8],
                segments: Vec::new(),
                omitted: 0,
                request_bytes: 0,
                response_bytes: 0,
                status: None,
                transport: None,
            }),
        })
    }
    pub fn enter(&self, phase: Stage) -> Result<(), &'static str> {
        self.enter_at(phase, Instant::now())
    }
    fn enter_at(&self, phase: Stage, at: Instant) -> Result<(), &'static str> {
        let mut s = self.state.lock().map_err(|_| "trace unavailable")?;
        if s.ended.is_some() {
            return Err("trace already finished");
        }
        if at < s.phase_start {
            return Err("invalid trace boundary");
        }
        let index = s.phase as usize;
        let begin = millis(s.start, s.phase_start);
        let end = millis(s.start, at);
        s.totals[index] += end - begin;
        s.last[index] = Some(end);
        if s.segments.len() == MAX_EVENTS {
            s.segments.remove(MAX_EVENTS / 2);
            s.omitted += 1;
        }
        s.segments.push(Segment {
            stage: index,
            start_ms: begin,
            end_ms: end,
        });
        s.phase = phase;
        s.phase_start = at;
        let index = phase as usize;
        if s.first[index].is_none() {
            s.first[index] = Some(end);
        }
        Ok(())
    }
    pub fn set_observation(
        &self,
        request_bytes: u64,
        response_bytes: u64,
        status: Option<u16>,
        is_sse: bool,
    ) -> Result<(), &'static str> {
        let mut s = self.state.lock().map_err(|_| "trace unavailable")?;
        if s.ended.is_some() {
            return Err("trace already finished");
        }
        s.request_bytes = request_bytes;
        s.response_bytes = response_bytes;
        s.status = status;
        s.transport = Some(if is_sse { "SSE" } else { "JSON" });
        Ok(())
    }
    pub fn finish(&self) -> Result<(), &'static str> {
        self.finish_at(Instant::now())
    }
    fn finish_at(&self, at: Instant) -> Result<(), &'static str> {
        let mut s = self.state.lock().map_err(|_| "trace unavailable")?;
        if s.ended.is_some() || at < s.phase_start {
            return Err("invalid trace boundary");
        }
        s.ended = Some(at);
        Ok(())
    }
    pub fn snapshot(&self) -> Result<Value, &'static str> {
        self.snapshot_at(Instant::now())
    }
    fn snapshot_at(&self, now: Instant) -> Result<Value, &'static str> {
        let s = self.state.lock().map_err(|_| "trace unavailable")?;
        let at = s.ended.unwrap_or(now);
        if at < s.phase_start {
            return Err("invalid trace boundary");
        }
        let total = millis(s.start, at);
        let current = s.phase as usize;
        let begin = millis(s.start, s.phase_start);
        let mut totals = s.totals;
        totals[current] += total - begin;
        let mut last = s.last;
        last[current] = Some(total);
        let mut events: Vec<Value> = s
            .segments
            .iter()
            .map(|e| json!({"stage":STAGES[e.stage].0,"start_ms":e.start_ms,"end_ms":e.end_ms}))
            .collect();
        if events.len() < MAX_EVENTS {
            events.push(json!({"stage":STAGES[current].0,"start_ms":begin,"end_ms":total}));
        }
        let stages: Vec<Value>=STAGES.iter().enumerate().map(|(i,(id,label,detail))| {
            let measured=s.first[i].is_some();
            json!({"id":id,"label":label,"detail":detail,"duration_ms":if measured {Some(rounded(totals[i]))}else{None},
                "start_ms":s.first[i].map(rounded),"end_ms":if measured {Some(rounded(last[i].unwrap_or(total)))}else{None},
                "state":if s.ended.is_none() && i==current {"active"}else if measured {"done"}else{"pending"}})
        }).collect();
        Ok(
            json!({"version":1,"id":self.id,"scope":"inspector","boundary":"Приём HTTP в инспекторе → запись и flush ответа",
            "started":self.wall,"total_ms":rounded(total),"complete":s.ended.is_some(),"phase":if s.ended.is_some(){None}else{Some(STAGES[current].0)},
            "stages":stages,"events":events,"events_omitted":s.omitted,"transport":s.transport,"http_status":s.status,
            "request_bytes":s.request_bytes,"response_bytes":s.response_bytes,
            "outside_scope":["Клиент → Caddy → инспектор","Доставка после flush и отображение ответа в Notion","Отдельные внутренние операции инструмента MCP"]}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    #[test]
    fn stage_durations_are_monotonic_and_completion_is_frozen() {
        let trace = RequestTrace::new().unwrap();
        let start = trace.state.lock().unwrap().start;
        trace
            .enter_at(Stage::Body, start + Duration::from_millis(10))
            .unwrap();
        trace
            .enter_at(Stage::Forward, start + Duration::from_millis(30))
            .unwrap();
        trace.finish_at(start + Duration::from_millis(40)).unwrap();
        let result = trace.snapshot_at(start + Duration::from_secs(10)).unwrap();
        assert_eq!(result["total_ms"], 40.0);
        assert_eq!(result["stages"][0]["duration_ms"], 10.0);
        assert_eq!(result["stages"][1]["duration_ms"], 20.0);
        assert_eq!(result["stages"][7]["duration_ms"], 10.0);
        assert_eq!(result["complete"], true);
        assert!(trace.enter(Stage::Send).is_err());
    }
    #[test]
    fn bounded_events_keep_initial_and_latest_segments() {
        let trace = RequestTrace::new().unwrap();
        let start = trace.state.lock().unwrap().start;
        for i in 1..=200 {
            trace
                .enter_at(Stage::Body, start + Duration::from_millis(i))
                .unwrap();
        }
        let result = trace
            .snapshot_at(start + Duration::from_millis(201))
            .unwrap();
        assert_eq!(result["events"].as_array().unwrap().len(), 128);
        assert_eq!(result["events_omitted"], 72);
        assert_eq!(result["events"][0]["stage"], "route");
        assert_eq!(result["events"][127]["end_ms"], 200.0);
    }
    #[test]
    fn reports_bytes_and_status_without_request_payload() {
        let trace = RequestTrace::new().unwrap();
        trace.set_observation(3, 4, Some(200), true).unwrap();
        let result = trace.snapshot().unwrap();
        assert_eq!(result["transport"], "SSE");
        assert_eq!(result["request_bytes"], 3);
        assert_eq!(result["response_bytes"], 4);
        assert_eq!(result["http_status"], 200);
        assert!(result.get("headers").is_none());
        assert!(result.get("body").is_none());
    }
}
