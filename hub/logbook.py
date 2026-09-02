# -*- coding: utf-8 -*-
"""Единый структурный журнал хаба.

Зачем модуль нужен:
  * раньше ошибки уходили в консоль отсоединённого процесса и терялись;
  * теперь каждый RPC-вызов, HTTP 5xx, действие супервизора и даже исключение
    в браузере попадают в одно место: кольцевой буфер в памяти, JSONL-файл с
    ротацией и SSE-поток для панели;
  * панель читает журнал методом ``logs.tail`` и показывает его во вкладке
    «Логи» с фильтрами по уровню, источнику и тексту.

Правила:
  * только стандартная библиотека, потокобезопасно, Python 3.8+;
  * запись в журнал не может уронить операцию, которую она описывает —
    любые внутренние сбои проглатываются;
  * секреты (токены, пароли, cookie) вычищаются до записи.
"""

import json
import logging
import os
import threading
import time
import traceback
import uuid
from collections import deque

from . import config
from .events import BUS

LEVELS = ("debug", "info", "warn", "error")
LEVEL_VALUE = {"debug": 10, "info": 20, "warn": 30, "error": 40}

RING_SIZE = 3000              # записей в памяти: мгновенный tail без чтения файла
FILE_LIMIT = 2 * 1024 * 1024  # ротация JSONL
KEEP_ROTATIONS = 3
MAX_MESSAGE = 2000
MAX_FIELD = 800
MAX_TRACEBACK = 4000
PUSH_BUDGET = 30              # событий в секунду в браузер; остальное суммируется

SECRET_HINTS = ("token", "password", "secret", "authorization", "cookie",
                "apikey", "api_key", "pass", "credential")


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "\u2026"


