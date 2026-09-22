"""Tests for the Modbus connection driver.

A fake pymodbus client records every call, so the tests assert on the exact
register addresses and counts that go on the wire — that is where the
byte-to-register conversion can go wrong.
"""

import socket

import pytest
from pymodbus.exceptions import ModbusIOException

from busfactor.modbus import (
    format_row_address,
    MAX_BITS_PER_READ,
    MAX_REGISTERS_PER_READ,
    MAX_REGISTERS_PER_WRITE,
    ModbusConnection,
    bits_to_bytes,
    bytes_to_bits,
    bytes_to_registers,
    registers_to_bytes,
)
from busfactor.protocols import ConnectionConfig, ConnectionState, DataSource

HOLDING = DataSource.modbus("Holding")
INPUT_REGS = DataSource.modbus("Input")
COIL = DataSource.modbus("Coil")
DISCRETE = DataSource.modbus("Discrete")


class FakeResponse:
    def __init__(self, registers=None, bits=None, error=False):
        self.registers = registers or []
        self.bits = bits or []
        self._error = error

    def isError(self):
        return self._error


class FakeSocket:
    """Only the two calls abort() makes on a pymodbus socket."""

    def __init__(self):
        self.shutdown_calls: list[int] = []

    def shutdown(self, how):
        self.shutdown_calls.append(how)


class FakeClient:
    """Stands in for pymodbus. Holding/input registers default to address+1."""

    def __init__(self, error=False, connect_ok=True, short_to=None, pad_bits=False,
                 drop_first=0, fail_writes=0, raise_first=0):
        self.calls: list[tuple] = []
        self.registers: dict[int, int] = {}
        self.coils: dict[int, bool] = {}
        self.error = error
        self.connect_ok = connect_ok
        # short_to mimics a gateway that drops part of a reply.
        self.short_to = short_to
        # pad_bits mimics pymodbus padding a bit reply out to a whole byte.
        self.pad_bits = pad_bits
        # drop_first mimics a gateway losing the first N replies, then recovering.
        self.drop_first = drop_first
        # fail_writes mimics the first N writes being rejected.
        self.fail_writes = fail_writes
        # raise_first mimics pymodbus giving up: it raises rather than
        # returning anything at all.
        self.raise_first = raise_first
        self.closed = False
        self.socket = None

    def _maybe_raise(self):
        if self.raise_first > 0:
            self.raise_first -= 1
            raise ModbusIOException("No response received after 3 retries")

    def _limit(self, values):
        if self.drop_first > 0:
            self.drop_first -= 1
            return []
        return values if self.short_to is None else values[: self.short_to]

    def _bits(self, address, count):
        bits = [self.coils.get(address + i, False) for i in range(count)]
        if self.pad_bits and count % 8:
            bits += [False] * (8 - count % 8)
        return self._limit(bits)

    def connect(self):
        self.socket = FakeSocket()
        return self.connect_ok

    def close(self):
        self.closed = True
        self.socket = None

    def _regs(self, address, count):
        return self._limit(
            [self.registers.get(address + i, address + i + 1) for i in range(count)]
        )

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_holding", address, count, device_id))
        self._maybe_raise()
        if self.error:
            return FakeResponse(error=True)
        return FakeResponse(registers=self._regs(address, count))

    def read_input_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_input", address, count, device_id))
        self._maybe_raise()
        return FakeResponse(registers=self._regs(address, count))

    def read_coils(self, address, *, count=1, device_id=1):
        self.calls.append(("read_coils", address, count, device_id))
        self._maybe_raise()
        return FakeResponse(bits=self._bits(address, count))

    def read_discrete_inputs(self, address, *, count=1, device_id=1):
        self.calls.append(("read_discrete", address, count, device_id))
        self._maybe_raise()
        return FakeResponse(bits=self._bits(address, count))

    def write_registers(self, address, values, *, device_id=1):
        self.calls.append(("write_registers", address, list(values), device_id))
        if self.fail_writes > 0:
            self.fail_writes -= 1
            return FakeResponse(error=True)
        for i, v in enumerate(values):
            self.registers[address + i] = v
        return FakeResponse()

    def write_coils(self, address, values, *, device_id=1):
        self.calls.append(("write_coils", address, list(values), device_id))
        for i, v in enumerate(values):
            self.coils[address + i] = v
        return FakeResponse()


