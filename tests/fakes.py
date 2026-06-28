"""Shared fake implementations for tests."""

from collections.abc import Hashable

from busfactor.protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult
from busfactor.variable import S7Area


class BaseFakeConnection(Connection):
    """Shared fake connection driven by a dict of area buffers.

    Subclasses can override ``_buffer_key(source)`` to change how
    buffer lookups are keyed.  The default implementation parses a
    ``DataSource`` into an ``(area, db)`` tuple.
    """

    def __init__(self, buffers: dict | None = None, *, address: str = "10.0.0.5"):
        self._config = ConnectionConfig(address=address)
        self._state = ConnectionState.CONNECTED
        self._buffers: dict[Hashable, bytearray] = buffers or {}
        self.writes: list[tuple] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.read_error: Exception | None = None

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
        self.connect_calls += 1
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._state = ConnectionState.DISCONNECTED

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        if self.read_error is not None:
            self._state = ConnectionState.ERROR
            raise self.read_error
        key = self._buffer_key(source)
        buf = self._buffers.get(key, bytearray(64))
        return ReadResult(
            data=bytearray(buf[offset : offset + size]),
            source=source, start=offset, size=size,
        )

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        self.writes.append((source, offset, bytes(data)))
        key = self._buffer_key(source)
        buf = self._buffers.setdefault(key, bytearray(64))
        buf[offset : offset + len(data)] = data

    def _buffer_key(self, source: DataSource) -> Hashable:
        if source.value.startswith("DB"):
            return S7Area.DB, int(source.value[2:])
        if source.value.startswith("EIP."):
            return source.value, 0
        return S7Area(source.value), 0
