"""Tests for the headless MonitorEngine."""

from unittest.mock import MagicMock

import pytest

from s7pymon.connection import ConnectionConfig, ConnectionState, ReadResult, S7Connection
from s7pymon.engine import (
    MonitorEngine,
    Snapshot,
    WriteBlockedError,
    WriteMode,
    area_label,
    format_hex_dump,
    group_key,
)
from s7pymon.variable import S7Area, S7Type, S7Variable


class FakeConnection:
    """Minimal S7Connection stand-in driven by a dict of area buffers."""

    def __init__(self, buffers: dict[tuple, bytearray] | None = None):
        self.config = ConnectionConfig(address="10.0.0.5")
        self.state = ConnectionState.CONNECTED
        self._buffers: dict[tuple, bytearray] = buffers or {}
        self.writes: list[tuple] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.read_error: Exception | None = None

    @property
    def connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    def connect(self) -> None:
        self.connect_calls += 1
        self.state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.state = ConnectionState.DISCONNECTED

    def area_read(self, area, start, size, db=0):
        if self.read_error is not None:
            self.state = ConnectionState.ERROR
            raise self.read_error
        buf = self._buffers.get((area, db), bytearray(64))
        return ReadResult(data=bytearray(buf[start : start + size]), area=area, db=db,
                          start=start, size=size)

    def area_write(self, area, start, data, db=0):
        self.writes.append((area, start, bytes(data), db))
        buf = self._buffers.setdefault((area, db), bytearray(64))
        buf[start : start + len(data)] = data


class Grp:
    """Lightweight ReadGroup stand-in (engine only needs these attributes)."""

    def __init__(self, area, db, start, size, label=None):
        self.area = area
        self.db = db
        self.start = start
        self.size = size
        self.label = label or area_label(area, db)

    @property
    def key(self):
        return group_key(self.area, self.db)


def make_engine(buffers, variables, **kw):
    conn = FakeConnection(buffers)
    groups = kw.pop("groups", None)
    if groups is None:
        # one DB group covering 0..16 by default
        groups = [Grp(S7Area.DB, 210, 0, 16)]
    return MonitorEngine(conn, variables, groups, **kw), conn


class TestHelpers:
    def test_area_label_db(self):
        assert area_label(S7Area.DB, 210) == "DB210"

    def test_area_label_non_db(self):
        assert area_label(S7Area.EB, 0) == "EB"

    def test_group_key_matches_label(self):
        assert group_key(S7Area.DB, 5) == "DB5"
        assert group_key(S7Area.MB, 0) == "MB"

    def test_format_hex_dump_reexported(self):
        out = format_hex_dump(bytearray([0x41]))
        assert "0000" in out and "41" in out