def make_connection(client=None, **cfg):
    client = client if client is not None else FakeClient()
    config = ConnectionConfig(protocol="modbus", address="10.0.0.1", tcp_port=502, **cfg)
    conn = ModbusConnection(config, client_factory=lambda: client)
    return conn, client


class TestByteRegisterConversion:
    def test_registers_to_bytes_is_high_byte_first(self):
        assert bytes(registers_to_bytes([0x016C, 0x0040])) == b"\x01\x6c\x00\x40"

    def test_bytes_to_registers_is_high_byte_first(self):
        assert bytes_to_registers(b"\x01\x6c\x00\x40") == [0x016C, 0x0040]

    def test_bytes_to_registers_rejects_odd_length(self):
        with pytest.raises(ValueError, match="whole number of registers"):
            bytes_to_registers(b"\x01\x6c\x00")

    def test_round_trip(self):
        regs = [0, 1, 65535, 4660]
        assert bytes_to_registers(registers_to_bytes(regs)) == regs

    def test_bits_to_bytes_is_lsb_first(self):
        assert bytes(bits_to_bytes([True, False, False, True], 1)) == b"\x09"

    def test_bytes_to_bits_is_lsb_first(self):
        assert bytes_to_bits(b"\x09")[:4] == [True, False, False, True]


class TestConnect:
    def test_connect_sets_state(self):
        conn, _ = make_connection()
        conn.connect()
        assert conn.connected
        assert conn.state == ConnectionState.CONNECTED

    def test_failed_connect_raises_and_records_error(self):
        conn, _ = make_connection(FakeClient(connect_ok=False))
        with pytest.raises(ConnectionError, match="Could not open"):
            conn.connect()
        assert conn.state == ConnectionState.ERROR
        assert "Could not open" in conn.error

    def test_disconnect_closes_client(self):
        conn, client = make_connection()
        conn.connect()
        conn.disconnect()
        assert client.closed
        assert conn.state == ConnectionState.DISCONNECTED

    def test_read_before_connect_raises(self):
        conn, _ = make_connection()
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.read_source(HOLDING, 0, 2)

    def test_status_extra_shows_slave_and_framer(self):
        conn, _ = make_connection(slave_id=65, framer="rtu")
        conn.connect()
        # Counters join it once requests have been made; see TestCounters.
        assert conn.status_extra == {"Slave": "65", "Framer": "rtu", "Req": "0"}


