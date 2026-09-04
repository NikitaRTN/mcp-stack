# -*- coding: utf-8 -*-
"""Проверки завершения SSE без запуска рабочего хаба."""

import io
import queue
import unittest
from unittest import mock

from hub import server
from hub.events import Bus, MAX_QUEUE


class EventStreamTests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.handler = object.__new__(server.PanelHandler)
        self.handler.path = "/api/events"
        self.handler.headers = {}
        self.handler.wfile = io.BytesIO()
        self.handler.close_connection = False
        for name in ("send_response", "send_header", "end_headers", "send_json"):
            setattr(self.handler, name, mock.Mock())
        patcher = mock.patch.object(server, "BUS", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server, "LOG")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_overflow_closes_stream_and_keeps_other_subscriber(self):
        healthy, _ = self.bus.subscribe()

        def write(frame):
            if frame == b": connected\n\n":
                for _ in range(MAX_QUEUE + 1):
                    self.bus.publish("state.dirty")
                    healthy.get_nowait()
            else:
                self.fail("Переполненный поток продолжает отправлять данные")

        self.handler.wfile = mock.Mock(write=write)
        self.handler.stream_events()
        self.assertTrue(self.handler.close_connection)
        self.assertEqual(self.bus.listeners, 1)
        event = self.bus.publish("service.state")
        self.assertEqual(healthy.get_nowait(), event)

    def test_failed_headers_release_subscription(self):
        self.handler.end_headers.side_effect = BrokenPipeError
        self.handler.stream_events()
        self.assertEqual(self.bus.listeners, 0)
        self.assertTrue(self.handler.close_connection)

    def test_invalid_cursor_returns_bad_request_without_subscribing(self):
        for cursor in ("abc", "-1"):
            with self.subTest(cursor=cursor):
                self.handler.path = "/api/events?lastSeq=" + cursor
                self.handler.stream_events()
                self.assertEqual(self.handler.send_json.call_args.args[0], 400)
                self.assertEqual(self.bus.listeners, 0)

    def test_unexpected_encoding_error_is_not_a_heartbeat(self):
        self.bus.publish("one")
        self.handler.end_headers.side_effect = lambda: self.bus.publish("two")
        with mock.patch.object(server, "encode", side_effect=ValueError("bad event")):
            with self.assertRaisesRegex(ValueError, "bad event"):
                self.handler.stream_events()
        self.assertNotIn(b": ping", self.handler.wfile.getvalue())
        self.assertEqual(self.bus.listeners, 0)

    def test_idle_stream_sends_heartbeat_and_disconnects_cleanly(self):
        subscriber = mock.Mock()
        subscriber.get.side_effect = [queue.Empty, BrokenPipeError]
        with mock.patch.object(self.bus, "subscribe", return_value=(subscriber, [])), \
                mock.patch.object(self.bus, "is_subscribed", return_value=True, create=True):
            self.handler.stream_events()
        self.assertIn(b": ping\n\n", self.handler.wfile.getvalue())
        self.assertTrue(self.handler.close_connection)


if __name__ == "__main__":
    unittest.main()
