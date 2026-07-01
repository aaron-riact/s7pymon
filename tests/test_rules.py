"""Tests for the output rules engine."""

from unittest.mock import MagicMock, call

import pytest

from busfactor.protocols import DataSource
from busfactor.rules import (
    FollowRule,
    OutputRule,
    PulseRule,
    RulesEngine,
    ToggleRule,
)
from busfactor.variable import S7Variable
from tests.fakes import BaseFakeConnection


# ----------------------------------------------------------- fake connection


class FakeConnection(BaseFakeConnection):
    """Fake connection that keys buffers by str(source) for EIP support."""

    def _buffer_key(self, source: DataSource) -> str:
        return str(source)


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
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x2A"))]

    def test_follow_int_to_int(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Int0", source="EIP.Input.Int2"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Int2": "1000"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\xE8\x03"))]

    def test_follow_bit_to_bit(self):
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3"),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")
        engine.apply(conn, {"EIP.Input.Bit0.3": "True"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x01"))]

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
        conn._buffers["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x00"))

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x01"))

    def test_toggle_bit_every_3_cycles(self):
        engine = RulesEngine([
            ToggleRule(target="EIP.Output.Bit0.0", period=3),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes == []  # not yet

        engine.apply(conn, {})
        assert conn.writes == []  # not yet

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x01"))

    def test_toggle_read_modify_write_bit(self):
        engine = RulesEngine([
            ToggleRule(target="EIP.Output.Bit2.3", period=1),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00\x00\x08")  # bit 3 already set

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 2, bytearray(b"\x08"))  # stays set

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 2, bytearray(b"\x00"))  # cleared

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 2, bytearray(b"\x08"))  # set again

    def test_pulse_trigger(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=5),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")

        engine.trigger_pulse("EIP.Output.Bit0.0")

        for _ in range(5):
            engine.apply(conn, {})
            assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x00"))

    def test_pulse_duration_1(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=1),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")

        engine.trigger_pulse("EIP.Output.Bit0.0")
        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x01"))

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x00"))

    def test_pulse_not_triggered(self):
        engine = RulesEngine([
            PulseRule(target="EIP.Output.Bit0.0", duration=5),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")

        engine.apply(conn, {})
        assert conn.writes[-1] == (DataSource.eip("Output"), 0, bytearray(b"\x00"))

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
        conn._buffers["EIP.Output"] = bytearray(b"\x00")
        conn._buffers["EIP.Input"] = bytearray(b"\x2A")

        engine.apply(conn, {"EIP.Input.Byte0": "42"})
        assert (DataSource.eip("Output"), 0, bytearray(b"\x2A")) in conn.writes

        # Toggle hasn't fired (period=2, only 1 cycle elapsed)
        assert (DataSource.eip("Output"), 0, bytearray(b"\xAB")) not in conn.writes

    def test_rules_property(self):
        rules = [FollowRule(target="DB1.Byte0", source="DB1.Byte1")]
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
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x63"))]

    def test_follow_eip_to_s7(self):
        """Reverse direction: EIP input to S7 DB."""
        engine = RulesEngine([
            FollowRule(target="DB200.Byte0", source="EIP.Input.Byte0"),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte0": "77"})
        assert conn.writes == [(DataSource.s7_db(200), 0, bytearray(b"\x4D"))]

    def test_follow_inverted_bit_true_to_false(self):
        """Inverted follow writes the inverse of a True source bit."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3", inverted=True),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\xFF")  # current byte
        engine.apply(conn, {"EIP.Input.Bit0.3": "True"})
        # Source is True, inverted → write False → bit 0 cleared
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\xFE"))]

    def test_follow_inverted_bit_false_to_true(self):
        """Inverted follow writes the inverse of a False source bit."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3", inverted=True),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")
        engine.apply(conn, {"EIP.Input.Bit0.3": "False"})
        # Source is False, inverted → write True → bit 0 set
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x01"))]

    def test_follow_non_inverted_bit_still_works(self):
        """Regular (non-inverted) follow still copies the value as-is."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3", inverted=False),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\x00")
        engine.apply(conn, {"EIP.Input.Bit0.3": "True"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x01"))]

    def test_follow_inverted_default_is_false(self):
        """Default inverted=False matches non-inverted behavior."""
        assert not FollowRule(target="t", source="s").inverted

    def test_follow_delay_0_writes_immediately(self):
        """delay_ms=0 writes immediately (same as no delay)."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4", delay_ms=0),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x2A"))]

    def test_follow_delay_scheduled_not_written(self):
        """delay_ms > 0 schedules but does not write immediately."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4", delay_ms=60_000),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == []

    def test_follow_delay_expired_writes(self):
        """delay_ms elapses, then next apply() writes.

        Checks the real bug scenario: repeated apply() calls with the
        same value must NOT reset the timer every cycle (the bug was
        that _apply_follow unconditionally rescheduled on every call).
        """
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4", delay_ms=10),
        ])
        conn = FakeConnection()

        # Simulate several poll cycles with the same value
        for _ in range(5):
            engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == []  # still not written

        import time
        time.sleep(0.015)  # past the 10ms delay

        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x2A"))]

    def test_follow_delay_rescheduled_on_change(self):
        """Source value change before delay expires reschedules (timer resets)."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4", delay_ms=10),
        ])
        conn = FakeConnection()
        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == []

        engine.apply(conn, {"EIP.Input.Byte4": "99"})
        assert conn.writes == []  # still pending with updated value

        # Value changed, so timer was reset — the 10ms delay starts over
        # from the last apply().  Wait and verify the NEW value is written.
        import time
        time.sleep(0.015)

        engine.apply(conn, {"EIP.Input.Byte4": "99"})
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\x63"))]  # 0x63 = 99

    def test_follow_delay_not_rescheduled_on_same_value(self):
        """Unchanged source value does not reset the timer."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Byte0", source="EIP.Input.Byte4", delay_ms=10),
        ])
        conn = FakeConnection()

        engine.apply(conn, {"EIP.Input.Byte4": "42"})
        assert conn.writes == []

        # Snapshot the due time after the first apply
        pending = engine._states[0].pending
        assert pending is not None
        ts_after_first = pending.due_at

        # Same value again — should NOT reset the timer
        engine.apply(conn, {"EIP.Input.Byte4": "42"})

        pending = engine._states[0].pending
        assert pending is not None
        assert pending.due_at == ts_after_first, "Timer reset on unchanged value — write never fires"

    def test_follow_delay_bit_write_modifies_correctly(self):
        """Delayed bit follow modifies only the target bit."""
        engine = RulesEngine([
            FollowRule(target="EIP.Output.Bit0.0", source="EIP.Input.Bit0.3", delay_ms=10),
        ])
        conn = FakeConnection()
        conn._buffers["EIP.Output"] = bytearray(b"\xFF")

        engine.apply(conn, {"EIP.Input.Bit0.3": "False"})
        assert conn.writes == []  # not yet

        import time
        time.sleep(0.015)

        engine.apply(conn, {"EIP.Input.Bit0.3": "False"})
        # Should have written 0xFE (only bit 0 cleared)
        assert conn.writes == [(DataSource.eip("Output"), 0, bytearray(b"\xFE"))]

    def test_follow_delay_default_is_0(self):
        """Default delay_ms=0."""
        assert FollowRule(target="t", source="s").delay_ms == 0
