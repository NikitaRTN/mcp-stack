# -*- coding: utf-8 -*-
"""Event bus behind the SSE stream.

The panel never polls. Every state change is published here once and fanned out
to all open browser tabs over one long-lived HTTP/2 stream, so a tool call shows
up in the UI a few milliseconds after it finishes.

Why not gRPC: gRPC-web needs protobuf codegen plus a translating proxy, and the
browser still cannot open a raw HTTP/2 gRPC stream. Server-Sent Events give the
same server-push semantics with zero dependencies, automatic reconnect and
replay via Last-Event-ID, which is exactly the shape of this workload
(many small server->client events, rare client->server commands).
"""

import json
import queue
import threading
import time
from collections import deque

MAX_QUEUE = 512          # slow tab: drop it rather than grow memory forever
REPLAY_BUFFER = 400      # events kept for Last-Event-ID reconnects


class Bus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()
        self._history = deque(maxlen=REPLAY_BUFFER)
        self._seq = 0

    # -- publishing ------------------------------------------------------- #

    def publish(self, kind, payload=None):
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": time.time(),
                "kind": kind,
                "data": payload if payload is not None else {},
            }
            self._history.append(event)
            dead = []
            for sub in self._subscribers:
                try:
                    sub.put_nowait(event)
                except queue.Full:
                    dead.append(sub)
            for sub in dead:
                self._subscribers.discard(sub)
        return event

    # -- subscribing ------------------------------------------------------ #

    def subscribe(self, last_seq=0):
        """Return (queue, backlog). Backlog replays what the tab missed."""
        sub = queue.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            backlog = [e for e in self._history if e["seq"] > last_seq] if last_seq else []
            self._subscribers.add(sub)
        return sub, backlog

    def unsubscribe(self, sub):
        with self._lock:
            self._subscribers.discard(sub)

    def is_subscribed(self, sub):
        """Позволяет SSE-обработчику закрыть поток после переполнения очереди."""
        with self._lock:
            return sub in self._subscribers

    @property
    def listeners(self):
        with self._lock:
            return len(self._subscribers)


def encode(event):
    """One SSE frame. `id:` lets the browser resume exactly where it stopped."""
    body = json.dumps(event, ensure_ascii=False, default=str)
    return ("id: %d\nevent: %s\ndata: %s\n\n" % (event["seq"], event["kind"], body)).encode("utf-8")


BUS = Bus()
