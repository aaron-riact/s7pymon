"""Modbus connection driver.

Wraps ``pymodbus`` behind the :class:`Connection` ABC so :class:`MonitorEngine`
can poll Modbus tables as ``DataSource`` values.

The rest of busfactor addresses everything in bytes.  Modbus addresses
registers (16 bits) and bits (1 bit), so this module converts:

* ``Holding`` / ``Input`` -- byte offset ``n`` lives in register ``n // 2``,
  high byte first.
* ``Coil`` / ``Discrete`` -- byte offset ``n`` holds bits ``n * 8`` to
  ``n * 8 + 7``, least significant bit first.

Framing is chosen with ``framer``: ``socket`` for ordinary Modbus TCP, ``rtu``
for RTU frames carried over a TCP stream.  Serial gateways that expose a raw
byte pipe -- the Dobot flange bus on port 60000, or socat in front of an RS485
adapter -- need ``rtu``; they forward bytes and never add an MBAP header.
"""

from __future__ import annotations

import logging
import re
import threading

from .errors import log_error
from .protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult

log = logging.getLogger(__name__)

_MODBUS_SOURCE = re.compile(r"^MB\.(Holding|Input|Coil|Discrete)$", re.IGNORECASE)

# Modbus caps one request's payload. A read reply carries a single byte count,
# and a write request carries its own, so writes fit two fewer registers.
MAX_REGISTERS_PER_READ = 125
MAX_REGISTERS_PER_WRITE = 123
MAX_BITS_PER_READ = 2000
MAX_BITS_PER_WRITE = 1968

_REGISTER_TABLES = ("holding", "input")
_BIT_TABLES = ("coil", "discrete")


def registers_to_bytes(registers: list[int]) -> bytearray:
    """Pack 16-bit registers into bytes, high byte first."""
    out = bytearray(len(registers) * 2)
    for i, reg in enumerate(registers):
        out[i * 2] = (reg >> 8) & 0xFF
        out[i * 2 + 1] = reg & 0xFF
    return out


def bytes_to_registers(data: bytes | bytearray) -> list[int]:
    """Unpack bytes into 16-bit registers, high byte first."""
    if len(data) % 2:
        raise ValueError(f"Need a whole number of registers, got {len(data)} bytes")
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]


def bits_to_bytes(bits: list[bool], count: int) -> bytearray:
    """Pack bits into bytes, least significant bit first."""
    out = bytearray(count)
    for i in range(min(len(bits), count * 8)):
        if bits[i]:
            out[i >> 3] |= 1 << (i & 7)
    return out


def bytes_to_bits(data: bytes | bytearray) -> list[bool]:
    """Unpack bytes into bits, least significant bit first."""
    return [bool(data[i >> 3] & (1 << (i & 7))) for i in range(len(data) * 8)]


