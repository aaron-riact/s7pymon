"""Protocol connection abstraction for busfactor.

Defines the :class:`Connection` ABC that every protocol driver (S7, EIP, …)
must implement, along with shared types that are not protocol-specific.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from .errors import log_error


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ConnectionConfig:
    protocol: str = "s7"
    address: str = ""
    tcp_port: int = 102
    timeout_ms: int = 3000
    # S7-specific (harmless defaults for other protocols)
    rack: int = 0
    slot: int = 2
    # EIP-specific
    eip_port: int = 44818
    input_assembly: int = 101
    output_assembly: int = 100
    config_assembly: int = 102
    input_size: int = 32
    output_size: int = 32
    rpi_ms: int = 50
    # Modbus-specific
    slave_id: int = 1
    framer: str = "socket"  # "socket" = Modbus TCP, "rtu" = RTU frames over TCP
    retries: int = 3

    @property
    def display(self) -> str:
        if self.protocol == "s7":
            return f"{self.address}:{self.tcp_port} rack={self.rack} slot={self.slot}"
        if self.protocol == "eip":
            return (
                f"{self.address}:{self.tcp_port} "
                f"in={self.input_assembly} out={self.output_assembly} "
                f"rpi={self.rpi_ms}ms"
            )
        if self.protocol == "modbus":
            return (
                f"{self.address}:{self.tcp_port} "
                f"slave={self.slave_id} framer={self.framer}"
            )
        return f"{self.address}:{self.tcp_port}"


@dataclass(frozen=True)
class DataSource:
    """Identifies a readable/writable data source (DB, assembly, …).

    Each protocol connection parses the :attr:`value` string internally.
    Factory methods provide protocol-specific construction:

    >>> DataSource.s7_db(210)
    DataSource('DB210')
    >>> DataSource.eip("Input")
    DataSource('EIP.Input')
    >>> DataSource.modbus("Holding")
    DataSource('MB.Holding')
    >>> DataSource.s7_area("EB")
    DataSource('EB')
    """
    value: str

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def s7_db(number: int) -> DataSource:
        return DataSource(f"DB{number}")

    @staticmethod
    def s7_area(area: str) -> DataSource:
        return DataSource(area)

    @staticmethod
    def eip(name: str) -> DataSource:
        return DataSource(f"EIP.{name}")

    @staticmethod
    def modbus(table: str) -> DataSource:
        return DataSource(f"MB.{table}")


@dataclass
class ReadResult:
    data: bytearray
    source: DataSource
    start: int
    size: int
    timestamp: float = field(default_factory=time.monotonic)


class Connection(ABC):
    """Abstract protocol connection driver.

    Every protocol (S7, EIP, …) implements this so that :class:`MonitorEngine`
    and frontends can drive it without knowing which wire protocol is in use.

    The connection state machine lives here. A driver implements
    :meth:`_open` and :meth:`_close` for its protocol; :meth:`connect` and
    :meth:`disconnect` wrap them so every driver moves through the same
    states, records a failure the same way and re-raises it. Reads and
    writes stay with the driver, which knows what a failure means for its
    link: an S7 read error is a lost connection, a Modbus one is a dropped
    reply that the next poll retries.
    """

    protocol: str

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._error = ""
        self._lock = threading.Lock()

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def error(self) -> str:
        """Why the state is ERROR; empty otherwise."""
        return self._error

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    def connect(self) -> None:
        """Open the link, or record why it could not be opened and re-raise."""
        with self._lock:
            self._state = ConnectionState.CONNECTING
            self._error = ""
            try:
                self._open()
            except Exception as e:
                self._record_failure(
                    f"{self.protocol} connection to {self._config.address}:{self._config.tcp_port} failed", e
                )
                self._close_quietly()
                raise
            self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        with self._lock:
            self._close_quietly()
            self._state = ConnectionState.DISCONNECTED
            self._error = ""

    def _record_failure(self, what: str, error: Exception) -> None:
        """Log *error* and put the connection into the ERROR state."""
        log_error(f"{what}: {error}")
        self._state = ConnectionState.ERROR
        self._error = str(error)

    def _close_quietly(self) -> None:
        try:
            self._close()
        except Exception:
            pass

    @abstractmethod
    def _open(self) -> None:
        """Open the protocol link. Raise on failure; connect() records the state."""

    @abstractmethod
    def _close(self) -> None:
        """Release the protocol link. Also called after a failed open, so it must cope with a half-open one."""

    @abstractmethod
    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        ...

    @abstractmethod
    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        ...

    @property
    def status_extra(self) -> dict[str, str]:
        """Protocol-specific status info shown in the connection bar."""
        return {}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
