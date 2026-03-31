"""S7 PLC connection management for the monitor tool.

Provides a clean interface over python-snap7 for reading and writing
DB areas, with connection state tracking and reconnection support.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Protocol

import snap7


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ConnectionConfig:
    address: str
    rack: int = 0
    slot: int = 2
    tcp_port: int = 102
    timeout_ms: int = 3000

    @property
    def display(self) -> str:
        return f"{self.address}:{self.tcp_port} rack={self.rack} slot={self.slot}"


@dataclass
class ReadResult:
    data: bytearray
    db: int
    start: int
    size: int
    timestamp: float = field(default_factory=time.monotonic)


class S7ClientProtocol(Protocol):
    """Protocol for snap7 client to enable testing with mocks."""

    def set_param(self, param: int, value: int) -> int: ...
    def connect(self, address: str, rack: int, slot: int, tcp_port: int) -> int: ...
    def get_connected(self) -> bool: ...
    def disconnect(self) -> int: ...
    def db_read(self, db_number: int, start: int, size: int) -> bytearray: ...
    def db_write(self, db_number: int, start: int, data: bytearray) -> int: ...


class S7Connection:
    """Manages an S7 PLC connection with state tracking."""

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

    def db_read(self, db: int, start: int, size: int) -> ReadResult:
        """Read a range of bytes from a DB."""
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            try:
                data = self._client.db_read(db, start, size)
                return ReadResult(
                    data=bytearray(data),
                    db=db,
                    start=start,
                    size=size,
                )
            except Exception as e:
                self._state = ConnectionState.ERROR
                self._error = str(e)
                raise

    def db_write(self, db: int, start: int, data: bytearray) -> None:
        """Write bytes to a DB."""
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            try:
                self._client.db_write(db, start, data)
            except Exception as e:
                self._state = ConnectionState.ERROR
                self._error = str(e)
                raise

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
