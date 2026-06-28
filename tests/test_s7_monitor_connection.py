from unittest.mock import MagicMock
import pytest

from busfactor.connection import (
    ConnectionConfig,
    ConnectionState,
    ReadResult,
    S7Connection,
)
from busfactor.protocols import DataSource
from busfactor.variable import S7Area


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_connected.return_value = True
    client.connect.return_value = 0
    client.disconnect.return_value = 0
    client.db_read.return_value = bytearray(b"\x00" * 18)
    client.db_write.return_value = 0
    return client


@pytest.fixture
def config():
    return ConnectionConfig(address="192.168.1.100", rack=0, slot=2, tcp_port=102)


@pytest.fixture
def connection(config, mock_client):
    return S7Connection(config, client=mock_client)


class TestConnectionConfig:
    def test_defaults(self):
        cfg = ConnectionConfig(address="10.0.0.1")
        assert cfg.rack == 0
        assert cfg.slot == 2
        assert cfg.tcp_port == 102
        assert cfg.timeout_ms == 3000

    def test_display(self):
        cfg = ConnectionConfig(address="10.0.0.1", tcp_port=1102)
        assert cfg.display == "10.0.0.1:1102 rack=0 slot=2"


class TestS7Connection:
    def test_initial_state(self, connection):
        assert connection.state == ConnectionState.DISCONNECTED
        assert not connection.connected
        assert connection.error == ""

    def test_connect_success(self, connection, mock_client):
        connection.connect()
        assert connection.state == ConnectionState.CONNECTED
        assert connection.connected
        mock_client.connect.assert_called_once_with(
            "192.168.1.100", 0, 2, tcp_port=102
        )

    def test_connect_failure(self, connection, mock_client):
        mock_client.connect.side_effect = ConnectionError("refused")
        with pytest.raises(ConnectionError):
            connection.connect()
        assert connection.state == ConnectionState.ERROR
        assert "refused" in connection.error

    def test_connect_but_not_connected(self, connection, mock_client):
        mock_client.get_connected.return_value = False
        with pytest.raises(ConnectionError, match="get_connected"):
            connection.connect()
        assert connection.state == ConnectionState.ERROR

    def test_disconnect(self, connection):
        connection.connect()
        connection.disconnect()
        assert connection.state == ConnectionState.DISCONNECTED
        assert connection.error == ""

    def test_context_manager(self, connection, mock_client):
        with connection:
            assert connection.connected
        assert connection.state == ConnectionState.DISCONNECTED

    def test_context_manager_on_error(self, connection, mock_client):
        mock_client.connect.side_effect = ConnectionError("fail")
        with pytest.raises(ConnectionError):
            with connection:
                pass

    def test_config_accessible(self, connection, config):
        assert connection.config is config

    def test_timeout_params_set(self, connection, mock_client):
        connection.connect()
        assert mock_client.set_param.call_count == 3


class TestS7ConnectionReadSource:
    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_connected.return_value = True
        client.connect.return_value = 0
        client.db_read.return_value = bytearray(b"\x01\x02\x03")
        client.eb_read.return_value = bytearray(b"\xAA\xBB")
        client.ab_read.return_value = bytearray(b"\xCC\xDD")
        client.mb_read.return_value = bytearray(b"\xEE\xFF")
        return client

    @pytest.fixture
    def connection(self, mock_client):
        config = ConnectionConfig(address="10.0.0.1")
        conn = S7Connection(config, client=mock_client)
        conn.connect()
        return conn

    def test_read_db_source(self, connection, mock_client):
        result = connection.read_source(DataSource.s7_db(210), 0, 3)
        assert isinstance(result, ReadResult)
        assert result.data == bytearray(b"\x01\x02\x03")
        assert result.source == DataSource("DB210")
        assert result.start == 0
        assert result.size == 3
        mock_client.db_read.assert_called_once_with(210, 0, 3)

    def test_read_eb_source(self, connection, mock_client):
        result = connection.read_source(DataSource.s7_area("EB"), 0, 2)
        assert result.data == bytearray(b"\xAA\xBB")
        assert result.source == DataSource("EB")
        mock_client.eb_read.assert_called_once_with(0, 2)

    def test_read_ab_source(self, connection, mock_client):
        result = connection.read_source(DataSource.s7_area("AB"), 0, 2)
        assert result.data == bytearray(b"\xCC\xDD")
        assert result.source == DataSource("AB")
        mock_client.ab_read.assert_called_once_with(0, 2)

    def test_read_mb_source(self, connection, mock_client):
        result = connection.read_source(DataSource.s7_area("MB"), 0, 2)
        mock_client.mb_read.assert_called_once_with(0, 2)

    def test_read_not_connected(self, mock_client):
        config = ConnectionConfig(address="10.0.0.1")
        conn = S7Connection(config, client=mock_client)
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.read_source(DataSource("EB"), 0, 2)

    def test_read_error_sets_state(self, connection, mock_client):
        mock_client.db_read.side_effect = RuntimeError("comm error")
        with pytest.raises(RuntimeError):
            connection.read_source(DataSource("DB210"), 0, 18)
        assert connection.state == ConnectionState.ERROR


class TestS7ConnectionWriteSource:
    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_connected.return_value = True
        client.connect.return_value = 0
        client.db_write.return_value = 0
        return client

    @pytest.fixture
    def connection(self, mock_client):
        config = ConnectionConfig(address="10.0.0.1")
        conn = S7Connection(config, client=mock_client)
        conn.connect()
        return conn

    def test_write_db_source(self, connection, mock_client):
        connection.write_source(DataSource.s7_db(210), 0, bytearray(b"\xff"))
        mock_client.db_write.assert_called_once_with(210, 0, bytearray(b"\xff"))

    def test_write_eb_source(self, connection, mock_client):
        connection.write_source(DataSource.s7_area("EB"), 0, bytearray(b"\x01"))
        mock_client.eb_write.assert_called_once_with(0, 1, bytearray(b"\x01"))

    def test_write_ab_source(self, connection, mock_client):
        connection.write_source(DataSource.s7_area("AB"), 0, bytearray(b"\x01"))
        mock_client.ab_write.assert_called_once_with(0, bytearray(b"\x01"))

    def test_write_not_connected(self, mock_client):
        config = ConnectionConfig(address="10.0.0.1")
        conn = S7Connection(config, client=mock_client)
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.write_source(DataSource("DB210"), 0, bytearray(b"\xff"))

    def test_write_error_sets_state(self, connection, mock_client):
        mock_client.db_write.side_effect = RuntimeError("write fail")
        with pytest.raises(RuntimeError):
            connection.write_source(DataSource("DB210"), 0, bytearray(b"\xff"))
        assert connection.state == ConnectionState.ERROR
