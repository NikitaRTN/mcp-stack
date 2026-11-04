//! SQLite-backed call history, compatible with the existing Python schema.
//! This is not yet wired to an HTTP inspector or the panel.
use crate::events::EventBus;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS calls (
 id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, method TEXT NOT NULL,
 tool TEXT, rpc_id TEXT, session TEXT, started REAL NOT NULL, finished REAL,
 duration_ms REAL, status TEXT NOT NULL DEFAULT 'running', error TEXT, args TEXT,
 req_bytes INTEGER DEFAULT 0, resp_bytes INTEGER DEFAULT 0, client TEXT
);
CREATE INDEX IF NOT EXISTS calls_service_started ON calls(service, started DESC);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started DESC);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status);
"#;
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn trim(value: &str, maximum: usize) -> String {
    let mut chars = value.chars();
    let mut out: String = chars.by_ref().take(maximum).collect();
    if chars.next().is_some() {
        out.push('…');
    }
    out
}
fn json_preview(value: Option<&Value>, max: usize) -> Result<Option<String>, String> {
    value
        .map(|v| {
            serde_json::to_string(v)
                .map(|text| trim(&text, max))
                .map_err(|_| "cannot encode metadata".into())
        })
        .transpose()
}

pub struct CallStart<'a> {
    pub service: &'a str,
    pub method: &'a str,
    pub tool: Option<&'a str>,
    pub rpc_id: Option<&'a str>,
    pub session: Option<&'a str>,
    pub args: Option<&'a Value>,
    pub request_bytes: u64,
    pub client: Option<&'a str>,
}
#[derive(Debug)]
pub struct Call {
    pub id: i64,
    pub service: String,
    pub method: String,
    pub tool: Option<String>,
    pub started: f64,
    pub finished: Option<f64>,
    pub duration_ms: Option<f64>,
    pub status: String,
    pub error: Option<String>,
    pub args: Option<String>,
    pub trace: Option<String>,
}
pub struct TelemetryStore {
    conn: Mutex<Connection>,
    bus: EventBus,
}
impl TelemetryStore {
    /// Opens only the explicitly supplied file. Callers must back up a legacy
    /// database and avoid running a second controller against it during migration.
    pub fn open(path: &Path, bus: EventBus) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|_| "cannot open telemetry database")?;
        Self::from_connection(conn, bus)
    }
    fn from_connection(mut conn: Connection, bus: EventBus) -> Result<Self, String> {
        conn.busy_timeout(Duration::from_secs(5))
            .map_err(|_| "cannot configure telemetry database")?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            .map_err(|_| "cannot configure telemetry database")?;
        let tx = conn
            .transaction()
            .map_err(|_| "cannot start telemetry migration")?;
        tx.execute_batch(SCHEMA)
            .map_err(|_| "cannot initialize telemetry schema")?;
        let has_trace = {
            let mut stmt = tx
                .prepare("PRAGMA table_info(calls)")
                .map_err(|_| "cannot inspect telemetry schema")?;
            let columns = stmt
                .query_map([], |row| row.get::<_, String>(1))
                .map_err(|_| "cannot inspect telemetry schema")?;
            let names = columns
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| "cannot inspect telemetry schema")?;
            names.iter().any(|x| x == "trace")
        };
        if !has_trace {
            tx.execute("ALTER TABLE calls ADD COLUMN trace TEXT", [])
                .map_err(|_| "cannot migrate telemetry schema")?;
        }
        tx.commit()
            .map_err(|_| "cannot commit telemetry migration")?;
        Ok(Self {
            conn: Mutex::new(conn),
            bus,
        })
    }
    pub fn begin(&self, call: CallStart<'_>) -> Result<i64, String> {
        if call.service.is_empty() || call.method.is_empty() {
            return Err("missing call identity".into());
        }
        let started = now();
        let args = json_preview(call.args, 4000)?;
        let req_bytes = i64::try_from(call.request_bytes).map_err(|_| "invalid byte count")?;
        let id = {
            let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
            conn.execute("INSERT INTO calls(service,method,tool,rpc_id,session,started,status,args,req_bytes,client) VALUES(?,?,?,?,?,?,'running',?,?,?)",
                params![trim(call.service,128),trim(call.method,256),call.tool.map(|v|trim(v,512)),call.rpc_id.map(|v|trim(v,512)),call.session.map(|v|trim(v,512)),started,args,req_bytes,call.client.map(|v|trim(v,512))]).map_err(|_| "cannot write call")?;
            conn.last_insert_rowid()
        };
        let _=self.bus.publish("call.started",json!({"id":id,"service":call.service,"method":call.method,"tool":call.tool,"started":started}));
        Ok(id)
    }
    /// Finish exactly once; unknown or already closed rows return false.
    pub fn finish(
        &self,
        id: i64,
        status: &str,
        error: Option<&str>,
        response_bytes: u64,
        trace: Option<&Value>,
    ) -> Result<bool, String> {
        if !matches!(status, "ok" | "error" | "cancelled" | "timeout") {
            return Err("invalid call status".into());
        }
        let bytes = i64::try_from(response_bytes).map_err(|_| "invalid byte count")?;
        let trace = trace
            .map(|v| serde_json::to_string(v).map_err(|_| "invalid trace"))
            .transpose()?;
        if trace.as_ref().is_some_and(|s| s.len() > 256_000) {
            return Err("trace too large".into());
        }
        let finished = now();
        let changed = {
            let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
            conn.execute("UPDATE calls SET finished=?, duration_ms=MAX(0,(?-started)*1000),status=?,error=?,resp_bytes=?,trace=? WHERE id=? AND status='running'",
                params![finished,finished,status,error.map(|v|trim(v,2000)),bytes,trace,id]).map_err(|_| "cannot finish call")?
        };
        if changed > 0 {
            let _ = self.bus.publish(
                "call.finished",
                json!({"id":id,"status":status,"finished":finished}),
            );
        }
        Ok(changed > 0)
    }
    pub fn get(&self, id: i64) -> Result<Option<Call>, String> {
        let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
        conn.query_row("SELECT id,service,method,tool,started,finished,duration_ms,status,error,args,trace FROM calls WHERE id=?",[id],row_to_call)
            .optional().map_err(|_| "cannot read call".into())
    }
    pub fn list(&self, service: Option<&str>, limit: usize) -> Result<Vec<Call>, String> {
        let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
        let mut stmt=conn.prepare("SELECT id,service,method,tool,started,finished,duration_ms,status,error,args,trace FROM calls WHERE (?1 IS NULL OR service=?1) ORDER BY started DESC,id DESC LIMIT ?2").map_err(|_| "cannot read calls")?;
        let rows = stmt
            .query_map(params![service, limit.clamp(1, 1000) as i64], row_to_call)
            .map_err(|_| "cannot read calls")?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|_| "cannot read calls".into())
    }
    pub fn abandon_running(&self) -> Result<usize, String> {
        let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
        conn.execute("UPDATE calls SET status='cancelled',error='Hub restarted during call',finished=started WHERE status='running'",[]).map_err(|_| "cannot reconcile calls".into())
    }
    pub fn stats(&self, service: Option<&str>, window_sec: u32) -> Result<Value, String> {
        if window_sec == 0 {
            return Err("invalid statistics window".into());
        }
        let since = now() - f64::from(window_sec);
        let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
        let totals: (i64,i64,i64,i64,Option<f64>,Option<f64>) = conn.query_row(
            "SELECT COUNT(*),COALESCE(SUM(status='ok'),0),COALESCE(SUM(status IN ('error','timeout')),0),COALESCE(SUM(status='running'),0),AVG(duration_ms),MAX(duration_ms) FROM calls WHERE started>=?1 AND (?2 IS NULL OR service=?2)",
            params![since,service],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_| "cannot read statistics")?;
        let mut statement=conn.prepare("SELECT duration_ms FROM calls WHERE started>=?1 AND (?2 IS NULL OR service=?2) AND duration_ms IS NOT NULL ORDER BY duration_ms").map_err(|_| "cannot read statistics")?;
        let durations = statement
            .query_map(params![since, service], |r| r.get::<_, f64>(0))
            .map_err(|_| "cannot read statistics")?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| "cannot read statistics")?;
        let percentile = |share: f64| -> Option<f64> {
            if durations.is_empty() {
                return None;
            }
            let index = (((durations.len() - 1) as f64) * share).round() as usize;
            Some((durations[index] * 10.0).round() / 10.0)
        };
        let last: Option<f64> = conn
            .query_row(
                "SELECT MAX(started) FROM calls WHERE (?1 IS NULL OR service=?1)",
                params![service],
                |r| r.get(0),
            )
            .map_err(|_| "cannot read statistics")?;
        let mut statement=conn.prepare("SELECT tool,COUNT(*),COALESCE(SUM(status IN ('error','timeout')),0),AVG(duration_ms) FROM calls WHERE started>=?1 AND (?2 IS NULL OR service=?2) AND tool IS NOT NULL GROUP BY tool ORDER BY COUNT(*) DESC LIMIT 12").map_err(|_| "cannot read statistics")?;
        let tools=statement.query_map(params![since,service],|r|Ok(json!({"tool":r.get::<_,String>(0)?,"calls":r.get::<_,i64>(1)?,"failed":r.get::<_,i64>(2)?,"avg_ms":r.get::<_,Option<f64>>(3)?}))).map_err(|_| "cannot read statistics")?.collect::<Result<Vec<_>,_>>().map_err(|_| "cannot read statistics")?;
        let rate = if totals.0 == 0 {
            0.0
        } else {
            ((totals.2 as f64) * 1000.0 / (totals.0 as f64)).round() / 10.0
        };
        Ok(
            json!({"windowSec":window_sec,"total":totals.0,"ok":totals.1,"failed":totals.2,"running":totals.3,"errorRate":rate,
            "avgMs":totals.4.map(|n|(n*10.0).round()/10.0),"maxMs":totals.5.map(|n|(n*10.0).round()/10.0),"p50Ms":percentile(0.50),"p95Ms":percentile(0.95),"lastCallAt":last,"tools":tools}),
        )
    }
    /// Deletes only calls older than the requested retention window. A zero
    /// value disables pruning, matching the legacy application.
    pub fn prune(&self, days: u32) -> Result<usize, String> {
        if days == 0 {
            return Ok(0);
        }
        let cutoff = now() - f64::from(days) * 86400.0;
        let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
        conn.execute("DELETE FROM calls WHERE started<?", [cutoff])
            .map_err(|_| "cannot prune calls".into())
    }
    pub fn purge(&self, service: Option<&str>) -> Result<usize, String> {
        let changed = {
            let conn = self.conn.lock().map_err(|_| "telemetry unavailable")?;
            conn.execute(
                "DELETE FROM calls WHERE (?1 IS NULL OR service=?1)",
                params![service],
            )
            .map_err(|_| "cannot purge calls")?
        };
        let _ = self.bus.publish("calls.purged", json!({"service":service}));
        Ok(changed)
    }
}
fn row_to_call(row: &rusqlite::Row<'_>) -> rusqlite::Result<Call> {
    Ok(Call {
        id: row.get(0)?,
        service: row.get(1)?,
        method: row.get(2)?,
        tool: row.get(3)?,
        started: row.get(4)?,
        finished: row.get(5)?,
        duration_ms: row.get(6)?,
        status: row.get(7)?,
        error: row.get(8)?,
        args: row.get(9)?,
        trace: row.get(10)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn store() -> TelemetryStore {
        TelemetryStore::from_connection(Connection::open_in_memory().unwrap(), EventBus::default())
            .unwrap()
    }
    fn start<'a>(service: &'a str, args: &'a Value) -> CallStart<'a> {
        CallStart {
            service,
            method: "tools/call",
            tool: Some("echo"),
            rpc_id: Some("1"),
            session: None,
            args: Some(args),
            request_bytes: 10,
            client: None,
        }
    }
    #[test]
    fn begins_finishes_once_and_filters_calls() {
        let db = store();
        let args = json!({"text":"hello"});
        let id = db.begin(start("alpha", &args)).unwrap();
        assert_eq!(db.get(id).unwrap().unwrap().status, "running");
        assert!(db
            .finish(id, "ok", None, 22, Some(&json!({"version":1})))
            .unwrap());
        assert!(!db.finish(id, "error", None, 0, None).unwrap());
        let row = db.get(id).unwrap().unwrap();
        assert_eq!(row.status, "ok");
        assert!(row.duration_ms.unwrap() >= 0.0);
        assert!(row.trace.unwrap().contains("version"));
        assert_eq!(db.list(Some("alpha"), 100).unwrap().len(), 1);
        assert!(db.list(Some("other"), 100).unwrap().is_empty());
        assert!(db.finish(id, "not-a-status", None, 0, None).is_err());
    }
    #[test]
    fn migrates_old_schema_without_losing_history() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(SCHEMA).unwrap();
        conn.execute(
            "INSERT INTO calls(service,method,started,status) VALUES('legacy','ping',123,'ok')",
            [],
        )
        .unwrap();
        let db = TelemetryStore::from_connection(conn, EventBus::default()).unwrap();
        let rows = db.list(None, 200).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].service, "legacy");
        assert!(rows[0].trace.is_none());
    }
    #[test]
    fn bounds_previews_and_cancels_abandoned_calls() {
        let db = store();
        let args = json!({"text":"аб".repeat(3000)});
        let id = db.begin(start("alpha", &args)).unwrap();
        assert!(db.get(id).unwrap().unwrap().args.unwrap().chars().count() <= 4001);
        assert_eq!(db.abandon_running().unwrap(), 1);
        assert_eq!(db.get(id).unwrap().unwrap().status, "cancelled");
    }
    #[test]
    fn emits_events_only_after_the_write_without_arguments() {
        let bus = EventBus::default();
        let (sub, _) = bus.subscribe(0).unwrap();
        let db =
            TelemetryStore::from_connection(Connection::open_in_memory().unwrap(), bus).unwrap();
        let args = json!({"secret":"never-in-events"});
        let id = db.begin(start("alpha", &args)).unwrap();
        let event = sub.receiver.try_recv().unwrap();
        assert_eq!(event.data["id"], id);
        assert!(!event.data.to_string().contains("never-in-events"));
        assert!(db.finish(id, "ok", None, 0, None).unwrap());
        assert_eq!(sub.receiver.try_recv().unwrap().kind, "call.finished");
    }
    #[test]
    fn reports_statistics_and_purges_only_the_selected_service() {
        let db = store();
        let args = json!({});
        let ok = db.begin(start("one", &args)).unwrap();
        let fail = db.begin(start("one", &args)).unwrap();
        let other = db.begin(start("two", &args)).unwrap();
        db.finish(ok, "ok", None, 0, None).unwrap();
        db.finish(fail, "timeout", None, 0, None).unwrap();
        let stats = db.stats(Some("one"), 3600).unwrap();
        assert_eq!(stats["total"], 2);
        assert_eq!(stats["ok"], 1);
        assert_eq!(stats["failed"], 1);
        assert_eq!(stats["errorRate"], 50.0);
        assert!(stats["p95Ms"].is_number());
        assert_eq!(db.stats(Some("two"), 3600).unwrap()["running"], 1);
        assert_eq!(db.prune(0).unwrap(), 0);
        assert_eq!(db.purge(Some("one")).unwrap(), 2);
        assert!(db.get(other).unwrap().is_some());
        assert!(db.get(ok).unwrap().is_none());
        assert!(db.stats(None, 0).is_err());
    }
}
