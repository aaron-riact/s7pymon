"""Tests for the output rules engine."""

from unittest.mock import MagicMock, call

import pytest

from s7pymon.protocols import Connection, DataSource, ReadResult
from s7pymon.rules import (
    FollowRule,
    OutputRule,
    PulseRule,
    RulesEngine,
    ToggleRule,
)
from s7pymon.variable import S7Variable


# ----------------------------------------------------------- fake connection


class FakeConnection(Connection):
    protocol = "test"

    def __init__(self):
        self._buf: dict[str, bytearray] = {}
        self.writes: list[tuple[str, int, bytearray]] = []

    @property
    def state(self):
        from s7pymon.protocols import ConnectionState
        return ConnectionState.CONNECTED

    @property
    def connected(self):
        return True

    @property
    def config(self):
        from s7pymon.protocols import ConnectionConfig
        return ConnectionConfig(address="fake")

    def connect(self):
        pass

    def disconnect(self):
        pass

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        key = str(source)
        if key not in self._buf or len(self._buf[key]) < offset + size:
            return ReadResult(data=bytearray(size), source=source, start=offset, size=size)
        data = self._buf[key][offset:offset + size]
        if len(data) < size:
            data = data + bytearray(size - len(data))
        return ReadResult(data=data, source=source, start=offset, size=size)

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        key = str(source)
        if key not in self._buf or len(self._buf[key]) < offset + len(data):
            needed = max(len(self._buf.get(key, bytearray())), offset + len(data))
            buf = self._buf.get(key, bytearray(needed))
            if len(buf) < needed:
                buf = buf + bytearray(needed - len(buf))
            self._buf[key] = buf
        self._buf[key][offset:offset + len(data)] = data
        self.writes.append((key, offset, bytearray(data)))


# -------------------------------------------------------------------- tests


class TestRulesEngine:
    def test_empty_rules(self):
        engine = RulesEngine([])
        conn = FakeConnection()
        engine.apply(conn, {})
        assert conn.writes == []

    def test_follow_byte_to_byte(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == [("EIP.Output", 0, bytearray(b"\x2A"))]

    def test_follow_int_to_int(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Int0", source="EIP.Input.Int2"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Int2": "1000"})
        assert conn.writes == [("EIP.Output", 0, bytearray(b"\x03\xE8"))]

    def test_follow_bit_to_bit(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3"),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")
        engine.apply(conn, {"EIP.Input.Bit0.3": "True"})
        assert conn.writes == [("EIP.Output", 0, bytearray(b"\x01"))]

    def test_follow_missing_source_skips(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {})
        assert conn.writes == []

    def test_toggle_bit_every_cycle(self):
        engine = RulesEngine([
            ToggleRule(target="EIP.Output.Bit0.0", period=1),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x00"))

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x01"))

    def test_toggle_bit_every_3_cycles(self):
        engine = RulesEngine([
            ToggleRule(target="EIP.Output.Bit0.0", period=3),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes == []  # not yet

        engine.apply(conn, {})
        assert conn.writes == []  # not yet

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x01"))

    def test_toggle_read_modify_write_bit(self):
        engine = RulesEngine([
            ToggleRule(target="EIP.Output.Bit2.3", period=1),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00\x00\x08")  # bit 3 already set

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 2, bytearray(b"\x08"))  # stays set

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 2, bytearray(b"\x00"))  # cleared

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 2, bytearray(b"\x08"))  # set again

    def test_pulse_trigger(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=5),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")

        engine.trigger_pulse("EIP.Output.Bit0.0")

        for _ in range(5):
            engine.apply(conn, {})
            assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x00"))

    def test_pulse_duration_1(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=1),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")

        engine.trigger_pulse("EIP.Output.Bit0.0")
        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x00"))

    def test_pulse_not_triggered(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=5),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes[-1] == ("EIP.Output", 0, bytearray(b"\x00"))

    def test_pulse_trigger_unknown_raises(self):
        engine = RulesEngine([])
        with pytest.raises(KeyError, match="No pulse rule for"):
            engine.trigger_pulse("EIP.Output.Bit0.0")

    def test_mixed_rules(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte0"),
            ToggleRule(target="EIP.Output.Bit0.7", period=2),
        ])
        conn = FakeConnection()
        conn._buf["EIP.Output"] = bytearray(b"\x00")
        conn._buf["EIP.Input"] = bytearray(b"\x2A")

        engine.apply(conn, {"EIP.Input.Byte0": "42"})
        assert ("EIP.Output", 0, bytearray(b"\x2A")) in conn.writes

        # Toggle hasn't fired (period=2, only 1 cycle elapsed)
        assert ("EIP.Output", 0, bytearray(b"\xAB")) not in conn.writes

    def test_rules_property(self):
        rules = [FollowRule(target="t", source="s")]
        engine = RulesEngine(rules)
        assert engine.rules == rules
        assert engine.rules is not rules  # defensive copy

    def test_follow_cross_protocol(self):
        """Follow from S7 DB to EIP output."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="DB200.Byte0"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"DB200.Byte0": "99"})
        assert conn.writes == [("EIP.Output", 0, bytearray(b"\x63"))]

    def test_follow_eip_to_s7(self):
        """Reverse direction: EIP input to S7 DB."""
        engine = RulesEngine([
            FollowRule(target="DB200.Byte0", source="EIP.Input.Byte0"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte0": "77"})
        assert conn.writes == [("DB200", 0, bytearray(b"\x4D"))]
