"""Headless monitoring engine shared by non-TUI frontends.

This module extracts the *behaviour* of the monitor — polling, decoding,
change detection, data logging and writing — out of the Textual UI so it can
drive any frontend (the web dashboard, scripts, tests) without pulling in a
terminal-UI framework.

The engine is deliberately UI-agnostic: :meth:`MonitorEngine.poll` returns a
:class:`Snapshot` of plain, JSON-friendly dataclasses, and write/connection
helpers raise ordinary exceptions instead of touching any widgets. The Textual
app re-exports :class:`WriteMode` and :func:`format_hex_dump` from here so the
two frontends share a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Union

from .connection import ConnectionState, S7Connection
from .logging import DataLogger, LogEntry
from .variable import S7Area, S7Type, S7Variable, extract_value

Value = Union[int, float, bool, str]


class WriteMode(Enum):
    """Controls whether writes to the PLC are permitted."""

    DISABLED = "disabled"  # Writes blocked entirely
    CONFIRM = "confirm"  # Writes require explicit confirmation (frontend prompt)
    ALLOWED = "allowed"  # Writes go through immediately


def format_hex_dump(data: bytearray, start_offset: int = 0, bytes_per_line: int = 16) -> str:
    """Format raw bytes as a hex dump with offset, hex values, and ASCII."""
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i : i + bytes_per_line]
        offset = start_offset + i
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        if len(chunk) > 8:
            hex_left = " ".join(f"{b:02X}" for b in chunk[:8])
            hex_right = " ".join(f"{b:02X}" for b in chunk[8:])
            hex_part = f"{hex_left}  {hex_right}"
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
        hex_padded = hex_part.ljust(3 * bytes_per_line + 1)
        lines.append(f"  {offset:04X} │ {hex_padded}│ {ascii_part}")
    return "\n".join(lines)


def area_label(area: S7Area, db: int) -> str:
    """Short label for a variable's area, e.g. ``DB210`` or ``EB``."""
    return f"DB{db}" if area == S7Area.DB else area.value


def group_key(area: S7Area, db: int) -> str:
    """Key used to associate a variable with the buffer of its read group."""
    return area_label(area, db)


@dataclass(frozen=True)
class VariableReading:
    """A single variable's decoded value for one poll cycle."""

    spec: str
    label: str
    area: str
    type: str
    offset: int
    bit: int | None
    value: str
    raw_hex: str
    changed: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "label": self.label,
            "area": self.area,
            "type": self.type,
            "offset": self.offset,
            "bit": self.bit,
            "value": self.value,
            "raw_hex": self.raw_hex,
            "changed": self.changed,
            "error": self.error,
        }


@dataclass(frozen=True)
class GroupDump:
    """Hex dump of one read group's buffer for one poll cycle."""

    key: str
    label: str
    start: int
    size: int
    bytes_hex: str
    hex_dump: str

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "start": self.start,
            "size": self.size,
            "bytes_hex": self.bytes_hex,
            "hex_dump": self.hex_dump,
        }


