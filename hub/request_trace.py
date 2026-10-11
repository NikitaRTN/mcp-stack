"""Bounded monotonic request traces; no headers, bodies or credentials."""
import math
import threading
import time
import uuid

STAGES = (
    ("route", "Маршрутизация", "Маршрут, настройки и проверка доступа"),
    ("body", "Приём запроса", "Чтение HTTP-тела в инспекторе"),
    ("decode", "Разбор RPC", "JSON-RPC и регистрация задачи"),
    ("connect", "Соединение", "Соединение с upstream MCP"),
    ("send", "Передача в MCP", "Отправка HTTP-запроса upstream"),
    ("headers", "Ожидание заголовков", "До HTTP-заголовков upstream"),
    ("receive", "Получение ответа", "Чтение ответа; включает ожидание данных MCP"),
    ("forward", "Отправка ответа", "Запись и flush ответа в сторону клиента"),
)
_NAMES = {key: (title, detail) for key, title, detail in STAGES}
MAX_EVENTS = 128


class RequestTrace:
    """One HTTP request; batch RPC rows can snapshot the same trace separately."""
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.started_wall = time.time()
        self.started = time.perf_counter()
        self._lock = threading.RLock()
        self._segments = []
        self._totals = {key: 0.0 for key, *_ in STAGES}
        self._first = {}
        self._last = {}
        self._phase = "route"
        self._phase_start = self.started
        self._first["route"] = 0.0
        self._omitted = 0
        self.bytes_received = 0
        self.bytes_sent = 0
        self.transport = None
        self.http_status = None

    def now(self):
        return time.perf_counter()

    def enter(self, key):
        if key not in _NAMES:
            raise ValueError("Unknown trace stage")
        at = self.now()
        with self._lock:
            self._close_phase(at)
            self._phase = key
            self._phase_start = at
            self._first.setdefault(key, (at - self.started) * 1000)

    def _close_phase(self, at):
        start = max(0.0, (self._phase_start - self.started) * 1000)
        end = max(start, (at - self.started) * 1000)
        self._totals[self._phase] += end - start
        self._last[self._phase] = end
        segment = {"stage": self._phase, "start_ms": start, "end_ms": end}
        if len(self._segments) < MAX_EVENTS:
            self._segments.append(segment)
        else:
            self._segments.pop(MAX_EVENTS // 2)
            self._segments.append(segment)
            self._omitted += 1

    def snapshot(self, at=None, complete=False):
        at = self.now() if at is None else at
        with self._lock:
            total = max(0.0, (at - self.started) * 1000)
            totals = dict(self._totals)
            first, last = dict(self._first), dict(self._last)
            events = [dict(e) for e in self._segments if e["start_ms"] <= total]
            # A supplied completion boundary is always the most recent flush;
            # no later phase can be attributed to an already sent reply.
            current_start = max(0.0, (self._phase_start - self.started) * 1000)
            if at >= self._phase_start:
                duration = max(0.0, total - current_start)
                totals[self._phase] += duration
                last[self._phase] = total
                if len(events) < MAX_EVENTS:
                    events.append({"stage": self._phase, "start_ms": current_start,
                                   "end_ms": total})
            stages = []
            for key, title, detail in STAGES:
                measured = key in first and first[key] <= total
                stages.append({
                    "id": key, "label": title, "detail": detail,
                    "duration_ms": round(totals[key], 4) if measured else None,
                    "start_ms": round(first[key], 4) if measured else None,
                    "end_ms": round(last.get(key, total), 4) if measured else None,
                    "state": ("active" if not complete and key == self._phase
                              else "done" if measured else "pending"),
                })
            return {
                "version": 1, "id": self.id, "scope": "inspector",
                "boundary": "Приём HTTP в инспекторе → запись и flush ответа",
                "started": self.started_wall, "total_ms": round(total, 4),
                "complete": bool(complete), "phase": None if complete else self._phase,
                "stages": stages, "events": events,
                "events_omitted": self._omitted,
                "transport": self.transport, "http_status": self.http_status,
                "request_bytes": self.bytes_received, "response_bytes": self.bytes_sent,
                "outside_scope": [
                    "Клиент → Caddy → инспектор",
                    "Доставка после flush и отображение ответа в Notion",
                    "Отдельные внутренние операции инструмента MCP",
                ],
            }


def summary(trace):
    if not isinstance(trace, dict):
        return None
    return {key: trace.get(key) for key in
            ("version", "id", "scope", "total_ms", "complete", "phase")}