def scrub(value, _depth=0):
    """Копия payload без секретов. В журнале не должно быть токена."""
    if _depth > 4:
        return "\u2026"
    if isinstance(value, dict):
        clean = {}
        for key, item in list(value.items())[:40]:
            name = str(key)
            if any(hint in name.lower() for hint in SECRET_HINTS):
                clean[name] = "***"
            else:
                clean[name] = scrub(item, _depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in list(value)[:20]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return _clip(str(value), MAX_FIELD)


class Logbook:
    """Кольцевой буфер + JSONL-файл + SSE-публикация."""

    def __init__(self, name="hub", ring=RING_SIZE):
        self._name = name
        self._lock = threading.RLock()
        self._ring = deque(maxlen=ring)
        self._seq = 0
        self._counts = dict((level, 0) for level in LEVELS)
        self._sources = {}
        self._handle = None
        self._path = None
        self._window_started = 0.0
        self._window_count = 0
        self._suppressed = 0

    # -- запись ----------------------------------------------------------- #

    def record(self, level, source, message, event="", **fields):
        """Основная точка входа. Никогда не бросает исключение наружу."""
        try:
            return self._record(level, source, message, event, fields)
        except Exception:                                    # noqa: BLE001
            return None

    def debug(self, source, message, event="", **fields):
        return self.record("debug", source, message, event, **fields)

    def info(self, source, message, event="", **fields):
        return self.record("info", source, message, event, **fields)

    def warn(self, source, message, event="", **fields):
        return self.record("warn", source, message, event, **fields)

    def error(self, source, message, event="", **fields):
        return self.record("error", source, message, event, **fields)

    def exception(self, source, message, exc=None, event="exception", **fields):
        """Ошибка с трассировкой. Возвращает запись с errorId для показа в UI."""
        if exc is not None:
            fields["error"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                fields["traceback"] = _clip("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)), MAX_TRACEBACK)
            except Exception:                                # noqa: BLE001
                fields["traceback"] = _clip(traceback.format_exc(), MAX_TRACEBACK)
        return self.record("error", source, message, event, **fields)

    def _record(self, level, source, message, event, fields):
        level = level if level in LEVEL_VALUE else "info"
        source = str(source or "hub")[:40]
        entry = {
            "ts": time.time(),
            "level": level,
            "source": source,
            "event": str(event or "")[:60],
            "message": _clip(message if message is not None else "", MAX_MESSAGE),
            "fields": scrub(fields) if fields else {},
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            if level == "error":
                entry["errorId"] = "E-" + uuid.uuid4().hex[:6].upper()
            self._ring.append(entry)
            self._counts[level] = self._counts.get(level, 0) + 1
            self._sources[source] = self._sources.get(source, 0) + 1
            self._write(entry)
            self._publish(entry)
        return entry

    # -- файл ------------------------------------------------------------- #

    def path(self):
        return config.LOGS / ("%s.jsonl" % self._name)

    def _file(self):
        if self._handle is None:
            config.LOGS.mkdir(parents=True, exist_ok=True)
            self._path = self.path()
            self._handle = open(str(self._path), "a", encoding="utf-8")
        return self._handle

    def _write(self, entry):
        try:
            handle = self._file()
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            if handle.tell() > FILE_LIMIT:
                self._rotate()
        except Exception:                                    # noqa: BLE001
            self._handle = None                              # попробуем в следующий раз

    def _rotate(self):
        try:
            if self._handle:
                self._handle.close()
        except Exception:                                    # noqa: BLE001
            pass
        self._handle = None
        base = str(self.path())
        try:
            oldest = "%s.%d" % (base, KEEP_ROTATIONS)
            if os.path.exists(oldest):
                os.remove(oldest)
            for index in range(KEEP_ROTATIONS - 1, 0, -1):
                src, dst = "%s.%d" % (base, index), "%s.%d" % (base, index + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(base):
                os.replace(base, base + ".1")
        except Exception:                                    # noqa: BLE001
            pass

    # -- SSE -------------------------------------------------------------- #

    def _publish(self, entry):
        now = entry["ts"]
        if now - self._window_started >= 1.0:
            self._window_started = now
            self._window_count = 0
            if self._suppressed:
                dropped, self._suppressed = self._suppressed, 0
                BUS.publish("log.overflow", {"dropped": dropped, "lastSeq": self._seq})
        if entry["level"] in ("warn", "error") or self._window_count < PUSH_BUDGET:
            self._window_count += 1
            BUS.publish("log.entry", entry)
        else:
            self._suppressed += 1

    # -- чтение ----------------------------------------------------------- #

    def tail(self, limit=200, level=None, source=None, search=None,
             since_seq=None, event=None):
        """Записи от старых к новым — так удобно дописывать в конец списка."""
        floor = LEVEL_VALUE.get(str(level or "").lower(), 0)
        needle = (search or "").strip().lower()
        want_source = (source or "").strip()
        since = int(since_seq or 0)
        limit = max(1, min(int(limit or 200), 2000))
        with self._lock:
            rows = list(self._ring)
            counts = dict(self._counts)
            sources = sorted(self._sources.items(), key=lambda kv: -kv[1])
            last_seq = self._seq
        picked = []
        for entry in rows:
            if entry["seq"] <= since:
                continue
            if LEVEL_VALUE.get(entry["level"], 0) < floor:
                continue
            if want_source and entry["source"] != want_source:
                continue
            if event and entry.get("event") != event:
                continue
            if needle:
                blob = "%s %s %s %s" % (entry["message"], entry["source"],
                                        entry.get("event") or "",
                                        json.dumps(entry.get("fields") or {},
                                                   ensure_ascii=False, default=str))
                if needle not in blob.lower():
                    continue
            picked.append(entry)
        truncated = max(0, len(picked) - limit)
        return {
            "entries": picked[-limit:],
            "lastSeq": last_seq,
            "truncated": truncated,
            "counts": counts,
            "sources": [{"name": name, "count": count} for name, count in sources],
            "path": str(self.path()),
            "buffered": len(rows),
        }

    def snapshot(self):
        with self._lock:
            size = 0
            try:
                size = self.path().stat().st_size
            except Exception:                                # noqa: BLE001
                pass
            return {
                "lastSeq": self._seq,
                "counts": dict(self._counts),
                "buffered": len(self._ring),
                "path": str(self.path()),
                "bytes": size,
                "suppressed": self._suppressed,
            }

    def clear(self):
        with self._lock:
            self._ring.clear()
            self._counts = dict((level, 0) for level in LEVELS)
            self._sources = {}
        BUS.publish("log.cleared", {})
        return True


# --------------------------------------------------------------------------- #
#  Перехват стандартного logging                                              #
# --------------------------------------------------------------------------- #

class _StdlibBridge(logging.Handler):
    _MAP = {logging.DEBUG: "debug", logging.INFO: "info",
            logging.WARNING: "warn", logging.ERROR: "error",
            logging.CRITICAL: "error"}

    def emit(self, record):
        try:
            level = self._MAP.get(record.levelno, "info")
            fields = {"logger": record.name}
            if record.exc_info:
                fields["traceback"] = _clip(
                    "".join(traceback.format_exception(*record.exc_info)), MAX_TRACEBACK)
            LOG.record(level, "python", record.getMessage(), event="stdlib", **fields)
        except Exception:                                    # noqa: BLE001
            pass


LOG = Logbook()


def capture_stdlib(level=logging.INFO):
    """Проброс стандартного logging в журнал панели (один раз при старте)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, _StdlibBridge):
            return False
    bridge = _StdlibBridge()
    bridge.setLevel(level)
    root.addHandler(bridge)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return True


# Короткие обёртки, чтобы в коде читалось как logbook.info(...)
def debug(source, message, event="", **fields):
    return LOG.debug(source, message, event, **fields)


def info(source, message, event="", **fields):
    return LOG.info(source, message, event, **fields)


def warn(source, message, event="", **fields):
    return LOG.warn(source, message, event, **fields)


def error(source, message, event="", **fields):
    return LOG.error(source, message, event, **fields)


def exception(source, message, exc=None, event="exception", **fields):
    return LOG.exception(source, message, exc, event, **fields)


def tail(**kwargs):
    return LOG.tail(**kwargs)