@dataclass(frozen=True)
class Snapshot:
    """A complete, JSON-friendly view of the monitor at one instant."""

    timestamp: str
    poll_count: int
    connection_state: str
    paused: bool
    write_mode: str
    error: str | None
    readings: list[VariableReading] = field(default_factory=list)
    groups: list[GroupDump] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "poll_count": self.poll_count,
            "connection_state": self.connection_state,
            "paused": self.paused,
            "write_mode": self.write_mode,
            "error": self.error,
            "readings": [r.to_dict() for r in self.readings],
            "groups": [g.to_dict() for g in self.groups],
        }


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a write request."""

    spec: str
    description: str
    bytes_hex: str
    offset: int
    target: str


class WriteBlockedError(RuntimeError):
    """Raised when a write is attempted while writes are disabled."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MonitorEngine:
    """UI-agnostic driver for monitoring and writing S7 PLC data.

    Parameters mirror the Textual app so the CLI can build either frontend from
    the same resolved runtime. The engine owns the change-detection state and
    the optional data logger; frontends simply call :meth:`poll` on a schedule
    and render the returned :class:`Snapshot`.
    """

    def __init__(
        self,
        connection: S7Connection,
        variables: list[S7Variable],
        read_groups: list,
        poll_interval: float = 1.0,
        write_mode: WriteMode = WriteMode.DISABLED,
        logger: DataLogger | None = None,
    ):
        self._connection = connection
        self._variables = variables
        self._read_groups = read_groups
        self._poll_interval = poll_interval
        self._write_mode = write_mode
        self._logger = logger
        self._previous_values: dict[str, str] = {}
        self._current_values: dict[str, str] = {}
        self._poll_count = 0
        self._paused = False

    # ------------------------------------------------------------------ state
    @property
    def connection(self) -> S7Connection:
        return self._connection

    @property
    def variables(self) -> list[S7Variable]:
        return self._variables

    @property
    def read_groups(self) -> list:
        return self._read_groups

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)

    @property
    def write_mode(self) -> WriteMode:
        return self._write_mode

    @write_mode.setter
    def write_mode(self, mode: WriteMode) -> None:
        self._write_mode = mode

    def cycle_write_mode(self) -> WriteMode:
        """Advance disabled → confirm → allowed → disabled and return the new mode."""
        nxt = {
            WriteMode.DISABLED: WriteMode.CONFIRM,
            WriteMode.CONFIRM: WriteMode.ALLOWED,
            WriteMode.ALLOWED: WriteMode.DISABLED,
        }
        self._write_mode = nxt[self._write_mode]
        return self._write_mode

    @property
    def writes_enabled(self) -> bool:
        return self._write_mode != WriteMode.DISABLED

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        self._connection.connect()

    def disconnect(self) -> None:
        self._connection.disconnect()

    def reconnect(self) -> None:
        self._connection.disconnect()
        self._connection.connect()

    def close(self) -> None:
        """Release resources (logger + connection)."""
        if self._logger is not None:
            self._logger.close()
        try:
            self._connection.disconnect()
        except Exception:
            pass

    # ------------------------------------------------------------------- read
    def find_variable(self, spec: str) -> S7Variable | None:
        return next((v for v in self._variables if v.spec == spec), None)

    def poll(self) -> Snapshot:
        """Read every group, decode all variables and detect changes.

        Always returns a :class:`Snapshot`. Read failures are captured in the
        snapshot's ``error`` field (and surfaced via the connection state)
        rather than raised, so a frontend's render loop never crashes.
        """
        if not self._connection.connected:
            return self._snapshot(error="Not connected", groups=[], readings=[])

        try:
            buffers: dict[str, tuple[bytearray, int]] = {}
            groups: list[GroupDump] = []
            for group in self._read_groups:
                result = self._connection.area_read(
                    group.area, group.start, group.size, db=group.db
                )
                buffers[group.key] = (result.data, group.start)
                groups.append(
                    GroupDump(
                        key=group.key,
                        label=group.label,
                        start=group.start,
                        size=group.size,
                        bytes_hex=" ".join(f"{b:02X}" for b in result.data),
                        hex_dump=format_hex_dump(result.data, group.start),
                    )
                )
        except Exception as e:
            return self._snapshot(error=str(e), groups=[], readings=[])

        self._previous_values = dict(self._current_values)
        readings = [self._read_variable(var, buffers) for var in self._variables]
        self._poll_count += 1
        return self._snapshot(error=None, groups=groups, readings=readings)

    def _read_variable(
        self, var: S7Variable, buffers: dict[str, tuple[bytearray, int]]
    ) -> VariableReading:
        label = area_label(var.area, var.db)
        key = group_key(var.area, var.db)
        buffer = buffers.get(key)
        if buffer is None:
            return self._reading(var, label, value="—", raw_hex="", changed=False,
                                 error="no data")
        data, data_start = buffer
        try:
            value = extract_value(var, data, data_start)
            formatted = var.format_value(value)
            local = var.offset - data_start
            raw_bytes = data[local : local + var.byte_size]
            raw_hex = " ".join(f"{b:02X}" for b in raw_bytes)
        except Exception as e:
            return self._reading(var, label, value="ERR", raw_hex="", changed=False,
                                 error=str(e))

        prev = self._previous_values.get(var.spec)
        changed = prev is not None and prev != formatted
        self._current_values[var.spec] = formatted

        if changed and self._logger is not None:
            self._logger.log(
                LogEntry(
                    timestamp=_now_iso(),
                    variable=var.display_name,
                    type=var.type.value,
                    area=label,
                    offset=var.offset,
                    old_value=prev or "",
                    new_value=formatted,
                    raw_hex=raw_hex,
                )
            )
        return self._reading(var, label, value=formatted, raw_hex=raw_hex,
                            changed=changed, error=None)

    def _reading(
        self,
        var: S7Variable,
        label: str,
        *,
        value: str,
        raw_hex: str,
        changed: bool,
        error: str | None,
    ) -> VariableReading:
        return VariableReading(
            spec=var.spec,
            label=var.display_name,
            area=label,
            type=var.type.value,
            offset=var.offset,
            bit=var.extra if var.type == S7Type.BIT else None,
            value=value,
            raw_hex=raw_hex,
            changed=changed,
            error=error,
        )

    def _snapshot(
        self,
        *,
        error: str | None,
        groups: list[GroupDump],
        readings: list[VariableReading],
    ) -> Snapshot:
        return Snapshot(
            timestamp=_now_iso(),
            poll_count=self._poll_count,
            connection_state=self._connection.state.value,
            paused=self._paused,
            write_mode=self._write_mode.value,
            error=error,
            readings=readings,
            groups=groups,
        )

    # ------------------------------------------------------------------ write
    def write_variable(self, spec: str, text: str) -> WriteResult:
        """Encode ``text`` for variable ``spec`` and write it to the PLC.

        Raises :class:`WriteBlockedError` when writes are disabled, ``ValueError``
        for an unknown spec or unparsable value, and propagates connection
        errors. Bit writes are performed as a read-modify-write of the byte.
        """
        if not self.writes_enabled:
            raise WriteBlockedError("Writes are disabled")
        var = self.find_variable(spec) or S7Variable.parse(spec)
        return self._write(var, text)

    def write_spec(self, spec: str, text: str) -> WriteResult:
        """Like :meth:`write_variable` but always parses ``spec`` fresh.

        Used by the command bar where the target need not be a monitored var.
        """
        if not self.writes_enabled:
            raise WriteBlockedError("Writes are disabled")
        return self._write(S7Variable.parse(spec), text)

    def _write(self, var: S7Variable, text: str) -> WriteResult:
        parsed = var.parse_input(text)
        if var.type == S7Type.BIT:
            if not isinstance(parsed, bool):
                raise TypeError("Bit writes require a boolean value")
            current = self._connection.area_read(var.area, var.offset, 1, db=var.db)
            encoded = var.encode_bit(current.data[0], parsed)
        else:
            encoded = var.encode(parsed)
        self._connection.area_write(var.area, var.offset, encoded, db=var.db)
        return WriteResult(
            spec=var.spec,
            description=f"Set {var.display_name} = {parsed}",
            bytes_hex=" ".join(f"{b:02X}" for b in encoded),
            offset=var.offset,
            target=area_label(var.area, var.db),
        )

    def write_raw(self, db: int, offset: int, data: bytearray) -> WriteResult:
        """Raw byte write to a DB (command-bar ``write`` command)."""
        if not self.writes_enabled:
            raise WriteBlockedError("Writes are disabled")
        self._connection.area_write(S7Area.DB, offset, data, db=db)
        return WriteResult(
            spec=f"DB{db}@{offset}",
            description=f"Raw write to DB{db} at offset {offset}",
            bytes_hex=" ".join(f"{b:02X}" for b in data),
            offset=offset,
            target=f"DB{db}",
        )

    # ----------------------------------------------------------------- export
    def describe(self) -> dict:
        """Static metadata for a frontend's initial handshake."""
        return {
            "address": self._connection.config.display,
            "poll_interval": self._poll_interval,
            "write_mode": self._write_mode.value,
            "groups": [
                {"key": g.key, "label": g.label, "start": g.start, "size": g.size}
                for g in self._read_groups
            ],
            "variables": [
                {
                    "spec": v.spec,
                    "label": v.display_name,
                    "area": area_label(v.area, v.db),
                    "type": v.type.value,
                    "offset": v.offset,
                    "bit": v.extra if v.type == S7Type.BIT else None,
                }
                for v in self._variables
            ],
        }
