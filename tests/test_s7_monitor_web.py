"""Tests for the stdlib HTTP + SSE web server."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from s7pymon.protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult
from s7pymon.engine import MonitorEngine, ReadGroup, WriteMode
from s7pymon.variable import S7Area, S7Variable
from s7pymon.web import Broadcaster, S7WebServer


# --------------------------------------------------------------------------- fakes
class FakeConnection(Connection):
    def __init__(self, buffers):
        self._config = ConnectionConfig(address="10.0.0.9")
        self._state = ConnectionState.CONNECTED
        self._buffers = buffers
        self.writes = []

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def state(self) -> ConnectionState:
        return self._state

    @state.setter
    def state(self, value: ConnectionState) -> None:
        self._state = value

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        area, db = self._parse(source)
        buf = self._buffers.get((area, db), bytearray(64))
        return ReadResult(data=bytearray(buf[offset:offset + size]),
                          source=source, start=offset, size=size)

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        self.writes.append((source, offset, bytes(data)))
        area, db = self._parse(source)
        buf = self._buffers.setdefault((area, db), bytearray(64))
        buf[offset:offset + len(data)] = data

    @staticmethod
    def _parse(source: DataSource) -> tuple:
        if source.value.startswith("DB"):
            return S7Area.DB, int(source.value[2:])
        return S7Area(source.value), 0


@pytest.fixture
def server():
    buffers = {(S7Area.DB, 210): bytearray([0x2A] + [0] * 15)}
    conn = FakeConnection(buffers)
    engine = MonitorEngine(
        conn,
        [S7Variable.parse("DB210.Byte0", label="answer")],
        [ReadGroup(DataSource.s7_db(210), start=0, size=16)],
        poll_interval=0.05,
        write_mode=WriteMode.DISABLED,
    )
    srv = S7WebServer(engine, host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.start()  # poller
    yield srv, engine, conn
    srv.shutdown()
    t.join(timeout=2)


def _get(url):
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status, json.loads(resp.read())


def _post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestBroadcaster:
    def test_publish_to_subscribers(self):
        b = Broadcaster()
        q = b.subscribe()
        b.publish("hello")
        assert q.get_nowait() == "hello"

    def test_replays_last_to_new_subscriber(self):
        b = Broadcaster()
        b.publish("first")
        q = b.subscribe()
        assert q.get_nowait() == "first"

    def test_unsubscribe(self):
        b = Broadcaster()
        q = b.subscribe()
        b.unsubscribe(q)
        assert b.subscriber_count == 0
        b.publish("x")
        assert q.empty()

    def test_drops_oldest_when_full(self):
        b = Broadcaster(maxsize=1)
        q = b.subscribe()
        b.publish("a")
        b.publish("b")  # should evict "a"
        assert q.get_nowait() == "b"


class TestHttpEndpoints:
    def test_index_served(self, server):
        srv, _, _ = server
        with urllib.request.urlopen(srv.url, timeout=2) as resp:
            assert resp.status == 200
            assert b"<" in resp.read()

    def test_static_asset(self, server):
        srv, _, _ = server
        with urllib.request.urlopen(srv.url + "static/app.js", timeout=2) as resp:
            assert resp.status == 200
            assert "javascript" in resp.headers["Content-Type"]

    def test_static_traversal_blocked(self, server):
        srv, _, _ = server
        # Encoded characters never match the safe-name guard.
        req = urllib.request.Request(srv.url + "static/..%2fweb.py")
        try:
            urllib.request.urlopen(req, timeout=2)
            assert False, "expected error"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404)

    def test_api_state(self, server):
        srv, _, _ = server
        code, body = _get(srv.url + "api/state")
        assert code == 200
        assert body["address"].startswith("10.0.0.9")
        assert body["variables"][0]["spec"] == "DB210.Byte0"
        assert body["variables"][0]["label"] == "answer"

    def test_not_found(self, server):
        srv, _, _ = server
        req = urllib.request.Request(srv.url + "nope")
        try:
            urllib.request.urlopen(req, timeout=2)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestControl:
    def test_pause_resume(self, server):
        srv, engine, _ = server
        code, body = _post(srv.url + "api/control", {"action": "pause"})
        assert code == 200
        assert body["status"]["paused"] is True
        assert engine.paused is True
        code, body = _post(srv.url + "api/control", {"action": "resume"})
        assert body["status"]["paused"] is False

    def test_write_mode_cycle(self, server):
        srv, engine, _ = server
        _post(srv.url + "api/control", {"action": "write_mode"})
        assert engine.write_mode == WriteMode.CONFIRM
        code, body = _post(srv.url + "api/control", {"action": "write_mode", "mode": "allowed"})
        assert engine.write_mode == WriteMode.ALLOWED
        assert body["status"]["write_mode"] == "allowed"

    def test_unknown_action(self, server):
        srv, _, _ = server
        code, body = _post(srv.url + "api/control", {"action": "boom"})
        assert code == 400
        assert "unknown action" in body["error"]


class TestWrite:
    def test_write_blocked_when_disabled(self, server):
        srv, _, _ = server
        code, body = _post(srv.url + "api/write", {"spec": "DB210.Byte0", "value": "5"})
        assert code == 403
        assert "disabled" in body["error"].lower()

    def test_write_allowed(self, server):
        srv, engine, conn = server
        engine.write_mode = WriteMode.ALLOWED
        code, body = _post(srv.url + "api/write", {"spec": "DB210.Byte0", "value": "0x2A"})
        assert code == 200
        assert body["ok"] is True
        assert conn.writes[-1] == (DataSource.s7_db(210), 0, b"\x2a")

    def test_write_raw(self, server):
        srv, engine, conn = server
        engine.write_mode = WriteMode.ALLOWED
        code, body = _post(srv.url + "api/write",
                           {"raw": {"db": 210, "offset": 1, "bytes": "FF 01"}})
        assert code == 200
        assert conn.writes[-1] == (DataSource.s7_db(210), 1, b"\xff\x01")

    def test_write_bad_value(self, server):
        srv, engine, _ = server
        engine.write_mode = WriteMode.ALLOWED
        code, body = _post(srv.url + "api/write", {"spec": "DB210.Byte0", "value": "notnum"})
        assert code == 400


class TestStream:
    def test_sse_initial_event(self, server):
        srv, _, _ = server
        with urllib.request.urlopen(srv.url + "api/stream", timeout=2) as resp:
            assert resp.headers["Content-Type"] == "text/event-stream"
            line = resp.readline().decode()
            assert line.startswith("data: ")
            payload = json.loads(line[len("data: "):])
            assert "type" in payload
            assert payload["connection_state"] == "connected"