class TestPoll:
    def test_poll_decodes_variables(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray([0x2A] + [0] * 15)}, [var])
        snap = engine.poll()
        assert isinstance(snap, Snapshot)
        assert snap.poll_count == 1
        assert snap.connection_state == "connected"
        assert len(snap.readings) == 1
        r = snap.readings[0]
        assert r.spec == "DB210.Byte0"
        assert r.value == "42"
        assert r.raw_hex == "2A"
        assert r.changed is False  # first poll never marks changed

    def test_poll_emits_group_hex_dump(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(range(16))}, [var])
        snap = engine.poll()
        assert len(snap.groups) == 1
        g = snap.groups[0]
        assert g.key == "DB210"
        assert g.bytes_hex.startswith("00 01 02")
        assert "0000" in g.hex_dump

    def test_change_detection(self):
        var = S7Variable.parse("DB210.Byte0")
        buf = bytearray([1] + [0] * 15)
        engine, conn = make_engine({(S7Area.DB, 210): buf}, [var])
        first = engine.poll()
        assert first.readings[0].changed is False
        buf[0] = 2
        second = engine.poll()
        assert second.readings[0].value == "2"
        assert second.readings[0].changed is True
        assert second.poll_count == 2

    def test_poll_not_connected_returns_error_snapshot(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, conn = make_engine({}, [var])
        conn.state = ConnectionState.DISCONNECTED
        snap = engine.poll()
        assert snap.error == "Not connected"
        assert snap.readings == []
        assert snap.poll_count == 0

    def test_poll_read_error_captured(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, conn = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        conn.read_error = RuntimeError("comm lost")
        snap = engine.poll()
        assert snap.error == "comm lost"
        assert snap.connection_state == "error"

    def test_multiple_groups(self):
        db_var = S7Variable.parse("DB210.Byte0")
        eb_var = S7Variable.parse("EB.Byte0")
        engine, _ = make_engine(
            {
                (S7Area.DB, 210): bytearray([0x11] + [0] * 15),
                (S7Area.EB, 0): bytearray([0x22] + [0] * 15),
            },
            [db_var, eb_var],
            groups=[Grp(S7Area.DB, 210, 0, 16), Grp(S7Area.EB, 0, 0, 16)],
        )
        snap = engine.poll()
        values = {r.spec: r.value for r in snap.readings}
        assert values["DB210.Byte0"] == "17"
        assert values["EB.Byte0"] == "34"
        assert {g.key for g in snap.groups} == {"DB210", "EB"}


class TestWriteMode:
    def test_cycle(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        assert engine.write_mode == WriteMode.DISABLED
        assert engine.cycle_write_mode() == WriteMode.CONFIRM
        assert engine.cycle_write_mode() == WriteMode.ALLOWED
        assert engine.cycle_write_mode() == WriteMode.DISABLED

    def test_writes_enabled_flag(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        assert engine.writes_enabled is False
        engine.write_mode = WriteMode.ALLOWED
        assert engine.writes_enabled is True


class TestWrite:
    def test_write_blocked_when_disabled(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        with pytest.raises(WriteBlockedError):
            engine.write_variable("DB210.Byte0", "5")

    def test_write_byte(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, conn = make_engine({(S7Area.DB, 210): bytearray(16)}, [var],
                                   write_mode=WriteMode.ALLOWED)
        res = engine.write_variable("DB210.Byte0", "0x2A")
        assert conn.writes[-1] == (S7Area.DB, 0, b"\x2a", 210)
        assert res.bytes_hex == "2A"
        assert res.target == "DB210"

    def test_write_bit_read_modify_write(self):
        var = S7Variable.parse("DB210.Bit0.3")
        engine, conn = make_engine({(S7Area.DB, 210): bytearray([0x00] + [0] * 15)},
                                   [var], write_mode=WriteMode.ALLOWED)
        engine.write_variable("DB210.Bit0.3", "1")
        # bit 3 set -> 0x08
        assert conn.writes[-1] == (S7Area.DB, 0, b"\x08", 210)

    def test_write_spec_unmonitored(self):
        engine, conn = make_engine({(S7Area.DB, 5): bytearray(16)}, [],
                                   groups=[Grp(S7Area.DB, 5, 0, 16)],
                                   write_mode=WriteMode.ALLOWED)
        engine.write_spec("DB5.Byte2", "9")
        assert conn.writes[-1] == (S7Area.DB, 2, b"\x09", 5)

    def test_write_raw(self):
        engine, conn = make_engine({(S7Area.DB, 7): bytearray(16)}, [],
                                   groups=[Grp(S7Area.DB, 7, 0, 16)],
                                   write_mode=WriteMode.ALLOWED)
        res = engine.write_raw(7, 1, bytearray([0xFF, 0x01]))
        assert conn.writes[-1] == (S7Area.DB, 1, b"\xff\x01", 7)
        assert res.bytes_hex == "FF 01"


class TestConnectionControl:
    def test_reconnect(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, conn = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        engine.reconnect()
        assert conn.disconnect_calls == 1
        assert conn.connect_calls == 1
        assert engine.connection.connected

    def test_pause_flag(self):
        var = S7Variable.parse("DB210.Byte0")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(16)}, [var])
        assert engine.paused is False
        engine.paused = True
        assert engine.paused is True


class TestDescribe:
    def test_describe_metadata(self):
        var = S7Variable.parse("DB210.Bit0.3", label="estop")
        engine, _ = make_engine({(S7Area.DB, 210): bytearray(16)}, [var],
                                poll_interval=0.5)
        meta = engine.describe()
        assert meta["address"].startswith("10.0.0.5")
        assert meta["poll_interval"] == 0.5
        assert meta["write_mode"] == "disabled"
        assert meta["variables"][0]["spec"] == "DB210.Bit0.3"
        assert meta["variables"][0]["label"] == "estop"
        assert meta["variables"][0]["bit"] == 3
        assert meta["groups"][0]["key"] == "DB210"


class TestLogging:
    def test_change_logged(self):
        var = S7Variable.parse("DB210.Byte0")
        buf = bytearray([1] + [0] * 15)
        logger = MagicMock()
        engine, _ = make_engine({(S7Area.DB, 210): buf}, [var], logger=logger)
        engine.poll()
        logger.log.assert_not_called()  # first poll, no change
        buf[0] = 9
        engine.poll()
        logger.log.assert_called_once()
        entry = logger.log.call_args[0][0]
        assert entry.new_value == "9"
        assert entry.old_value == "1"
