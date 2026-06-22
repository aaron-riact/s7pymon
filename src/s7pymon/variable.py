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
from dataclasses import dataclass
from enum import Enum
from typing import Union

from .protocols import DataSource


class S7Area(Enum):
    """S7 PLC memory area types."""

    DB = "DB"    # Data Blocks
    EB = "EB"    # Process Image Input  (Eingangsbereich / PE)
    AB = "AB"    # Process Image Output (Ausgangsbereich / PA)
    MB = "MB"    # Merkers / Flags      (Merkerbereich / MK)
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


class DataType(Enum):
    BYTE = "Byte"
    INT = "Int"
    DINT = "DInt"
    WORD = "Word"
    DWORD = "DWord"
    REAL = "Real"
    BIT = "Bit"
    STRING = "String"

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


S7Type = DataType
"Deprecated alias — use DataType."

# Pattern: DB<num>.<Type><offset>[.<extra>]
_DB_VAR_PATTERN = re.compile(
    r"^DB(\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String)(\d+)(?:\.(\d+))?$",
    re.IGNORECASE,
)

# Pattern: <Area>.<Type><offset>[.<extra>]  (for EB, AB, MB, CT, TM)
_AREA_VAR_PATTERN = re.compile(
    r"^(EB|AB|MB|CT|TM)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String)(\d+)(?:\.(\d+))?$",
    re.IGNORECASE,
)

# Pattern: EIP.<Assembly>.<Type><offset>[.<extra>]
_EIP_VAR_PATTERN = re.compile(
    r"^EIP\.(Input|Output|Config|\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String)"
    r"(\d+)(?:\.(\d+))?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- shared helpers


def _decode_value(data: bytes | bytearray, data_type: DataType, extra: int | None) -> Union[int, float, bool, str]:
    if len(data) < data_type.byte_size and data_type != DataType.STRING and data_type != DataType.BIT:
        raise ValueError(f"Need {data_type.byte_size} bytes to decode, got {len(data)}")
    raw = data[:data_type.byte_size] if data_type != DataType.STRING else data

    if data_type == DataType.BIT:
        assert extra is not None
        return bool(raw[0] & (1 << extra))

    if data_type == DataType.STRING:
        if len(raw) < 2:
            return ""
        actual_len = raw[1]
        return raw[2 : 2 + actual_len].decode("ascii", errors="replace")

    fmt = data_type.struct_format
    assert fmt is not None
    return struct.unpack(fmt, raw)[0]


def _encode_value(data_type: DataType, extra: int | None, value: Union[int, float, bool, str]) -> bytearray:
    if data_type == DataType.BIT:
        raise ValueError("Cannot encode full byte for Bit type; use encode_bit() instead")

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

    fmt = data_type.struct_format
    assert fmt is not None
    coerced = float(value) if data_type == DataType.REAL else int(value)
    return bytearray(struct.pack(fmt, coerced))


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
    if data_type == DataType.STRING:
        return repr(value)
    return str(value)


def _parse_input(data_type: DataType, text: str) -> Union[int, float, bool, str]:
    text = text.strip()
    if data_type == DataType.BIT:
        if text.lower() in ("1", "true", "on", "yes"):
            return True
        if text.lower() in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"Invalid bit value: {text!r}")
    if data_type == DataType.REAL:
        return float(text)
    if data_type == DataType.STRING:
        return text
    if text.startswith("0x") or text.startswith("0X"):
        return int(text, 16)
    return int(text)


def _validate_type(extra: int | None, data_type: DataType, spec: str) -> None:
    if data_type == DataType.BIT:
        if extra is None:
            raise ValueError(f"Bit variable requires bit number: {spec} (e.g. DB200.Bit0.3)")
        if not 0 <= extra <= 7:
            raise ValueError(f"Bit number must be 0-7, got {extra} in {spec}")
    if data_type == DataType.STRING and extra is None:
        raise ValueError(f"String variable requires max length: {spec} (e.g. DB200.String50.20)")


_type_map: dict[str, DataType] = {str(t.value).lower(): t for t in DataType}


def _parse_type_name(type_name: str) -> DataType:
    return _type_map[type_name.lower()]


