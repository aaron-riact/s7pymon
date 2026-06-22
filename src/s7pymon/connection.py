"""S7 PLC connection management for the monitor tool.

Provides a clean interface over python-snap7 for reading and writing
DB, EB, AB, MB, CT, and TM areas, with connection state tracking.
"""

from __future__ import annotations

import re
from threading import Lock
from typing import Protocol

import snap7

from .protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult
from .variable import S7Area

__all__ = [
    "Connection",
    "ConnectionConfig",
    "ConnectionState",
    "DataSource",
    "ReadResult",
    "S7ClientProtocol",
    "S7Connection",
]

_DB_SOURCE = re.compile(r"^DB(\d+)$")


def _parse_s7_source(source: DataSource) -> tuple[S7Area, int]:
    """Split a DataSource like ``DB210`` or ``EB`` into (area, db_number)."""
    m = _DB_SOURCE.match(source.value)
    if m:
        return S7Area.DB, int(m.group(1))
    return S7Area(source.value), 0


class S7ClientProtocol(Protocol):
    """Protocol for snap7 client to enable testing with mocks."""

    def set_param(self, param: int, value: int) -> int: ...
    def connect(self, address: str, rack: int, slot: int, tcp_port: int) -> int: ...
    def get_connected(self) -> bool: ...
    def disconnect(self) -> int: ...
    def db_read(self, db_number: int, start: int, size: int) -> bytearray: ...
    def db_write(self, db_number: int, start: int, data: bytearray) -> int: ...
    def eb_read(self, start: int, size: int) -> bytearray: ...
    def eb_write(self, start: int, size: int, data: bytearray) -> int: ...
    def ab_read(self, start: int, size: int) -> bytearray: ...
    def ab_write(self, start: int, data: bytearray) -> int: ...
    def mb_read(self, start: int, size: int) -> bytearray: ...
    def mb_write(self, start: int, size: int, data: bytearray) -> int: ...
    def ct_read(self, start: int, amount: int) -> bytearray: ...
    def ct_write(self, start: int, amount: int, data: bytearray) -> int: ...
    def tm_read(self, start: int, amount: int) -> bytearray: ...
    def tm_write(self, start: int, amount: int, data: bytearray) -> int: ...


class S7Connection(Connection):
    """Manages an S7 PLC connection with state tracking."""

    protocol = "s7"

    def __init__(self, config: ConnectionConfig, client: S7ClientProtocol | None = None):
        self._config = config
        self._client = client or snap7.Client()
        self._state = ConnectionState.DISCONNECTED
        self._error: str = ""
        self._lock = Lock()

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    def connect(self) -> None:
        """Establish connection to the S7 PLC."""
        with self._lock:
            self._state = ConnectionState.CONNECTING
            self._error = ""
            try:
                self._client.set_param(snap7.type.Parameter.SendTimeout, self._config.timeout_ms)
                self._client.set_param(snap7.type.Parameter.PingTimeout, self._config.timeout_ms)
                self._client.set_param(snap7.type.Parameter.RecvTimeout, self._config.timeout_ms)
                self._client.connect(
                    self._config.address,
                    self._config.rack,
                    self._config.slot,
                    tcp_port=self._config.tcp_port,
                )
                if not self._client.get_connected():
                    raise ConnectionError("connect() returned but get_connected() is False")
                self._state = ConnectionState.CONNECTED
            except Exception as e:
                self._state = ConnectionState.ERROR
                self._error = str(e)
                raise

    def disconnect(self) -> None:
        """Disconnect from the PLC."""
        with self._lock:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._state = ConnectionState.DISCONNECTED
            self._error = ""

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        """Read bytes from an S7 data source (DB210, EB, etc.)."""
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            try:
                area, db = _parse_s7_source(source)
                if area == S7Area.DB:
                    raw = self._client.db_read(db, offset, size)
                elif area == S7Area.EB:
                    raw = self._client.eb_read(offset, size)
                elif area == S7Area.AB:
                    raw = self._client.ab_read(offset, size)
                elif area == S7Area.MB:
                    raw = self._client.mb_read(offset, size)
                elif area == S7Area.CT:
                    raw = self._client.ct_read(offset, size)
                elif area == S7Area.TM:
                    raw = self._client.tm_read(offset, size)
                else:
                    raise ValueError(f"Unsupported S7 area: {area}")
                return ReadResult(
                    data=bytearray(raw),
                    source=source,
                    start=offset,
                    size=size,
                )
            except Exception as e:
                self._state = ConnectionState.ERROR
                self._error = str(e)
                raise

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        """Write bytes to an S7 data source (DB210, EB, etc.)."""
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            try:
                area, db = _parse_s7_source(source)
                if area == S7Area.DB:
                    self._client.db_write(db, offset, data)
                elif area == S7Area.EB:
                    self._client.eb_write(offset, len(data), data)
                elif area == S7Area.AB:
                    self._client.ab_write(offset, data)
                elif area == S7Area.MB:
                    self._client.mb_write(offset, len(data), data)
                elif area == S7Area.CT:
                    self._client.ct_write(offset, len(data), data)
                elif area == S7Area.TM:
                    self._client.tm_write(offset, len(data), data)
                else:
                    raise ValueError(f"Unsupported S7 area: {area}")
            except Exception as e:
                self._state = ConnectionState.ERROR
                self._error = str(e)
                raise
