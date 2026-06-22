"""Protocol connection abstraction for s7pymon.

Defines the :class:`Connection` ABC that every protocol driver (S7, EIP, …)
must implement, along with shared types that are not protocol-specific.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


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
    """

    protocol: str

    @property
    @abstractmethod
    def state(self) -> ConnectionState:
        ...

    @property
    @abstractmethod
    def connected(self) -> bool:
        ...

    @property
    @abstractmethod
    def config(self) -> ConnectionConfig:
        ...

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        ...

    @abstractmethod
    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
