"""S7 variable specification parsing and value conversion.

Supports variable specs like Sharp7.Monitor format:
  DB200.Byte0     - unsigned byte at offset 0 of data block 200
  DB200.Int4      - signed 16-bit integer at offset 4
  DB200.DInt8     - signed 32-bit integer at offset 8
  DB200.Word2     - unsigned 16-bit integer at offset 2
  DB200.DWord6    - unsigned 32-bit integer at offset 6
  DB200.Real12    - 32-bit float at offset 12
  DB200.Bit0.3    - bit 3 of byte at offset 0
  DB200.String50.20 - string at offset 50, max length 20

Also supports S7 area addressing:
  EB.Byte0        - process image input byte at offset 0
  AB.Byte2        - process image output byte at offset 2
  MB.Byte0        - merker/flag byte at offset 0
  CT.Word0        - counter at offset 0
  TM.Word0        - timer at offset 0
"""

from __future__ import annotations

import re
import struct
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Union

from .protocols import Connection, DataSource


class S7Area(Enum):
    """S7 PLC memory area types."""

    DB = "DB"    # Data Blocks
    EB = "EB"    # Process Image Input  (Eingangsbereich / PE)
    AB = "AB"    # Process Image Output (Ausgangsbereich / PA)
    MB = "MB"    # Merkers / Flags      (Merkerebereich / MK)
    CT = "CT"    # Counters
    TM = "TM"    # Timers

    @property
    def description(self) -> str:
        return _AREA_DESCRIPTIONS[self]


_AREA_DESCRIPTIONS: dict[S7Area, str] = {
    S7Area.DB: "Data Block",
    S7Area.EB: "Process Input",
    S7Area.AB: "Process Output",
    S7Area.MB: "Merker/Flag",
    S7Area.CT: "Counter",
    S7Area.TM: "Timer",
}


class ByteOrder(Enum):
    BIG = "big"
    LITTLE = "little"


class DataType(Enum):
    BYTE = "Byte"
    INT = "Int"
    DINT = "DInt"
    WORD = "Word"
    DWORD = "DWord"
    REAL = "Real"
    BIT = "Bit"
    STRING = "String"
    CHARS = "Chars"

    @property
    def byte_size(self) -> int:
        return _TYPE_SIZES[self]

    @property
    def struct_format(self) -> str | None:
        return _TYPE_FORMATS.get(self)


_TYPE_SIZES: dict[DataType, int] = {
    DataType.BYTE: 1,
    DataType.INT: 2,
    DataType.DINT: 4,
    DataType.WORD: 2,
    DataType.DWORD: 4,
    DataType.REAL: 4,
    DataType.BIT: 1,
    DataType.STRING: 0,  # variable, determined by extra param
    DataType.CHARS: 0,   # variable, determined by extra param
}

# Big-endian struct formats (S7 is big-endian)
_TYPE_FORMATS: dict[DataType, str] = {
    DataType.BYTE: ">B",
    DataType.INT: ">h",
    DataType.DINT: ">i",
    DataType.WORD: ">H",
    DataType.DWORD: ">I",
    DataType.REAL: ">f",
}


def _struct_format(data_type: DataType, byte_order: ByteOrder) -> str:
    fmt = _TYPE_FORMATS[data_type]
    prefix = ">" if byte_order == ByteOrder.BIG else "<"
    return prefix + fmt[1:]


S7Type = DataType
"Deprecated alias — use DataType."

# Pattern: DB<num>.<Type><offset>[.<extra>]
_DB_VAR_PATTERN = re.compile(
    r"^DB(\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String|Chars)(\d+)(?:\.([0-9a-fA-F]+))?$",
    re.IGNORECASE,
)

# Pattern: <Area>.<Type><offset>[.<extra>]  (for EB, AB, MB, CT, TM)
_AREA_VAR_PATTERN = re.compile(
    r"^(EB|AB|MB|CT|TM)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String|Chars)(\d+)(?:\.([0-9a-fA-F]+))?$",
    re.IGNORECASE,
)

# Pattern: EIP.<Assembly>.<Type><offset>[.<extra>]
_EIP_VAR_PATTERN = re.compile(
    r"^EIP\.(Input|Output|Config|\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String|Chars)"
    r"(\d+)(?:\.([0-9a-fA-F]+))?$",
    re.IGNORECASE,
)

