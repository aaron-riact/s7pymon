"""Tests for EIPConnection."""

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from s7pymon.protocols import ConnectionConfig, ConnectionState, DataSource, ReadResult


@contextmanager
def _mock_ethernetip():
    """Replace the ethernetip module with a MagicMock for test isolation."""
    mock = MagicMock()
    mock.EtherNetIP.ENIP_IO_TYPE_INPUT = 0
    mock.EtherNetIP.ENIP_IO_TYPE_OUTPUT = 1

    eip_instance = MagicMock()
    mock.EtherNetIP.return_value = eip_instance

    conn_instance = MagicMock()
    conn_instance.sendFwdOpenReq.return_value = 0
    eip_instance.explicit_conn.return_value = conn_instance

    input_bits = [False] * 32
    output_bits = [False] * 32

    def register_assembly_side(iotype, size, inst, conn):
        return input_bits if iotype == 0 else output_bits

    eip_instance.registerAssembly.side_effect = register_assembly_side

    old = sys.modules.get("ethernetip")
    sys.modules["ethernetip"] = mock
    try:
        yield mock, eip_instance, conn_instance, input_bits, output_bits
    finally:
        if old is not None:
            sys.modules["ethernetip"] = old
        else:
            sys.modules.pop("ethernetip", None)


@pytest.fixture
def config():
    return ConnectionConfig(
        protocol="eip",
        address="192.168.1.100",
        input_size=4,
        output_size=4,
        rpi_ms=50,
    )


@pytest.fixture
def mock_eip():
    """Mock ethernetip library for the duration of a test."""
    with _mock_ethernetip() as mocks:
        yield mocks


def make_connection(config):
    """Create a bare EIPConnection."""
    from s7pymon.eip import EIPConnection

    return EIPConnection(config)


@pytest.fixture
def connected_conn(config, mock_eip):
    """Create a connected EIPConnection with mocked library."""
    conn = make_connection(config)
    conn.connect()
    return conn, mock_eip


# --------------------------------------------------------------------------- lifecycle