@dataclass(frozen=True)
class S7Variable:
    """Parsed S7 variable specification."""

    db: int  # DB number for DB area; 0 for non-DB areas
    type: DataType
    offset: int
    extra: int | None = None  # bit number for Bit, max length for String
    label: str | None = None  # optional human-readable name
    area: S7Area = S7Area.DB  # memory area

    @property
    def spec(self) -> str:
        """Canonical spec string like DB200.Byte0 or EB.Byte0."""
        if self.area == S7Area.DB:
            base = f"DB{self.db}.{self.type.value}{self.offset}"
        else:
            base = f"{self.area.value}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def display_name(self) -> str:
        return self.label or self.spec

    @property
    def byte_size(self) -> int:
        if self.type == DataType.STRING:
            if self.extra is None:
                raise ValueError(f"String variable {self.spec} requires max length")
            return self.extra + 2
        return self.type.byte_size

    @property
    def read_size(self) -> int:
        return self.byte_size

    @property
    def source(self) -> DataSource:
        if self.area == S7Area.DB:
            return DataSource.s7_db(self.db)
        return DataSource.s7_area(self.area.value)

    @classmethod
    def parse(cls, spec: str, label: str | None = None) -> S7Variable | EIPVariable:
        m = _EIP_VAR_PATTERN.match(spec)
        if m:
            return _parse_eip(m, label)
        m = _DB_VAR_PATTERN.match(spec)
        if m:
            db = int(m.group(1))
            type_name = m.group(2)
            offset = int(m.group(3))
            extra_str = m.group(4)
            extra = int(extra_str) if extra_str is not None else None
            data_type = _parse_type_name(type_name)
            _validate_type(extra, data_type, spec)
            return cls(db=db, type=data_type, offset=offset, extra=extra, label=label, area=S7Area.DB)
        m = _AREA_VAR_PATTERN.match(spec)
        if m:
            area_name = m.group(1)
            area_map: dict[str, S7Area] = {str(a.value).lower(): a for a in S7Area}
            area = area_map[area_name.lower()]
            type_name = m.group(2)
            offset = int(m.group(3))
            extra_str = m.group(4)
            extra = int(extra_str) if extra_str is not None else None
            data_type = _parse_type_name(type_name)
            _validate_type(extra, data_type, spec)
            return cls(db=0, type=data_type, offset=offset, extra=extra, label=label, area=area)
        raise ValueError(
            f"Invalid variable spec: {spec!r}. "
            f"Expected format: DB<num>.<Type><offset>[.<extra>] "
            f"or <Area>.<Type><offset>[.<extra>] "
            f"or EIP.<Assembly>.<Type><offset>[.<extra>] "
            f"e.g. DB200.Byte0, EB.Byte0, EIP.Input.Byte0"
        )

    def decode(self, data: bytes | bytearray) -> Union[int, float, bool, str]:
        if len(data) < self.byte_size:
            raise ValueError(f"Need {self.byte_size} bytes to decode {self.spec}, got {len(data)}")
        return _decode_value(data, self.type, self.extra)

    def encode(self, value: Union[int, float, bool, str]) -> bytearray:
        return _encode_value(self.type, self.extra, value)

    def encode_bit(self, current_byte: int, value: bool) -> bytearray:
        assert self.type == DataType.BIT and self.extra is not None
        return _encode_bit_value(self.extra, current_byte, value)

    def format_value(self, value: Union[int, float, bool, str]) -> str:
        return _format_value(self.type, value)

    def parse_input(self, text: str) -> Union[int, float, bool, str]:
        return _parse_input(self.type, text)


@dataclass(frozen=True)
class EIPVariable:
    """EtherNet/IP assembly variable specification."""

    assembly: str  # "Input", "Output", "Config", or numeric
    type: DataType
    offset: int
    extra: int | None = None
    label: str | None = None

    @property
    def spec(self) -> str:
        base = f"EIP.{self.assembly}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def display_name(self) -> str:
        return self.label or self.spec

    @property
    def byte_size(self) -> int:
        if self.type == DataType.STRING:
            if self.extra is None:
                raise ValueError(f"String variable {self.spec} requires max length")
            return self.extra + 2
        return self.type.byte_size

    @property
    def read_size(self) -> int:
        return self.byte_size

    @property
    def source(self) -> DataSource:
        return DataSource.eip(self.assembly)

    def decode(self, data: bytes | bytearray) -> Union[int, float, bool, str]:
        if len(data) < self.byte_size:
            raise ValueError(f"Need {self.byte_size} bytes to decode {self.spec}, got {len(data)}")
        return _decode_value(data, self.type, self.extra)

    def encode(self, value: Union[int, float, bool, str]) -> bytearray:
        return _encode_value(self.type, self.extra, value)

    def encode_bit(self, current_byte: int, value: bool) -> bytearray:
        assert self.type == DataType.BIT and self.extra is not None
        return _encode_bit_value(self.extra, current_byte, value)

    def format_value(self, value: Union[int, float, bool, str]) -> str:
        return _format_value(self.type, value)

    def parse_input(self, text: str) -> Union[int, float, bool, str]:
        return _parse_input(self.type, text)


def _parse_eip(m: re.Match, label: str | None = None) -> EIPVariable:
    """Build an EIPVariable from a regex match against _EIP_VAR_PATTERN."""
    assembly = m.group(1)
    type_name = m.group(2)
    offset = int(m.group(3))
    extra_str = m.group(4)
    extra = int(extra_str) if extra_str is not None else None
    data_type = _parse_type_name(type_name)
    spec = m.group(0)
    _validate_type(extra, data_type, spec)
    return EIPVariable(assembly=assembly, type=data_type, offset=offset, extra=extra, label=label)


def compute_read_range(variables: list) -> tuple[int, int]:
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
    variable, data: bytes | bytearray, data_start: int
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