# Pattern: MB.<Table>.<Type><offset>[.<extra>]
# Offsets are byte offsets, as everywhere else; register 268 is byte 536.
# Profiles address registers by number through ``field_vars``.
_MODBUS_VAR_PATTERN = re.compile(
    r"^MB\.(Holding|Input|Coil|Discrete)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String|Chars)"
    r"(\d+)(?:\.([0-9a-fA-F]+))?$",
    re.IGNORECASE,
)

_MODBUS_TABLES = {t.lower(): t for t in ("Holding", "Input", "Coil", "Discrete")}


# ---------------------------------------------------------------- shared helpers


def _decode_value(
    data: bytes | bytearray,
    data_type: DataType,
    extra: int | None,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> Union[int, float, bool, str]:
    if len(data) < data_type.byte_size and data_type not in (DataType.STRING, DataType.BIT, DataType.CHARS):
        raise ValueError(f"Need {data_type.byte_size} bytes to decode, got {len(data)}")
    raw = data if data_type in (DataType.STRING, DataType.CHARS) else data[:data_type.byte_size]

    if data_type == DataType.BIT:
        assert extra is not None
        return bool(raw[0] & (1 << extra))

    if data_type == DataType.STRING:
        if len(raw) < 2:
            return ""
        actual_len = raw[1]
        return raw[2 : 2 + actual_len].decode("ascii", errors="replace")

    if data_type == DataType.CHARS:
        return raw.rstrip(b"\x00").decode("ascii", errors="replace")

    if data_type in (DataType.WORD, DataType.DWORD) and extra is not None:
        register = struct.unpack(_struct_format(data_type, byte_order), raw)[0]
        return bool(register & (1 << extra))

    return struct.unpack(_struct_format(data_type, byte_order), raw)[0]


def _encode_value(
    data_type: DataType,
    extra: int | None,
    value: Union[int, float, bool, str],
    byte_order: ByteOrder = ByteOrder.BIG,
) -> bytearray:
    if data_type == DataType.BIT:
        raise ValueError("Cannot encode full byte for Bit type; use encode_bit() instead")

    if data_type in (DataType.WORD, DataType.DWORD) and extra is not None:
        raise ValueError(f"Cannot encode whole register for bit-addressed {data_type.value}; use read-modify-write instead")

    if data_type == DataType.STRING:
        assert extra is not None
        s = str(value)
        max_len = extra
        s = s[:max_len]
        buf = bytearray(max_len + 2)
        buf[0] = max_len
        buf[1] = len(s)
        buf[2 : 2 + len(s)] = s.encode("ascii", errors="replace")
        return buf

    if data_type == DataType.CHARS:
        assert extra is not None
        buf = bytearray(extra)
        encoded = str(value).encode("ascii", errors="replace")[:extra]
        buf[:len(encoded)] = encoded
        return buf

    coerced = float(value) if data_type == DataType.REAL else int(value)
    return bytearray(struct.pack(_struct_format(data_type, byte_order), coerced))


def _encode_bit_value(extra: int, current_byte: int, value: bool) -> bytearray:
    if value:
        result = current_byte | (1 << extra)
    else:
        result = current_byte & ~(1 << extra)
    return bytearray([result])


def _format_value(data_type: DataType, value: Union[int, float, bool, str]) -> str:
    if data_type == DataType.BIT:
        return "1" if value else "0"
    if data_type == DataType.REAL:
        return f"{value:.4f}"
    if data_type in (DataType.STRING, DataType.CHARS):
        return repr(value)
    return str(value)


def _parse_bit_text(text: str) -> bool:
    text = text.strip()
    if text.lower() in ("1", "true", "on", "yes"):
        return True
    if text.lower() in ("0", "false", "off", "no"):
        return False
    raise ValueError(f"Invalid bit value: {text!r}")


def _parse_input(data_type: DataType, text: str) -> Union[int, float, bool, str]:
    text = text.strip()
    if data_type == DataType.BIT:
        return _parse_bit_text(text)
    if data_type == DataType.REAL:
        return float(text)
    if data_type in (DataType.STRING, DataType.CHARS):
        return text
    if text.startswith("0x") or text.startswith("0X"):
        return int(text, 16)
    return int(text)


def _parse_extra(extra_str: str | None, data_type: DataType) -> int | None:
    if extra_str is None:
        return None
    if data_type in (DataType.WORD, DataType.DWORD):
        if any(c in extra_str for c in "abcdefABCDEF"):
            return int(extra_str, 16)
        return int(extra_str, 10)
    return int(extra_str, 10)


def _validate_type(extra: int | None, data_type: DataType, spec: str) -> None:
    if data_type == DataType.BIT:
        if extra is None:
            raise ValueError(f"Bit variable requires bit number: {spec} (e.g. DB200.Bit0.3)")
        if not 0 <= extra <= 7:
            raise ValueError(f"Bit number must be 0-7, got {extra} in {spec}")
    if data_type in (DataType.STRING, DataType.CHARS) and extra is None:
        type_name = data_type.value
        raise ValueError(f"{type_name} variable requires max length: {spec} (e.g. DB200.{type_name}50.20)")
    if data_type == DataType.WORD and extra is not None and not 0 <= extra <= 15:
        raise ValueError(f"Word bit must be 0-15 (0x0-0xf), got {extra} in {spec}")
    if data_type == DataType.DWORD and extra is not None and not 0 <= extra <= 31:
        raise ValueError(f"DWord bit must be 0-31 (0x0-0x1f), got {extra} in {spec}")


_type_map: dict[str, DataType] = {str(t.value).lower(): t for t in DataType}


def _parse_type_name(type_name: str) -> DataType:
    return _type_map[type_name.lower()]


_BYTE_ORDER_SUFFIXES = {
    ".le": ByteOrder.LITTLE,
    ".be": ByteOrder.BIG,
    ".little": ByteOrder.LITTLE,
    ".big": ByteOrder.BIG,
}


def _strip_byte_order_suffix(spec: str) -> tuple[str, ByteOrder | None]:
    """Strip a byte-order suffix (.le/.be/.little/.big) from a spec string.

    Stripping before regex matching avoids ambiguity between hex bit
    numbers (e.g. Word2.f) and byte-order suffixes (e.g. Word2.be).

    Returns (stripped_spec, byte_order_or_None).
    """
    for suffix, bo in _BYTE_ORDER_SUFFIXES.items():
        if spec.lower().endswith(suffix):
            return spec[:-len(suffix)], bo
    return spec, None


@dataclass(frozen=True, kw_only=True)
class Variable(ABC):
    """One addressable value: a data type at a byte offset in a data source.

    Decoding, encoding and formatting depend only on the data type and live
    here. A subclass adds the protocol-specific part: where the bytes live
    (:attr:`source`) and how the spec string is spelled (:attr:`spec`).
    """

    type: DataType
    offset: int
    extra: int | None = None  # bit number for Bit, max length for String/Chars, hex bit for Word/DWord
    label: str | None = None  # optional human-readable name
    byte_order: ByteOrder = ByteOrder.BIG

    @property
    @abstractmethod
    def spec(self) -> str:
        """Canonical spec string, e.g. ``DB200.Byte0`` or ``EIP.Input.Byte0``."""

    @property
    @abstractmethod
    def source(self) -> DataSource:
        """The data source this variable is read from and written to."""

    @property
    @abstractmethod
    def is_input(self) -> bool:
        """True when the source is a process input: read here, written by the field."""

    @property
    def display_name(self) -> str:
        return self.label or self.spec

    @property
    def offset_display(self) -> str:
        base = str(self.offset)
        if self.extra is not None and self.type.byte_size > 0:
            return f"{base}.{self.extra}"
        return base

    @property
    def byte_size(self) -> int:
        if self.type == DataType.STRING:
            if self.extra is None:
                raise ValueError(f"String variable {self.spec} requires max length")
            return self.extra + 2
        if self.type == DataType.CHARS:
            if self.extra is None:
                raise ValueError(f"Chars variable {self.spec} requires max length")
            return self.extra
        return self.type.byte_size

    @property
    def read_size(self) -> int:
        return self.byte_size

    @staticmethod
    def parse(
        spec: str,
        label: str | None = None,
        byte_order: ByteOrder | None = None,
    ) -> Variable:
        """Parse a spec string of any supported protocol into a variable.

        A ``.be``/``.le``/``.big``/``.little`` suffix on the spec selects the
        byte order; ``byte_order`` overrides both that and the protocol's
        default (S7 and Modbus big-endian, EIP little-endian).
        """
        spec_stripped, suffix_bo = _strip_byte_order_suffix(spec)
        bo = byte_order if byte_order is not None else suffix_bo
        m = _EIP_VAR_PATTERN.match(spec_stripped)
        if m:
            return _parse_eip(m, label, bo)
        m = _MODBUS_VAR_PATTERN.match(spec_stripped)
        if m:
            return _parse_modbus(m, label, bo)
        m = _DB_VAR_PATTERN.match(spec_stripped)
        if m:
            return _parse_s7(m, label, bo, area=S7Area.DB, db=int(m.group(1)))
        m = _AREA_VAR_PATTERN.match(spec_stripped)
        if m:
            return _parse_s7(m, label, bo, area=_AREAS[m.group(1).lower()], db=0)
        raise ValueError(
            f"Invalid variable spec: {spec!r}. "
            f"Expected format: DB<num>.<Type><offset>[.<extra>][.<be|le|big|little>] "
            f"or <Area>.<Type><offset>[.<extra>][.<be|le|big|little>] "
            f"or EIP.<Assembly>.<Type><offset>[.<extra>][.<be|le|big|little>] "
            f"or MB.<Table>.<Type><offset>[.<extra>][.<be|le|big|little>] "
            f"e.g. DB200.Byte0, EB.Byte0, EIP.Input.Byte0, MB.Holding.Word536"
        )

    def decode(self, data: bytes | bytearray) -> Union[int, float, bool, str]:
        if len(data) < self.byte_size:
            raise ValueError(f"Need {self.byte_size} bytes to decode {self.spec}, got {len(data)}")
        return _decode_value(data, self.type, self.extra, self.byte_order)

    def encode(self, value: Union[int, float, bool, str]) -> bytearray:
        return _encode_value(self.type, self.extra, value, self.byte_order)

    def encode_bit(self, current_byte: int, value: bool) -> bytearray:
        assert self.type == DataType.BIT and self.extra is not None
        return _encode_bit_value(self.extra, current_byte, value)

    @property
    def addresses_register_bit(self) -> bool:
        """True for ``Word2.5``-style specs: one bit of a 16- or 32-bit register."""
        return self.type in (DataType.WORD, DataType.DWORD) and self.extra is not None

    def format_value(self, value: Union[int, float, bool, str]) -> str:
        if self.addresses_register_bit:
            return "1" if value else "0"
        return _format_value(self.type, value)

    def parse_input(self, text: str) -> Union[int, float, bool, str]:
        if self.addresses_register_bit:
            return _parse_bit_text(text)
        return _parse_input(self.type, text)


@dataclass(frozen=True, kw_only=True)
class S7Variable(Variable):
    """A variable in an S7 data block or process area."""

    db: int  # DB number for the DB area; 0 for the other areas
    area: S7Area = S7Area.DB

    @property
    def spec(self) -> str:
        if self.area == S7Area.DB:
            base = f"DB{self.db}.{self.type.value}{self.offset}"
        else:
            base = f"{self.area.value}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def source(self) -> DataSource:
        if self.area == S7Area.DB:
            return DataSource.s7_db(self.db)
        return DataSource.s7_area(self.area.value)

    @property
    def is_input(self) -> bool:
        return self.area == S7Area.EB


@dataclass(frozen=True, kw_only=True)
class EIPVariable(Variable):
    """A variable in an EtherNet/IP assembly."""

    assembly: str  # "Input", "Output", "Config", or numeric
    byte_order: ByteOrder = ByteOrder.LITTLE

    @property
    def spec(self) -> str:
        base = f"EIP.{self.assembly}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def source(self) -> DataSource:
        return DataSource.eip(self.assembly)

    @property
    def is_input(self) -> bool:
        return self.assembly.lower() == "input"


_AREAS: dict[str, S7Area] = {str(a.value).lower(): a for a in S7Area}


def _parse_s7(
    m: re.Match,
    label: str | None,
    byte_order: ByteOrder | None,
    *,
    area: S7Area,
    db: int,
) -> S7Variable:
    """Build an S7Variable from a match against _DB_VAR_PATTERN or _AREA_VAR_PATTERN."""
    data_type = _parse_type_name(m.group(2))
    offset = int(m.group(3))
    extra = _parse_extra(m.group(4), data_type)
    _validate_type(extra, data_type, m.group(0))
    bo = byte_order if byte_order is not None else ByteOrder.BIG
    return S7Variable(db=db, type=data_type, offset=offset, extra=extra, label=label, area=area, byte_order=bo)


def _parse_eip(
    m: re.Match,
    label: str | None = None,
    byte_order: ByteOrder | None = None,
) -> EIPVariable:
    """Build an EIPVariable from a regex match against _EIP_VAR_PATTERN."""
    assembly = m.group(1)
    type_name = m.group(2)
    offset = int(m.group(3))
    extra_str = m.group(4)
    data_type = _parse_type_name(type_name)
    extra = _parse_extra(extra_str, data_type)
    spec = m.group(0)
    _validate_type(extra, data_type, spec)
    bo = byte_order if byte_order is not None else ByteOrder.LITTLE
    return EIPVariable(assembly=assembly, type=data_type, offset=offset, extra=extra, label=label, byte_order=bo)


@dataclass(frozen=True, kw_only=True)
class ModbusVariable(Variable):
    """A variable in a Modbus register or bit table.

    ``offset`` is a byte offset into the table, so holding register 268 is
    offset 536.  Modbus puts the high byte of a register first, hence the
    big-endian default inherited from Variable.
    """

    table: str  # "Holding", "Input", "Coil", "Discrete"

    @property
    def spec(self) -> str:
        base = f"MB.{self.table}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def register(self) -> int:
        """Holding/input register number this variable starts in."""
        return self.offset // 2

    @property
    def coil(self) -> int:
        """Coil or discrete-input number this variable addresses."""
        return self.offset * 8 + (self.extra or 0)

    @property
    def offset_display(self) -> str:
        """Address as the device manual writes it, not as a byte offset.

        Modbus documentation counts registers and coils, so showing the byte
        offset means reading every address twice: once here, once halved.
        A byte that sits inside a register has no register number of its own,
        so it falls back to the byte offset, marked.
        """
        if self.table in ("Coil", "Discrete"):
            return str(self.coil)
        if self.offset % 2:
            return f"b{self.offset}"
        if self.extra is not None:
            return f"{self.register}.{self.extra}"
        return str(self.register)

    @property
    def source(self) -> DataSource:
        return DataSource.modbus(self.table)

    @property
    def is_input(self) -> bool:
        return self.table in ("Input", "Discrete")


def _parse_modbus(
    m: re.Match,
    label: str | None = None,
    byte_order: ByteOrder | None = None,
) -> ModbusVariable:
    """Build a ModbusVariable from a regex match against _MODBUS_VAR_PATTERN."""
    table = _MODBUS_TABLES[m.group(1).lower()]
    data_type = _parse_type_name(m.group(2))
    offset = int(m.group(3))
    extra = _parse_extra(m.group(4), data_type)
    _validate_type(extra, data_type, m.group(0))
    bo = byte_order if byte_order is not None else ByteOrder.BIG
    return ModbusVariable(
        table=table, type=data_type, offset=offset, extra=extra, label=label, byte_order=bo
    )


def compute_read_range(variables: Sequence[Variable]) -> tuple[int, int]:
    """Compute the minimal (start, size) to cover all variables in a single read.

    All variables must be in the same source (same assembly/DB).
    Returns (start_offset, byte_count).
    """
    if not variables:
        raise ValueError("No variables provided")

    sources = {str(v.source) for v in variables}
    if len(sources) > 1:
        raise ValueError(f"Variables span multiple sources: {sources}")

    min_offset = min(v.offset for v in variables)
    max_end = max(v.offset + v.byte_size for v in variables)
    return min_offset, max_end - min_offset


def extract_value(
    variable: Variable, data: bytes | bytearray, data_start: int
) -> Union[int, float, bool, str]:
    """Extract a variable's value from a read buffer.

    data_start is the start offset used in the read call.
    """
    local_offset = variable.offset - data_start
    if local_offset < 0 or local_offset + variable.byte_size > len(data):
        raise ValueError(
            f"Variable {variable.spec} at offset {variable.offset} "
            f"not within read range (start={data_start}, size={len(data)})"
        )
    return variable.decode(data[local_offset : local_offset + variable.byte_size])


def encode_for_write(
    var: Variable, value: Union[int, float, bool, str], connection: Connection
) -> bytearray:
    """The bytes to write so that ``var`` reads back as ``value``.

    A Bit variable owns one bit of its byte, so the byte is read from the
    connection first and only that bit is changed. Every other type encodes
    to a whole value.
    """
    if var.type == DataType.BIT:
        if not isinstance(value, bool):
            raise TypeError("Bit writes require a boolean value")
        current = connection.read_source(var.source, var.offset, 1)
        return var.encode_bit(current.data[0], value)
    return var.encode(value)