class TestReadRegisters:
    def test_reads_the_register_holding_the_byte_offset(self):
        conn, client = make_connection(slave_id=65)
        conn.connect()
        conn.read_source(HOLDING, 536, 2)
        assert client.calls == [("read_holding", 268, 1, 65)]

    def test_returns_register_bytes_high_first(self):
        conn, client = make_connection()
        conn.connect()
        client.registers[268] = 364
        result = conn.read_source(HOLDING, 536, 2)
        assert bytes(result.data) == b"\x01\x6c"
        assert result.start == 536
        assert result.size == 2

    def test_odd_offset_reads_both_registers_and_slices(self):
        conn, client = make_connection()
        conn.connect()
        client.registers[0] = 0x1122
        client.registers[1] = 0x3344
        result = conn.read_source(HOLDING, 1, 2)
        assert client.calls == [("read_holding", 0, 2, 1)]
        assert bytes(result.data) == b"\x22\x33"

    def test_single_byte_read(self):
        conn, client = make_connection()
        conn.connect()
        client.registers[3] = 0xABCD
        result = conn.read_source(HOLDING, 7, 1)
        assert bytes(result.data) == b"\xcd"

    def test_input_table_uses_input_function(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(INPUT_REGS, 0, 2)
        assert client.calls == [("read_input", 0, 1, 1)]

    def test_long_read_is_split_at_the_protocol_limit(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(HOLDING, 0, (MAX_REGISTERS_PER_READ + 5) * 2)
        assert [(c[0], c[1], c[2]) for c in client.calls] == [
            ("read_holding", 0, MAX_REGISTERS_PER_READ),
            ("read_holding", MAX_REGISTERS_PER_READ, 5),
        ]

    def test_a_configured_limit_splits_the_read_further(self):
        conn, client = make_connection(max_registers_per_read=13)
        conn.connect()
        conn.read_source(HOLDING, 512, 40)
        assert [(c[0], c[1], c[2]) for c in client.calls] == [
            ("read_holding", 256, 13),
            ("read_holding", 269, 7),
        ]

    def test_a_configured_limit_keeps_the_data_contiguous(self):
        conn, _ = make_connection(max_registers_per_read=13)
        conn.connect()
        result = conn.read_source(HOLDING, 0, 40)
        # FakeClient defaults register n to n+1.
        expected = registers_to_bytes([n + 1 for n in range(20)])
        assert bytes(result.data) == bytes(expected)

    def test_a_limit_above_the_protocol_maximum_is_clamped(self):
        conn, client = make_connection(max_registers_per_read=500)
        conn.connect()
        conn.read_source(HOLDING, 0, (MAX_REGISTERS_PER_READ + 1) * 2)
        assert [c[2] for c in client.calls] == [MAX_REGISTERS_PER_READ, 1]

    def test_a_limit_below_one_still_makes_progress(self):
        conn, client = make_connection(max_registers_per_read=0)
        conn.connect()
        conn.read_source(HOLDING, 0, 4)
        assert [c[2] for c in client.calls] == [1, 1]

    def test_split_read_data_is_contiguous(self):
        conn, _ = make_connection()
        conn.connect()
        result = conn.read_source(HOLDING, 0, (MAX_REGISTERS_PER_READ + 2) * 2)
        # FakeClient defaults register n to n+1.
        expected = registers_to_bytes([n + 1 for n in range(MAX_REGISTERS_PER_READ + 2)])
        assert bytes(result.data) == bytes(expected)

    def test_error_response_raises(self):
        conn, _ = make_connection(FakeClient(error=True))
        conn.connect()
        with pytest.raises(ConnectionError, match="read holding"):
            conn.read_source(HOLDING, 0, 2)

    def test_zero_size_is_rejected(self):
        conn, _ = make_connection()
        conn.connect()
        with pytest.raises(ValueError, match="size must be positive"):
            conn.read_source(HOLDING, 0, 0)


class TestReadBits:
    def test_byte_offset_maps_to_eight_coils(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(COIL, 2, 1)
        assert client.calls == [("read_coils", 16, 8, 1)]

    def test_coils_pack_lsb_first(self):
        conn, client = make_connection()
        conn.connect()
        client.coils[0] = True
        client.coils[3] = True
        result = conn.read_source(COIL, 0, 1)
        assert bytes(result.data) == b"\x09"

    def test_discrete_table_uses_discrete_function(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(DISCRETE, 0, 1)
        assert client.calls == [("read_discrete", 0, 8, 1)]

    def test_long_bit_read_is_split(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(COIL, 0, (MAX_BITS_PER_READ // 8) + 1)
        assert [(c[0], c[1], c[2]) for c in client.calls] == [
            ("read_coils", 0, MAX_BITS_PER_READ),
            ("read_coils", MAX_BITS_PER_READ, 8),
        ]


class TestWrite:
    def test_aligned_write_sends_registers(self):
        conn, client = make_connection(slave_id=65)
        conn.connect()
        conn.write_source(HOLDING, 0, bytearray(b"\x00\xc8\x01\xf4"))
        assert client.calls == [("write_registers", 0, [200, 500], 65)]

    def test_odd_offset_preserves_the_untouched_half(self):
        conn, client = make_connection()
        conn.connect()
        client.registers[0] = 0x1122
        conn.write_source(HOLDING, 1, bytearray(b"\xff"))
        assert ("write_registers", 0, [0x11FF], 1) in client.calls

    def test_odd_length_preserves_the_trailing_half(self):
        conn, client = make_connection()
        conn.connect()
        client.registers[1] = 0x3344
        conn.write_source(HOLDING, 2, bytearray(b"\xff"))
        assert ("write_registers", 1, [0xFF44], 1) in client.calls

    def test_long_write_is_split_at_the_protocol_limit(self):
        conn, client = make_connection()
        conn.connect()
        conn.write_source(HOLDING, 0, bytearray((MAX_REGISTERS_PER_WRITE + 3) * 2))
        writes = [(c[1], len(c[2])) for c in client.calls if c[0] == "write_registers"]
        assert writes == [(0, MAX_REGISTERS_PER_WRITE), (MAX_REGISTERS_PER_WRITE, 3)]

    def test_coil_write_sends_bits(self):
        conn, client = make_connection()
        conn.connect()
        conn.write_source(COIL, 0, bytearray(b"\x09"))
        assert client.calls == [
            ("write_coils", 0, [True, False, False, True, False, False, False, False], 1)
        ]

    def test_input_registers_are_read_only(self):
        conn, _ = make_connection()
        conn.connect()
        with pytest.raises(ValueError, match="read-only"):
            conn.write_source(INPUT_REGS, 0, bytearray(b"\x00\x01"))

    def test_discrete_inputs_are_read_only(self):
        conn, _ = make_connection()
        conn.connect()
        with pytest.raises(ValueError, match="read-only"):
            conn.write_source(DISCRETE, 0, bytearray(b"\x01"))

    def test_empty_write_does_nothing(self):
        conn, client = make_connection()
        conn.connect()
        conn.write_source(HOLDING, 0, bytearray())
        assert client.calls == []


class TestShortReplies:
    def test_short_register_reply_raises(self):
        conn, _ = make_connection(FakeClient(short_to=1))
        conn.connect()
        with pytest.raises(ConnectionError, match="returned 1 of 2 values"):
            conn.read_source(HOLDING, 0, 4)

    def test_empty_register_reply_raises(self):
        conn, _ = make_connection(FakeClient(short_to=0))
        conn.connect()
        with pytest.raises(ConnectionError, match="returned 0 of 1 values"):
            conn.read_source(HOLDING, 534, 2)

    def test_short_coil_reply_raises(self):
        conn, _ = make_connection(FakeClient(short_to=1))
        conn.connect()
        with pytest.raises(ConnectionError, match="returned 1 of 8 values"):
            conn.read_source(COIL, 0, 1)

    def test_padded_bit_reply_is_accepted(self):
        conn, client = make_connection(FakeClient(pad_bits=True))
        conn.connect()
        client.coils[0] = True
        client.coils[3] = True
        # A 4-coil read comes back padded to a whole byte. That is not short.
        assert bytes(conn.read_source(COIL, 0, 1).data) == b"\x09"


class TestRetry:
    def test_a_dropped_reply_is_retried(self):
        conn, client = make_connection(FakeClient(drop_first=1))
        conn.connect()
        result = conn.read_source(HOLDING, 534, 2)
        assert len(result.data) == 2
        assert len(client.calls) == 2

    def test_it_gives_up_after_the_configured_attempts(self):
        conn, client = make_connection(FakeClient(drop_first=99), retries=3)
        conn.connect()
        with pytest.raises(ConnectionError, match="returned 0 of 1 values"):
            conn.read_source(HOLDING, 534, 2)
        assert len(client.calls) == 3

    def test_retries_can_be_turned_off(self):
        conn, client = make_connection(FakeClient(drop_first=99), retries=1)
        conn.connect()
        with pytest.raises(ConnectionError):
            conn.read_source(HOLDING, 534, 2)
        assert len(client.calls) == 1

    def test_a_disconnected_client_is_not_retried(self):
        conn, client = make_connection()
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.read_source(HOLDING, 534, 2)
        assert client.calls == []

    def test_a_rejected_write_is_retried(self):
        conn, client = make_connection(FakeClient(fail_writes=1))
        conn.connect()
        conn.write_source(HOLDING, 0, bytearray(b"\x00\xc8"))
        writes = [c for c in client.calls if c[0] == "write_registers"]
        assert len(writes) == 2
        assert client.registers[0] == 200

    def test_a_write_that_keeps_failing_raises(self):
        conn, client = make_connection(FakeClient(fail_writes=99), retries=3)
        conn.connect()
        with pytest.raises(ConnectionError, match="write registers"):
            conn.write_source(HOLDING, 0, bytearray(b"\x00\xc8"))
        assert len([c for c in client.calls if c[0] == "write_registers"]) == 3


class TestMissingReply:
    """pymodbus raises when nothing comes back at all. That is the same event
    as a reply that arrives empty, and it has to be retried the same way --
    the original tests only covered the empty case, so this one escaped."""

    def test_a_raised_missing_reply_is_retried(self):
        conn, client = make_connection(FakeClient(raise_first=1))
        conn.connect()
        assert len(conn.read_source(HOLDING, 534, 2).data) == 2
        assert len(client.calls) == 2

    def test_a_reply_that_never_comes_gives_up_as_a_connection_error(self):
        conn, _ = make_connection(FakeClient(raise_first=99), retries=3)
        conn.connect()
        with pytest.raises(ConnectionError, match="No response"):
            conn.read_source(HOLDING, 534, 2)

    def test_a_raised_missing_reply_counts_as_a_resend(self):
        conn, _ = make_connection(FakeClient(raise_first=1))
        conn.connect()
        conn.read_source(HOLDING, 534, 2)
        assert conn.requests == 1
        assert conn.resends == 1
        assert conn.failures == 0


class TestCounters:
    def test_a_clean_bus_counts_only_requests(self):
        conn, _ = make_connection()
        conn.connect()
        for _ in range(3):
            conn.read_source(HOLDING, 534, 2)
        assert (conn.requests, conn.resends, conn.failures) == (3, 0, 0)

    def test_a_giving_up_request_is_counted_once_as_a_failure(self):
        conn, _ = make_connection(FakeClient(drop_first=99), retries=3)
        conn.connect()
        with pytest.raises(ConnectionError):
            conn.read_source(HOLDING, 534, 2)
        assert conn.requests == 1
        assert conn.resends == 2      # two retries before the last attempt
        assert conn.failures == 1

    def test_the_status_bar_hides_counters_that_are_zero(self):
        conn, _ = make_connection(slave_id=67, framer="rtu")
        conn.connect()
        conn.read_source(HOLDING, 534, 2)
        assert conn.status_extra == {"Slave": "67", "Framer": "rtu", "Req": "1"}

    def test_the_status_bar_shows_resends_with_a_rate(self):
        conn, _ = make_connection(FakeClient(drop_first=1), slave_id=67, framer="rtu")
        conn.connect()
        conn.read_source(HOLDING, 534, 2)
        assert conn.status_extra["Resent"] == "1 (100.0%)"

    def test_the_status_bar_is_empty_before_connecting(self):
        conn, _ = make_connection()
        assert conn.status_extra == {}


class TestSourceResolution:
    def test_unknown_source_is_rejected(self):
        conn, _ = make_connection()
        conn.connect()
        with pytest.raises(ValueError, match="Invalid Modbus source"):
            conn.read_source(DataSource("EIP.Input"), 0, 2)

    def test_table_name_is_case_insensitive(self):
        conn, client = make_connection()
        conn.connect()
        conn.read_source(DataSource("MB.holding"), 0, 2)
        assert client.calls[0][0] == "read_holding"


class TestRowAddress:
    def test_register_table_shows_the_register(self):
        assert format_row_address("MB.Holding", 534) == "R267"

    def test_input_registers_too(self):
        assert format_row_address("MB.Input", 0) == "R0"

    def test_rows_step_by_eight_registers(self):
        # A hex dump row is 16 bytes, which is 8 registers.
        assert format_row_address("MB.Holding", 534) == "R267"
        assert format_row_address("MB.Holding", 550) == "R275"

    def test_coil_table_shows_the_coil(self):
        assert format_row_address("MB.Coil", 2) == "C16"
        assert format_row_address("MB.Discrete", 0) == "C0"

    def test_odd_byte_offset_has_no_register_number(self):
        assert format_row_address("MB.Holding", 535) is None

    def test_other_protocols_are_left_alone(self):
        assert format_row_address("EIP.Input", 0) is None
        assert format_row_address("DB210", 0) is None


class TestAbort:
    """Quitting must not wait for a request the gateway is not answering."""

    def test_shuts_the_socket_down_and_closes(self):
        conn, client = make_connection()
        conn.connect()
        sock = client.socket
        assert sock is not None
        conn.abort()
        assert sock.shutdown_calls == [socket.SHUT_RDWR]
        assert client.closed
        assert conn.state == ConnectionState.DISCONNECTED

    def test_does_not_wait_for_a_read_in_flight(self):
        """The lock is held for the whole of a read; abort must not queue behind it."""
        conn, client = make_connection()
        conn.connect()
        with conn._lock:
            conn.abort()
        assert client.closed
        assert conn.state == ConnectionState.DISCONNECTED

    def test_survives_a_connection_that_never_opened(self):
        conn, _ = make_connection()
        conn.abort()
        assert conn.state == ConnectionState.DISCONNECTED

    def test_ignores_a_socket_already_gone(self):
        conn, client = make_connection()
        conn.connect()

        def refuse(how):
            raise OSError("not connected")

        sock = client.socket
        assert sock is not None
        sock.shutdown = refuse
        conn.abort()
        assert client.closed