class ModbusConnection(Connection):
    """Manages a Modbus TCP or RTU-over-TCP connection with state tracking."""

    protocol = "modbus"

    def __init__(self, config: ConnectionConfig, client_factory=None):
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._error: str = ""
        self._lock = threading.Lock()
        self._client = None
        self._client_factory = client_factory

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

    @property
    def status_extra(self) -> dict[str, str]:
        if not self.connected:
            return {}
        return {"Slave": str(self._config.slave_id), "Framer": self._config.framer}

    def _build_client(self):
        from pymodbus import FramerType
        from pymodbus.client import ModbusTcpClient

        framers = {
            "socket": FramerType.SOCKET,
            "tcp": FramerType.SOCKET,
            "rtu": FramerType.RTU,
            "ascii": FramerType.ASCII,
        }
        name = self._config.framer.lower()
        if name not in framers:
            raise ValueError(
                f"Unknown Modbus framer {self._config.framer!r}. "
                f"Expected one of: {', '.join(sorted(set(framers)))}"
            )
        return ModbusTcpClient(
            self._config.address,
            port=self._config.tcp_port,
            framer=framers[name],
            timeout=self._config.timeout_ms / 1000.0,
        )

    def connect(self) -> None:
        with self._lock:
            self._state = ConnectionState.CONNECTING
            self._error = ""
            log.debug(
                "Connecting to %s:%s framer=%s slave=%s ...",
                self._config.address, self._config.tcp_port, self._config.framer, self._config.slave_id,
            )
            try:
                factory = self._client_factory or self._build_client
                client = factory()
                if not client.connect():
                    raise ConnectionError(
                        f"Could not open {self._config.address}:{self._config.tcp_port}"
                    )
                self._client = client
                self._state = ConnectionState.CONNECTED
                log.debug("Connected OK")
            except ImportError:
                self._state = ConnectionState.ERROR
                self._error = "pymodbus library not available"
                raise ConnectionError("pymodbus library not available") from None
            except Exception as e:
                log_error(f"Modbus connection failed: {e}")
                self._state = ConnectionState.ERROR
                self._error = str(e)
                self._cleanup()
                raise

    def disconnect(self) -> None:
        log.debug("Disconnecting ...")
        with self._lock:
            self._cleanup()
            self._state = ConnectionState.DISCONNECTED
            self._error = ""
            log.debug("Disconnected")

    def read_source(self, source: DataSource, offset: int, size: int) -> ReadResult:
        log.debug("read_source(%s, offset=%s, size=%s)", source, offset, size)
        with self._lock:
            table = self._resolve(source)
            if size <= 0:
                raise ValueError(f"Read {source} size must be positive, got {size}")
            if table in _REGISTER_TABLES:
                data = self._read_registers(table, offset, size)
            else:
                data = self._read_bits(table, offset, size)
            return ReadResult(data=data, source=source, start=offset, size=size)

    def write_source(self, source: DataSource, offset: int, data: bytearray) -> None:
        log.debug("write_source(%s, offset=%s, len=%s)", source, offset, len(data))
        with self._lock:
            table = self._resolve(source)
            if table == "input":
                raise ValueError("Input registers are read-only")
            if table == "discrete":
                raise ValueError("Discrete inputs are read-only")
            if not data:
                return
            if table == "holding":
                self._write_registers(offset, data)
            else:
                self._write_bits(offset, data)

    # ------------------------------------------------------------ registers

    def _read_registers(self, table: str, offset: int, size: int) -> bytearray:
        """Read a byte range out of a register table.

        A byte range rarely lands on register boundaries, so read whole
        registers either side and slice the result.
        """
        first = offset // 2
        last = (offset + size - 1) // 2
        count = last - first + 1
        raw = bytearray()
        for start, chunk in _chunks(first, count, MAX_REGISTERS_PER_READ):
            reader = (
                self._client_read_holding if table == "holding" else self._client_read_input
            )
            raw += registers_to_bytes(reader(start, chunk))
        lead = offset - first * 2
        return raw[lead:lead + size]

    def _write_registers(self, offset: int, data: bytearray) -> None:
        """Write a byte range into holding registers.

        Modbus writes whole registers, so a range that starts or ends mid
        register is read back first and the untouched half preserved.
        """
        first = offset // 2
        last = (offset + len(data) - 1) // 2
        lead = offset - first * 2
        trail = (last * 2 + 1) - (offset + len(data) - 1)
        block = bytearray(data)
        if lead:
            block = self._read_registers("holding", first * 2, lead) + block
        if trail:
            block = block + self._read_registers("holding", offset + len(data), trail)
        registers = bytes_to_registers(block)
        for start, chunk in _chunks(first, len(registers), MAX_REGISTERS_PER_WRITE):
            self._client_write_registers(start, registers[start - first:start - first + chunk])

    # ----------------------------------------------------------------- bits

    def _read_bits(self, table: str, offset: int, size: int) -> bytearray:
        first = offset * 8
        count = size * 8
        bits: list[bool] = []
        for start, chunk in _chunks(first, count, MAX_BITS_PER_READ):
            reader = self._client_read_coils if table == "coil" else self._client_read_discrete
            bits += reader(start, chunk)
        return bits_to_bytes(bits, size)

    def _write_bits(self, offset: int, data: bytearray) -> None:
        first = offset * 8
        bits = bytes_to_bits(data)
        for start, chunk in _chunks(first, len(bits), MAX_BITS_PER_WRITE):
            self._client_write_coils(start, bits[start - first:start - first + chunk])

    # --------------------------------------------------------- client calls

    def _require_client(self):
        if not self.connected or self._client is None:
            raise ConnectionError("Not connected")
        return self._client

    def _check(self, response, what: str):
        if response is None or (hasattr(response, "isError") and response.isError()):
            raise ConnectionError(f"Modbus {what} failed: {response}")
        return response

    def _client_read_holding(self, address: int, count: int) -> list[int]:
        client = self._require_client()
        resp = client.read_holding_registers(
            address, count=count, device_id=self._config.slave_id
        )
        return list(self._check(resp, f"read holding {address}+{count}").registers)

    def _client_read_input(self, address: int, count: int) -> list[int]:
        client = self._require_client()
        resp = client.read_input_registers(
            address, count=count, device_id=self._config.slave_id
        )
        return list(self._check(resp, f"read input {address}+{count}").registers)

    def _client_read_coils(self, address: int, count: int) -> list[bool]:
        client = self._require_client()
        resp = client.read_coils(address, count=count, device_id=self._config.slave_id)
        return list(self._check(resp, f"read coils {address}+{count}").bits)[:count]

    def _client_read_discrete(self, address: int, count: int) -> list[bool]:
        client = self._require_client()
        resp = client.read_discrete_inputs(
            address, count=count, device_id=self._config.slave_id
        )
        return list(self._check(resp, f"read discrete {address}+{count}").bits)[:count]

    def _client_write_registers(self, address: int, values: list[int]) -> None:
        client = self._require_client()
        resp = client.write_registers(address, values, device_id=self._config.slave_id)
        self._check(resp, f"write registers {address}+{len(values)}")

    def _client_write_coils(self, address: int, values: list[bool]) -> None:
        client = self._require_client()
        resp = client.write_coils(address, values, device_id=self._config.slave_id)
        self._check(resp, f"write coils {address}+{len(values)}")

    # ---------------------------------------------------------------- misc

    @staticmethod
    def _resolve(source: DataSource) -> str:
        m = _MODBUS_SOURCE.match(source.value)
        if not m:
            raise ValueError(f"Invalid Modbus source: {source.value}")
        return m.group(1).lower()

    def _cleanup(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None


def _chunks(start: int, total: int, limit: int):
    """Split a run into (start, count) pieces no larger than ``limit``."""
    done = 0
    while done < total:
        take = min(limit, total - done)
        yield start + done, take
        done += take
