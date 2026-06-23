"""Ethernet/IP scanner (originator) connection driver.

Wraps the ``python-ethernetip`` library behind the :class:`Connection` ABC
so :class:`MonitorEngine` can poll EIP assemblies as ``DataSource`` values.
"""

from __future__ import annotations

import re
import threading

from .errors import log_error
from .protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult

_EIP_SOURCE = re.compile(r"^EIP\.(Input|Output|Config|\d+)$", re.IGNORECASE)


class EIPConnection(Connection):
    """Manages an EtherNet/IP scanner connection with state tracking."""

    protocol = "eip"

    def __init__(self, config: ConnectionConfig):
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._error: str = ""
        self._lock = threading.Lock()
        self._eip = None  # ethernetip.EtherNetIP
        self._conn = None  # ethernetip.EtherNetIPExpConnection
        self._input_bits: list[bool] = []
        self._output_bits: list[bool] = []
        self._input_size: int = 0
        self._output_size: int = 0

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def error(self) -> str:
        return self._error

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    def connect(self) -> None:
        with self._lock:
            self._state = ConnectionState.CONNECTING
            self._error = ""
            try:
                import ethernetip

                eip = ethernetip.EtherNetIP(self._config.address)
                conn = eip.explicit_conn()
                conn.registerSession()

                self._input_size = self._config.input_size
                self._output_size = self._config.output_size

                input_bits = eip.registerAssembly(
                    ethernetip.EtherNetIP.ENIP_IO_TYPE_INPUT,
                    self._input_size,
                    self._config.input_assembly,
                    conn,
                )
                output_bits = eip.registerAssembly(
                    ethernetip.EtherNetIP.ENIP_IO_TYPE_OUTPUT,
                    self._output_size,
                    self._config.output_assembly,
                    conn,
                )

                eip.startIO(udp_port=0)
                result = conn.sendFwdOpenReq(
                    inputinst=self._config.input_assembly,
                    outputinst=self._config.output_assembly,
                    configinst=self._config.config_assembly,
                    torpi=self._config.rpi_ms,
                    otrpi=self._config.rpi_ms,
                    originator_udp_port=eip.originator_udp_port,
                )
                if result != 0:
                    raise ConnectionError(
                        f"Forward Open failed with code {result}"
                    )
                conn.produce()

                self._eip = eip
                self._conn = conn
                self._input_bits = input_bits
                self._output_bits = output_bits
                self._state = ConnectionState.CONNECTED
            except ImportError:
                self._state = ConnectionState.ERROR
                self._error = "ethernetip library not available"
                raise ConnectionError("ethernetip library not available") from None
            except Exception as e:
                log_error(f"EIP connection failed: {e}")
                self._state = ConnectionState.ERROR
                self._error = str(e)
                self._cleanup()
                raise

    def disconnect(self) -> None:
        with self._lock:
            self._cleanup()
            self._state = ConnectionState.DISCONNECTED
            self._error = ""

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            bits, asm_size = self._resolve(source)
            if offset + size > asm_size:
                raise ValueError(
                    f"Read {source} offset {offset} size {size} "
                    f"exceeds assembly size {asm_size}"
                )
            data = self._bits_to_bytes(bits, offset, size)
            return ReadResult(
                data=data,
                source=source,
                start=offset,
                size=size,
            )

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            bits, asm_size = self._resolve(source)
            if offset + len(data) > asm_size:
                raise ValueError(
                    f"Write {source} offset {offset} size {len(data)} "
                    f"exceeds assembly size {asm_size}"
                )
            self._write_bytes_to_bits(bits, offset, data)

    def _resolve(self, source: DataSource) -> tuple[list[bool], int]:
        """Resolve a DataSource to (bit_list, assembly_size_bytes)."""
        m = _EIP_SOURCE.match(source.value)
        if not m:
            raise ValueError(f"Invalid EIP source: {source.value}")
        name = m.group(1).lower()
        if name in ("input", str(self._config.input_assembly)):
            return self._input_bits, self._input_size
        if name in ("output", str(self._config.output_assembly)):
            return self._output_bits, self._output_size
        if name in ("config", str(self._config.config_assembly)):
            raise ValueError("Config assembly not yet supported")
        raise ValueError(f"Unknown EIP assembly: {source.value}")

    @staticmethod
    def _bits_to_bytes(
        bits: list[bool], byte_offset: int, count: int
    ) -> bytearray:
        """Extract bytes from a LSB-first bit list at a byte offset."""
        result = bytearray(count)
        start = byte_offset * 8
        for i in range(count * 8):
            idx = start + i
            if idx < len(bits) and bits[idx]:
                result[i >> 3] |= 1 << (i & 7)
        return result

    @staticmethod
    def _write_bytes_to_bits(
        bits: list[bool], byte_offset: int, data: bytes
    ) -> None:
        """Write bytes into a LSB-first bit list at a byte offset."""
        start = byte_offset * 8
        for i in range(len(data) * 8):
            idx = start + i
            if idx < len(bits):
                bits[idx] = bool(data[i >> 3] & (1 << (i & 7)))

    def _cleanup(self) -> None:
        if self._conn is not None:
            try:
                self._conn.stopProduce()
            except Exception:
                pass
            try:
                self._conn.sendFwdCloseReq(
                    self._config.input_assembly,
                    self._config.output_assembly,
                    self._config.config_assembly,
                )
            except Exception:
                pass
            try:
                self._conn.unregisterSession()
            except Exception:
                pass
        if self._eip is not None:
            try:
                self._eip.stopIO()
            except Exception:
                pass
        self._eip = None
        self._conn = None
        self._input_bits = []
        self._output_bits = []
        self._input_size = 0
        self._output_size = 0
