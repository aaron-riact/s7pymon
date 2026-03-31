"""S7 variable specification parsing and value conversion.

Supports variable specs like Sharp7.Monitor format:
  DB200.Byte0     - unsigned byte at offset 0
  DB200.Int4      - signed 16-bit integer at offset 4
  DB200.DInt8     - signed 32-bit integer at offset 8
  DB200.Word2     - unsigned 16-bit integer at offset 2
  DB200.DWord6    - unsigned 32-bit integer at offset 6
  DB200.Real12    - 32-bit float at offset 12
  DB200.Bit0.3    - bit 3 of byte at offset 0
  DB200.String50.20 - string at offset 50, max length 20
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Union


class S7Type(Enum):
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


_TYPE_SIZES: dict[S7Type, int] = {
    S7Type.BYTE: 1,
    S7Type.INT: 2,
    S7Type.DINT: 4,
    S7Type.WORD: 2,
    S7Type.DWORD: 4,
    S7Type.REAL: 4,
    S7Type.BIT: 1,
    S7Type.STRING: 0,  # variable, determined by extra param
}

# Big-endian struct formats (S7 is big-endian)
_TYPE_FORMATS: dict[S7Type, str] = {
    S7Type.BYTE: ">B",
    S7Type.INT: ">h",
    S7Type.DINT: ">i",
    S7Type.WORD: ">H",
    S7Type.DWORD: ">I",
    S7Type.REAL: ">f",
}

# Pattern: DB<num>.<Type><offset>[.<extra>]
_VAR_PATTERN = re.compile(
    r"^DB(\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String)(\d+)(?:\.(\d+))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class S7Variable:
    """Parsed S7 variable specification."""

    db: int
    type: S7Type
    offset: int
    extra: int | None = None  # bit number for Bit, max length for String
    label: str | None = None  # optional human-readable name

    @property
    def spec(self) -> str:
        """Canonical spec string like DB200.Byte0."""
        base = f"DB{self.db}.{self.type.value}{self.offset}"
        if self.extra is not None:
            return f"{base}.{self.extra}"
        return base

    @property
    def display_name(self) -> str:
        return self.label or self.spec

    @property
    def byte_size(self) -> int:
        if self.type == S7Type.STRING:
            if self.extra is None:
                raise ValueError(f"String variable {self.spec} requires max length")
            return self.extra + 2  # S7 strings have 2-byte header (max_len, actual_len)
        return self.type.byte_size

    @property
    def read_size(self) -> int:
        """Number of bytes to read from DB for this variable."""
        return self.byte_size

    @classmethod
    def parse(cls, spec: str, label: str | None = None) -> S7Variable:
        """Parse a variable spec string like DB200.Byte0 or DB200.Bit0.3."""
        m = _VAR_PATTERN.match(spec)
        if not m:
            raise ValueError(
                f"Invalid variable spec: {spec!r}. "
                f"Expected format: DB<num>.<Type><offset>[.<extra>] "
                f"e.g. DB200.Byte0, DB200.Bit0.3"
            )
        db = int(m.group(1))
        type_name = m.group(2)
        offset = int(m.group(3))
        extra_str = m.group(4)
        extra = int(extra_str) if extra_str is not None else None

        # Normalize type name to match enum (case-insensitive input)
        type_map = {t.value.lower(): t for t in S7Type}
        s7_type = type_map[type_name.lower()]

        # Validation
        if s7_type == S7Type.BIT:
            if extra is None:
                raise ValueError(f"Bit variable requires bit number: {spec} (e.g. DB200.Bit0.3)")
            if not 0 <= extra <= 7:
                raise ValueError(f"Bit number must be 0-7, got {extra} in {spec}")
        if s7_type == S7Type.STRING and extra is None:
            raise ValueError(f"String variable requires max length: {spec} (e.g. DB200.String50.20)")

        return cls(db=db, type=s7_type, offset=offset, extra=extra, label=label)

    def decode(self, data: bytes | bytearray) -> Union[int, float, bool, str]:
        """Decode raw bytes into a Python value."""
        if len(data) < self.byte_size:
            raise ValueError(
                f"Need {self.byte_size} bytes to decode {self.spec}, got {len(data)}"
            )
        raw = data[: self.byte_size]

        if self.type == S7Type.BIT:
            assert self.extra is not None
            return bool(raw[0] & (1 << self.extra))

        if self.type == S7Type.STRING:
            if len(raw) < 2:
                return ""
            actual_len = raw[1]
            return raw[2 : 2 + actual_len].decode("ascii", errors="replace")

        fmt = self.type.struct_format
        assert fmt is not None
        return struct.unpack(fmt, raw)[0]

    def encode(self, value: Union[int, float, bool, str]) -> bytearray:
        """Encode a Python value into raw bytes for writing."""
        if self.type == S7Type.BIT:
            raise ValueError(
                "Cannot encode full byte for Bit type; use encode_bit() instead"
            )

        if self.type == S7Type.STRING:
            assert self.extra is not None
            s = str(value)
            max_len = self.extra
            s = s[:max_len]
            buf = bytearray(max_len + 2)
            buf[0] = max_len
            buf[1] = len(s)
            buf[2 : 2 + len(s)] = s.encode("ascii", errors="replace")
            return buf

        fmt = self.type.struct_format
        assert fmt is not None
        return bytearray(struct.pack(fmt, self._coerce(value)))

    def encode_bit(self, current_byte: int, value: bool) -> bytearray:
        """Encode a bit value by modifying a single byte."""
        assert self.type == S7Type.BIT and self.extra is not None
        if value:
            result = current_byte | (1 << self.extra)
        else:
            result = current_byte & ~(1 << self.extra)
        return bytearray([result])

    def _coerce(self, value: Union[int, float, bool, str]) -> Union[int, float]:
        """Coerce a value to the appropriate Python type for struct packing."""
        if self.type == S7Type.REAL:
            return float(value)
        return int(value)

    def format_value(self, value: Union[int, float, bool, str]) -> str:
        """Format a decoded value for display."""
        if self.type == S7Type.BIT:
            return "1" if value else "0"
        if self.type == S7Type.REAL:
            return f"{value:.4f}"
        if self.type == S7Type.STRING:
            return repr(value)
        return str(value)

    def parse_input(self, text: str) -> Union[int, float, bool, str]:
        """Parse user text input into a value suitable for encode()."""
        text = text.strip()
        if self.type == S7Type.BIT:
            if text.lower() in ("1", "true", "on", "yes"):
                return True
            if text.lower() in ("0", "false", "off", "no"):
                return False
            raise ValueError(f"Invalid bit value: {text!r}")
        if self.type == S7Type.REAL:
            return float(text)
        if self.type == S7Type.STRING:
            return text
        # Integer types - support hex
        if text.startswith("0x") or text.startswith("0X"):
            return int(text, 16)
        return int(text)


def compute_read_range(variables: list[S7Variable]) -> tuple[int, int]:
    """Compute the minimal (start, size) to cover all variables in a single DB read.

    All variables must be in the same DB.
    Returns (start_offset, byte_count).
    """
    if not variables:
        raise ValueError("No variables provided")

    dbs = {v.db for v in variables}
    if len(dbs) > 1:
        raise ValueError(f"Variables span multiple DBs: {dbs}")

    min_offset = min(v.offset for v in variables)
    max_end = max(v.offset + v.byte_size for v in variables)
    return min_offset, max_end - min_offset


def extract_value(
    variable: S7Variable, data: bytes | bytearray, data_start: int
) -> Union[int, float, bool, str]:
    """Extract a variable's value from a DB read buffer.

    data_start is the start offset that was used in the db_read call.
    """
    local_offset = variable.offset - data_start
    if local_offset < 0 or local_offset + variable.byte_size > len(data):
        raise ValueError(
            f"Variable {variable.spec} at offset {variable.offset} "
            f"not within read range (start={data_start}, size={len(data)})"
        )
    return variable.decode(data[local_offset : local_offset + variable.byte_size])
