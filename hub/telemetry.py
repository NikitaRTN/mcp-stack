# -*- coding: utf-8 -*-
"""Tool-call telemetry: what was called, when, how long, and did it fail.

SQLite (stdlib) in WAL mode. Rows are written by the inspector on the request
path, so the store must stay cheap: one INSERT when a call starts, one UPDATE
when it finishes.
"""

import json
import sqlite3
import threading
import time

from . import config
from .events import BUS

DB_PATH = config.DATA / "telemetry.db"
ARGS_LIMIT = 4000        # keep a readable preview, never the whole payload
ERROR_LIMIT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL,
    method      TEXT NOT NULL,
    tool        TEXT,
    rpc_id      TEXT,
    session     TEXT,
    started     REAL NOT NULL,
    finished    REAL,
    duration_ms REAL,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT,
    args        TEXT,
    req_bytes   INTEGER DEFAULT 0,
    resp_bytes  INTEGER DEFAULT 0,
    client      TEXT
);
CREATE INDEX IF NOT EXISTS calls_service_started ON calls(service, started DESC);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started DESC);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status);
"""

_lock = threading.RLock()
_conn = None


def connect():
    global _conn
    with _lock:
        if _conn is None:
            config.DATA.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")      # readers never block the proxy
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def _trim(value, limit):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + "\u2026"
    return text


def begin(service, method, tool=None, rpc_id=None, session=None, args=None,
          req_bytes=0, client=None):
    """Record the start of a JSON-RPC call. Returns the row id."""
    conn = connect()
    with _lock:
        cur = conn.execute(
            "INSERT INTO calls (service, method, tool, rpc_id, session, started, status,"
            " args, req_bytes, client) VALUES (?,?,?,?,?,?,'running',?,?,?)",
            (service, method, tool, str(rpc_id) if rpc_id is not None else None, session,
             time.time(), _trim(args, ARGS_LIMIT), int(req_bytes or 0), client),
        )
        conn.commit()
        row_id = cur.lastrowid
    BUS.publish("call.started", {"id": row_id, "service": service, "method": method,
                                 "tool": tool, "session": session})
    return row_id


def finish(row_id, status, error=None, resp_bytes=0, session=None):
    """Close a call row. `status` is ok | error | cancelled | timeout."""
    conn = connect()
    now = time.time()
    with _lock:
        row = conn.execute("SELECT service, method, tool, started, session FROM calls WHERE id=?",
                           (row_id,)).fetchone()
        if row is None:
            return None
        duration = (now - float(row["started"])) * 1000.0
        conn.execute(
            "UPDATE calls SET finished=?, duration_ms=?, status=?, error=?, resp_bytes=?,"
            " session=COALESCE(?, session) WHERE id=?",
            (now, duration, status, _trim(error, ERROR_LIMIT), int(resp_bytes or 0),
             session, row_id),
        )
        conn.commit()
    payload = {
        "id": row_id, "service": row["service"], "method": row["method"],
        "tool": row["tool"], "status": status, "durationMs": round(duration, 1),
        "error": _trim(error, 400), "session": session or row["session"],
        "started": row["started"], "finished": now,
    }
    BUS.publish("call.finished", payload)
    return payload


def abandon_running(reason="Перезапуск хаба во время вызова"):
    """On startup, no call can still be running: close leftovers honestly."""
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE calls SET status='cancelled', error=?, finished=started "
            "WHERE status='running'", (reason,))
        conn.commit()


def list_calls(service=None, limit=200, status=None, tool=None, search=None, since=None):
    conn = connect()
    sql = ["SELECT * FROM calls WHERE 1=1"]
    params = []
    if service:
        sql.append("AND service=?")
        params.append(service)
    if status in ("ok", "error", "running", "cancelled", "timeout"):
        sql.append("AND status=?")
        params.append(status)
    elif status == "failed":
        sql.append("AND status IN ('error','timeout','cancelled')")
    if tool:
        sql.append("AND tool=?")
        params.append(tool)
    if search:
        sql.append("AND (tool LIKE ? OR method LIKE ? OR args LIKE ? OR error LIKE ?)")
        needle = "%%%s%%" % search
        params += [needle, needle, needle, needle]
    if since:
        sql.append("AND started >= ?")
        params.append(float(since))
    sql.append("ORDER BY started DESC LIMIT ?")
    params.append(max(1, min(int(limit or 200), 1000)))
    with _lock:
        rows = conn.execute(" ".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def stats(service=None, window_sec=3600):
    """Counters plus p50/p95 for the dashboard cards."""
    conn = connect()
    since = time.time() - float(window_sec)
    where = "started >= ?"
    params = [since]
    if service:
        where += " AND service=?"
        params.append(service)
    with _lock:
        totals = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(status='ok') AS ok,"
            " SUM(status IN ('error','timeout')) AS failed,"
            " SUM(status='running') AS running,"
            " AVG(duration_ms) AS avg_ms,"
            " MAX(duration_ms) AS max_ms"
            " FROM calls WHERE " + where, params).fetchone()
        durations = [r[0] for r in conn.execute(
            "SELECT duration_ms FROM calls WHERE " + where +
            " AND duration_ms IS NOT NULL ORDER BY duration_ms", params).fetchall()]
        tools = conn.execute(
            "SELECT tool, COUNT(*) AS calls, SUM(status IN ('error','timeout')) AS failed,"
            " AVG(duration_ms) AS avg_ms FROM calls WHERE " + where +
            " AND tool IS NOT NULL GROUP BY tool ORDER BY calls DESC LIMIT 12",
            params).fetchall()
        last = conn.execute(
            "SELECT MAX(started) AS last FROM calls WHERE " +
            ("service=?" if service else "1=1"),
            ([service] if service else [])).fetchone()

    def pct(values, share):
        if not values:
            return None
        index = min(len(values) - 1, int(round((len(values) - 1) * share)))
        return round(values[index], 1)

    total = int(totals["total"] or 0)
    failed = int(totals["failed"] or 0)
    return {
        "windowSec": int(window_sec),
        "total": total,
        "ok": int(totals["ok"] or 0),
        "failed": failed,
        "running": int(totals["running"] or 0),
        "errorRate": round(failed * 100.0 / total, 1) if total else 0.0,
        "avgMs": round(totals["avg_ms"], 1) if totals["avg_ms"] else None,
        "maxMs": round(totals["max_ms"], 1) if totals["max_ms"] else None,
        "p50Ms": pct(durations, 0.50),
        "p95Ms": pct(durations, 0.95),
        "lastCallAt": last["last"],
        "tools": [dict(t) for t in tools],
    }


def buckets(service=None, window_sec=3600, count=40):
    """Compact series for the sparkline: [{t, ok, failed}]."""
    conn = connect()
    count = max(4, min(int(count), 120))
    now = time.time()
    since = now - float(window_sec)
    width = float(window_sec) / count
    where = "started >= ?"
    params = [since]
    if service:
        where += " AND service=?"
        params.append(service)
    with _lock:
        rows = conn.execute(
            "SELECT started, status FROM calls WHERE " + where, params).fetchall()
    series = [{"t": since + i * width, "ok": 0, "failed": 0} for i in range(count)]
    for row in rows:
        index = int((float(row["started"]) - since) / width)
        index = max(0, min(count - 1, index))
        if row["status"] in ("error", "timeout", "cancelled"):
            series[index]["failed"] += 1
        else:
            series[index]["ok"] += 1
    return series


def prune(days=None):
    """Keep the database small; called on startup and after every 500 calls."""
    days = int(days if days is not None else config.load().get("telemetryDays", 14))
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    conn = connect()
    with _lock:
        cur = conn.execute("DELETE FROM calls WHERE started < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def purge(service=None):
    conn = connect()
    with _lock:
        if service:
            conn.execute("DELETE FROM calls WHERE service=?", (service,))
        else:
            conn.execute("DELETE FROM calls")
        conn.commit()
    BUS.publish("calls.purged", {"service": service})
    return True