class TestEIPConnectionLifecycle:
    def test_initial_state(self, config):
        conn = make_connection(config)
        assert conn.state == ConnectionState.DISCONNECTED
        assert not conn.connected
        assert conn.error == ""

    def test_connect_success(self, config, mock_eip):
        _, eip_instance, conn_instance, _, _ = mock_eip
        conn = make_connection(config)
        conn.connect()

        assert conn.state == ConnectionState.CONNECTED
        assert conn.connected
        eip_instance.explicit_conn.assert_called_once()
        conn_instance.registerSession.assert_called_once()
        assert eip_instance.registerAssembly.call_count == 2
        eip_instance.startIO.assert_called_once()
        conn_instance.sendFwdOpenReq.assert_called_once()
        conn_instance.produce.assert_called_once()

    def test_disconnect(self, config, mock_eip):
        conn = make_connection(config)
        conn.connect()
        conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.error == ""

    def test_connect_failure_cleans_up(self, config, mock_eip):
        _, eip_instance, conn_instance, _, _ = mock_eip
        conn_instance.registerSession.side_effect = RuntimeError("no device")
        conn = make_connection(config)

        with pytest.raises(RuntimeError, match="no device"):
            conn.connect()
        assert conn.state == ConnectionState.ERROR
        assert "no device" in conn.error

    def test_connect_missing_library(self, config):
        from s7pymon.eip import EIPConnection

        conn = EIPConnection(config)
        saved = sys.modules.get("ethernetip")
        sys.modules.pop("ethernetip", None)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ethernetip":
                raise ImportError("No module named 'ethernetip'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(ConnectionError, match="ethernetip library not available"):
                conn.connect()
            assert conn.state == ConnectionState.ERROR
            assert "ethernetip library not available" in conn.error
        if saved is not None:
            sys.modules["ethernetip"] = saved

    def test_context_manager(self, config, mock_eip):
        conn = make_connection(config)
        with conn:
            assert conn.connected
        assert conn.state == ConnectionState.DISCONNECTED

    def test_config_accessible(self, config):
        conn = make_connection(config)
        assert conn.config is config

    def test_disconnect_when_not_connected(self, config):
        conn = make_connection(config)
        conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED


# --------------------------------------------------------------------------- read


class TestEIPConnectionRead:
    def test_read_input_assembly(self, config, connected_conn):
        conn, (_, _, _, input_bits, _) = connected_conn
        input_bits[0] = True
        input_bits[8] = True

        result = conn.read_source(DataSource.eip("Input"), 0, 2)
        assert isinstance(result, ReadResult)
        assert result.source == DataSource.eip("Input")
        assert result.start == 0
        assert result.size == 2
        assert result.data == bytearray(b"\x01\x01")

    def test_read_input_offset(self, config, connected_conn):
        conn, (_, _, _, input_bits, _) = connected_conn
        input_bits[16] = True
        input_bits[17] = True

        result = conn.read_source(DataSource.eip("Input"), 2, 1)
        assert result.data == bytearray(b"\x03")

    def test_read_not_connected(self, config):
        conn = make_connection(config)
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.read_source(DataSource.eip("Input"), 0, 1)

    def test_read_beyond_assembly_raises(self, config, connected_conn):
        conn, _ = connected_conn
        with pytest.raises(ValueError, match="exceeds assembly size"):
            conn.read_source(DataSource.eip("Input"), 0, 5)

    def test_read_invalid_source(self, config, connected_conn):
        conn, _ = connected_conn
        with pytest.raises(ValueError, match="Invalid EIP source"):
            conn.read_source(DataSource("EIP.Foo"), 0, 1)

    def test_read_config_not_supported(self, config, connected_conn):
        conn, _ = connected_conn
        with pytest.raises(ValueError, match="not yet supported"):
            conn.read_source(DataSource.eip("Config"), 0, 1)


# --------------------------------------------------------------------------- write


class TestEIPConnectionWrite:
    def test_write_output_assembly(self, config, connected_conn):
        conn, (_, _, _, _, output_bits) = connected_conn
        conn.write_source(DataSource.eip("Output"), 0, bytearray(b"\x0F"))
        assert output_bits[0] is True
        assert output_bits[1] is True
        assert output_bits[2] is True
        assert output_bits[3] is True
        assert output_bits[4] is False
        for i in range(8, 32):
            assert output_bits[i] is False

    def test_write_output_offset(self, config, connected_conn):
        conn, (_, _, _, _, output_bits) = connected_conn
        conn.write_source(DataSource.eip("Output"), 2, bytearray(b"\x80"))
        assert output_bits[16] is False
        assert output_bits[23] is True

    def test_write_not_connected(self, config):
        conn = make_connection(config)
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.write_source(DataSource.eip("Output"), 0, bytearray(b"\x00"))

    def test_write_beyond_assembly_raises(self, config, connected_conn):
        conn, _ = connected_conn
        with pytest.raises(ValueError, match="exceeds assembly size"):
            conn.write_source(DataSource.eip("Output"), 3, bytearray(b"\x01\x02"))

    def test_write_assembly(self, config, connected_conn):
        conn, (_, _, _, _, output_bits) = connected_conn
        conn.write_source(DataSource.eip("Output"), 1, bytearray(b"\xFF"))
        assert output_bits[8:16] == [True] * 8


# --------------------------------------------------------------------------- bit conversion


class TestBitConversion:
    def test_bits_to_bytes_all_zeros(self):
        from s7pymon.eip import EIPConnection

        bits = [False] * 16
        result = EIPConnection._bits_to_bytes(bits, 0, 2)
        assert result == bytearray(b"\x00\x00")

    def test_bits_to_bytes_all_ones(self):
        from s7pymon.eip import EIPConnection

        bits = [True] * 16
        result = EIPConnection._bits_to_bytes(bits, 0, 2)
        assert result == bytearray(b"\xFF\xFF")

    def test_bits_to_bytes_with_offset(self):
        from s7pymon.eip import EIPConnection

        bits = [True] * 8 + [False] * 8 + [True] * 8
        result = EIPConnection._bits_to_bytes(bits, 1, 2)
        assert result == bytearray(b"\x00\xFF")

    def test_bytes_to_bits(self):
        from s7pymon.eip import EIPConnection

        bits = [False] * 24
        EIPConnection._write_bytes_to_bits(bits, 0, bytearray(b"\x0F\xF0"))
        assert bits[0] is True
        assert bits[3] is True
        assert bits[4] is False
        assert bits[8] is False
        assert bits[11] is False
        assert bits[12] is True

    def test_bytes_to_bits_with_offset(self):
        from s7pymon.eip import EIPConnection

        bits = [False] * 24
        EIPConnection._write_bytes_to_bits(bits, 1, bytearray(b"\xFF"))
        assert bits[0:8] == [False] * 8
        assert bits[8:16] == [True] * 8

    def test_round_trip(self):
        from s7pymon.eip import EIPConnection

        original = bytearray(b"\xAB\xCD\xEF")
        bits = [False] * 32
        EIPConnection._write_bytes_to_bits(bits, 0, original)
        result = EIPConnection._bits_to_bytes(bits, 0, 3)
        assert result == original


# --------------------------------------------------------------------------- source resolution


class TestSourceResolution:
    def test_resolve_input_by_name(self, config, connected_conn):
        conn, _ = connected_conn
        bits, size = conn._resolve(DataSource.eip("Input"))
        assert size == config.input_size

    def test_resolve_output_by_name(self, config, connected_conn):
        conn, _ = connected_conn
        bits, size = conn._resolve(DataSource.eip("Output"))
        assert size == config.output_size

    def test_resolve_by_numeric_instance(self, config, connected_conn):
        conn, _ = connected_conn
        bits, size = conn._resolve(DataSource(f"EIP.{config.input_assembly}"))
        assert size == config.input_size

    def test_resolve_unknown_raises(self, config, connected_conn):
        conn, _ = connected_conn
        with pytest.raises(ValueError, match="Unknown EIP assembly"):
            conn._resolve(DataSource("EIP.999"))
