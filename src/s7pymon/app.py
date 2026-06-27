"""S7 Monitor — Modern terminal UI for monitoring and writing S7 PLC data blocks.

A Textual-based TUI application inspired by Sharp7.Monitor that provides:
- Live hex dump of DB contents with auto-refresh
- Parsed variable table with change highlighting
- Inline editing of variable values with write confirmation
- Command bar for raw byte writes with confirmation
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static
from textual.widgets._data_table import ColumnKey, RowKey

from .protocols import Connection, ConnectionState, DataSource
from .engine import ReadGroup, WriteMode, format_hex_dump
from .errors import log_error
from .logging import DataLogger, LogEntry, LogFormat, SessionMetadata
from .rules import RulesEngine
from .variable import DataType, Variable, compute_read_range, extract_value

__all__ = ["S7MonitorApp", "WriteMode", "format_hex_dump", "ReadGroup"]


@dataclass
class PendingWrite:
    """Describes a write operation awaiting confirmation."""

    description: str  # Human-readable summary
    source: DataSource
    offset: int
    data: bytearray
    detail: str = ""  # For display purposes

    @property
    def target_label(self) -> str:
        return str(self.source)


class ConnectionStatus(Static):
    """Displays connection state in the header area."""

    state: reactive[ConnectionState] = reactive(ConnectionState.DISCONNECTED)
    config_text: reactive[str] = reactive("")
    poll_count: reactive[int] = reactive(0)
    write_mode: reactive[WriteMode] = reactive(WriteMode.DISABLED)

    def render(self) -> Text:
        state = self.state
        if state == ConnectionState.CONNECTED:
            indicator = Text("● ", style="bold green")
            status = Text("Connected", style="green")
        elif state == ConnectionState.CONNECTING:
            indicator = Text("◐ ", style="bold yellow")
            status = Text("Connecting…", style="yellow")
        elif state == ConnectionState.ERROR:
            indicator = Text("● ", style="bold red")
            status = Text("Error", style="red")
        else:
            indicator = Text("○ ", style="dim")
            status = Text("Disconnected", style="dim")

        result = Text("  ") + indicator + status
        if self.config_text:
            result += Text(f"  │  {self.config_text}", style="dim")
        if self.poll_count > 0:
            result += Text(f"  │  polls: {self.poll_count}", style="dim")

        # Write mode indicator
        wm = self.write_mode
        if wm == WriteMode.DISABLED:
            result += Text("  │  ", style="dim")
            result += Text("🔒 read-only", style="dim red")
        elif wm == WriteMode.CONFIRM:
            result += Text("  │  ", style="dim")
            result += Text("writes: confirm", style="dim yellow")
        else:
            result += Text("  │  ", style="dim")
            result += Text("writes: allowed", style="dim green")
        return result


class HexDumpDisplay(Static):
    """Live hex dump of read group contents."""

    collapsed: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._group_data: list[tuple[str, bytearray, int]] = []
        self._changed_abs_offsets: set[int] = set()
        self._selected_abs_offsets: dict[str, set[int]] = {}
        self._interesting_abs_offsets: set[int] | None = None
        self._hex_shape: tuple[tuple[str, int], ...] = ()

    def set_selected_offsets(self, group_label: str, offsets: set[int]) -> None:
        if self._selected_abs_offsets.get(group_label) == offsets:
            return
        self._selected_abs_offsets[group_label] = offsets
        self.refresh()

    def watch_collapsed(self, old_val: bool, new_val: bool) -> None:
        self.refresh(layout=True)

    def set_data(
        self,
        group_data: list[tuple[str, bytearray, int]],
        changed_abs_offsets: set[int] | None = None,
        interesting_abs_offsets: set[int] | None = None,
    ) -> None:
        self._group_data = group_data
        self._changed_abs_offsets = changed_abs_offsets or set()
        self._interesting_abs_offsets = interesting_abs_offsets
        shape = tuple((label, len(d)) for label, d, _ in group_data)
        needs_layout = shape != self._hex_shape
        self._hex_shape = shape
        self.refresh(layout=needs_layout)

    def render(self) -> Text:
        if self.collapsed:
            return Text("  ▸ hex dump (press h to expand)", style="dim cyan")

        if not self._group_data:
            return Text("  No data yet")

        result = Text()
        for gidx, (label, data, start) in enumerate(self._group_data):
            if gidx > 0:
                result.append("\n")

            sep = f"  ─── {label} "
            result.append(sep, style="bold cyan")
            result.append("─" * max(0, 62 - len(sep) + 2), style="dim cyan")
            result.append("\n")

            for i in range(0, len(data), 16):
                chunk = data[i : i + 16]
                abs_line = start + i
                result.append(f"  {abs_line:04X} │ ", style="dim cyan")

                for j, b in enumerate(chunk):
                    byte_abs = start + i + j
                    pair = f"{b:02X}"
                    interesting = self._interesting_abs_offsets is None or byte_abs in self._interesting_abs_offsets
                    group_selected = self._selected_abs_offsets.get(label, set())
                    if byte_abs in group_selected and byte_abs in self._changed_abs_offsets:
                        result.append(Text(pair, style="bold reverse #FF8800"))
                    elif byte_abs in group_selected:
                        result.append(Text(pair, style="bold reverse"))
                    elif byte_abs in self._changed_abs_offsets:
                        result.append(Text(pair, style="bold #FF8800"))
                    elif not interesting:
                        result.append(Text(pair, style="dim"))
                    else:
                        result.append(pair)
                    if j == 7 and len(chunk) > 8:
                        result.append("  ")
                    elif j < len(chunk) - 1:
                        result.append(" ")

                remaining = 16 - len(chunk)
                if remaining > 0:
                    hex_width = len(chunk) * 3 - 1
                    if len(chunk) > 8:
                        hex_width += 1
                    padding = 48 - hex_width
                    result.append(" " * padding)

                result.append(" │ ", style="dim cyan")
                for b in chunk:
                    result.append(chr(b) if 32 <= b < 127 else "·")

                if gidx < len(self._group_data) - 1 or i + 16 < len(data):
                    result.append("\n")

        return result


class EditVariableScreen(ModalScreen[str | None]):
    """Modal dialog for editing a variable value."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    EditVariableScreen {
        align: center middle;
        background: $surface 30%;
    }
    #edit-dialog {
        width: 60;
        height: auto;
        max-height: 14;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-title {
        text-style: bold;
        margin-bottom: 1;
        color: $text;
    }
    #edit-info {
        color: $text-muted;
        margin-bottom: 1;
    }
    #edit-input {
        margin-bottom: 1;
    }
    #edit-hint {
        color: $text-disabled;
    }
    """

    def __init__(self, variable: Variable, current_value: str):
        super().__init__()
        self._variable = variable
        self._current_value = current_value

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit: {self._variable.display_name}", id="edit-title")
            yield Label(
                f"Type: {self._variable.type.value}  │  Spec: {self._variable.spec}  │  Current: {self._current_value}",
                id="edit-info",
            )
            yield Input(
                value=self._current_value,
                placeholder="Enter new value…",
                id="edit-input",
            )
            hint = "Enter to confirm · Escape to cancel"
            if self._variable.type == DataType.BIT:
                hint += " · Values: 0/1, true/false, on/off"
            elif self._variable.type in (DataType.BYTE, DataType.WORD, DataType.DWORD):
                hint += " · Hex: 0xFF"
            yield Label(hint, id="edit-hint")

    def on_mount(self) -> None:
        self.query_one("#edit-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandBarScreen(ModalScreen[str | None]):
    """Command bar for raw operations (vim-style : commands)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    CommandBarScreen {
        align: center bottom;
        background: $surface 30%;
    }
    #cmd-bar {
        width: 100%;
        height: auto;
        max-height: 5;
        background: $surface;
        padding: 0 1;
        dock: bottom;
    }
    #cmd-input {
        width: 100%;
    }
    #cmd-hint {
        color: $text-disabled;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-bar"):
            yield Input(placeholder="write <db> <offset> <hex_bytes>  │  set <var_spec> <value>", id="cmd-input")
            yield Label(
                "Examples: write 210 0 FF 01  │  set DB210.Byte0 42  │  Escape to cancel",
                id="cmd-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmWriteScreen(ModalScreen[bool]):
    """Confirmation dialog shown before any write to the PLC."""

    BINDINGS = [
        Binding("y", "confirm", "Yes, write"),
        Binding("n", "cancel", "No, cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ConfirmWriteScreen {
        align: center middle;
        background: $surface 30%;
    }
    #confirm-dialog {
        width: 70;
        height: auto;
        max-height: 14;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #confirm-detail {
        color: $text;
        margin-bottom: 1;
    }
    #confirm-bytes {
        color: $warning;
        margin-bottom: 1;
    }
    #confirm-hint {
        color: $text-disabled;
    }
    """

    def __init__(self, pending: PendingWrite):
        super().__init__()
        self._pending = pending

    def compose(self) -> ComposeResult:
        hex_str = " ".join(f"{b:02X}" for b in self._pending.data)
        with Vertical(id="confirm-dialog"):
            yield Label("⚠  Confirm Write to PLC", id="confirm-title")
            yield Label(self._pending.description, id="confirm-detail", markup=False)
            yield Label(
                f"{self._pending.target_label} offset {self._pending.offset}: [{hex_str}] ({len(self._pending.data)} bytes)",
                id="confirm-bytes",
                markup=False,
            )
            if self._pending.detail:
                yield Label(self._pending.detail, id="confirm-extra", markup=False)
            yield Label("Press Y to confirm write · N or Escape to cancel", id="confirm-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class S7MonitorApp(App):
    """S7 PLC Data Block Monitor."""

    TITLE = "S7 Monitor"
    HORIZONTAL_BREAKPOINTS = [(120, "two-column")]

    CSS = """
    Screen {
        background: $surface;
    }
    #main-container {
        height: 1fr;
        padding: 0 1;
    }
    ConnectionStatus {
        height: 1;
        background: $primary-background;
        color: $text;
    }
    HexDumpDisplay {
        height: auto;
        max-height: 24;
        margin: 0 0 1 0;
    }
    HexDumpDisplay.expanded {
        max-height: 36;
    }
    #var-tables {
        height: 1fr;
        min-height: 5;
    }
    Screen.two-column #var-tables {
        layout: horizontal;
    }
    #var-table-input, #var-table-output {
        height: 1fr;
        min-width: 30;
    }
    Screen.two-column #var-table-input, Screen.two-column #var-table-output {
        width: 1fr;
    }
    #log-panel {
        height: 6;
        border-top: solid $primary;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("e", "edit_variable", "Edit"),
        Binding("colon", "command_bar", "Command"),
        Binding("space", "toggle_bit", "Toggle Bit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("c", "reconnect", "Reconnect"),
        Binding("w", "cycle_write_mode", "Write Mode"),
        Binding("h", "toggle_hex", "Hex Dump"),
    ]

    paused: reactive[bool] = reactive(False)
    write_mode: reactive[WriteMode] = reactive(WriteMode.DISABLED)
    _pending_write: PendingWrite | None = None

    def __init__(
        self,
        connection: Connection,
        variables: list[Variable],
        read_groups: list[ReadGroup],
        poll_interval: float = 1.0,
        write_mode: WriteMode = WriteMode.DISABLED,
        log_file: str | None = None,
        log_format: LogFormat = LogFormat.CSV,
        rules_engine: RulesEngine | None = None,
    ):
        super().__init__()
        self._connection = connection
        self._variables = variables
        self._read_groups = read_groups
        self._poll_interval = poll_interval
        self.write_mode = write_mode
        self._log_file = log_file
        self._log_format = log_format
        self._rules_engine = rules_engine
        self._data_logger: DataLogger | None = None
        self._current_data: dict[str, tuple[bytearray, int]] = {}  # keyed by group label
        self._current_values: dict[str, str] = {}
        self._previous_values: dict[str, str] = {}
        self._poll_count = 0
        self._poll_timer = None
        self._row_keys: dict[int, str] = {}
        self._row_key_to_var: dict = {}
        self._previous_hex_data: dict[str, bytearray] = {}
        self._flash_active: set[str] = set()
        self._tables: dict[str, DataTable] = {}
        self._hex_dump: HexDumpDisplay | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield ConnectionStatus(id="conn-status")
        with Vertical(id="main-container"):
            yield HexDumpDisplay(id="hex-dump")
            with Vertical(id="var-tables"):
                yield DataTable(id="var-table-input")
                yield DataTable(id="var-table-output")
            yield RichLog(id="log-panel", highlight=True, markup=True)
        yield Footer()

    # Column key constants for DataTable
    COL_AREA = "col_area"
    COL_VARIABLE = "col_variable"
    COL_TYPE = "col_type"
    COL_OFFSET = "col_offset"
    COL_VALUE = "col_value"
    COL_RAW_HEX = "col_raw_hex"

    @staticmethod
    def _var_side(var: Variable) -> str:
        """Which table a variable belongs in: inputs on one, everything writable on the other."""
        return "input" if var.is_input else "output"

    def _setup_table(self, table_id: str) -> DataTable:
        table = self.query_one(f"#{table_id}", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Area", key=self.COL_AREA)
        table.add_column("Variable", key=self.COL_VARIABLE)
        table.add_column("Type", key=self.COL_TYPE)
        table.add_column("Offset", key=self.COL_OFFSET)
        table.add_column("Value", key=self.COL_VALUE)
        table.add_column("Raw Hex", key=self.COL_RAW_HEX)
        return table

    def on_mount(self) -> None:
        self._hex_dump = self.query_one("#hex-dump", HexDumpDisplay)
        self._tables["input"] = self._setup_table("var-table-input")
        self._tables["output"] = self._setup_table("var-table-output")

        self._interesting_abs: dict[str, set[int]] = {}
        for var in self._variables:
            key = self._group_key_for_var(var)
            self._interesting_abs.setdefault(key, set()).update(
                range(var.offset, var.offset + var.byte_size)
            )

        # Pre-compute all interesting offsets for hex dump (config-derived, stable)
        self._all_interesting_abs: set[int] = set()
        for group in self._read_groups:
            offsets = self._interesting_abs.get(group.key)
            if offsets is not None:
                self._all_interesting_abs.update(
                    o for o in offsets if group.start <= o < group.start + group.size
                )

        for var in self._variables:
            side = self._var_side(var)
            table = self._tables[side]
            row_key = f"var_{id(var)}"
            self._row_keys[id(var)] = row_key
            self._row_key_to_var[row_key] = var
            table.add_row(
                str(var.source),
                var.display_name,
                var.type.value,
                var.offset_display,
                "—",
                "—",
                key=row_key,
            )

        # Set connection info
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.config_text = self._connection.config.display
        conn_status.state = self._connection.state
        conn_status.write_mode = self.write_mode

        log = self.query_one("#log-panel", RichLog)
        log.write("[bold]S7 Monitor[/bold] ready. Connecting…")
        if self._rules_engine is not None:
            log.write(f"[dim]Rules engine loaded: {len(self._rules_engine.rules)} rule(s)[/dim]")
        else:
            log.write("[dim]No rules engine[/dim]")

        # Start data logger if configured
        if self._log_file:
            try:
                metadata = SessionMetadata(
                    started=datetime.now(timezone.utc).isoformat(),
                    address=self._connection.config.display,
                    variables=[v.spec for v in self._variables],
                    poll_interval=self._poll_interval,
                    format=self._log_format.value,
                )
                self._data_logger = DataLogger(self._log_file, self._log_format, metadata)
                self._data_logger.open()
                log.write(f"[dim]Logging to {self._log_file} ({self._log_format.value})[/dim]")
            except Exception as e:
                log.write(f"[red]Failed to open log file: {e}[/red]")

        # Start the connection and polling
        self._connect_and_poll()

    @work(thread=True)
    def _connect_and_poll(self) -> None:
        """Connect and start the polling loop in a worker thread."""
        log = self.query_one("#log-panel", RichLog)

        if not self._connection.connected:
            try:
                self._connection.connect()
                self.call_from_thread(self._update_connection_state)
                self.call_from_thread(log.write, f"[green]Connected to {self._connection.config.display}[/green]")
            except Exception as e:
                self.call_from_thread(self._update_connection_state)
                self.call_from_thread(log.write, f"[red]Connection failed: {e}[/red]")
                return

        # Start polling timer on the main thread
        self.call_from_thread(self._start_polling)

    def _start_polling(self) -> None:
        """Start the periodic polling timer."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._poll_timer = self.set_interval(self._poll_interval, self._poll_tick)

    def _poll_tick(self) -> None:
        """Called each poll interval to read data."""
        if self.paused:
            return
        self._do_read()

    @work(thread=True)
    def _do_read(self) -> None:
        """Read all area groups in a worker thread."""
        if not self._connection.connected:
            return
        try:
            # Snapshot previous data before I/O — thread-safe copy of references
            old_hex_data = self._previous_hex_data.copy()

            results: dict[str, tuple[bytearray, int]] = {}
            for group in self._read_groups:
                result = self._connection.read_source(group.source, group.start, group.size)
                results[group.key] = (result.data, group.start)

            # Compute per-group changed offsets in the worker thread
            changed_offsets: dict[str, set[int]] = {}
            for group in self._read_groups:
                entry = results.get(group.key)
                if entry is None:
                    continue
                data, start = entry
                prev = old_hex_data.get(group.key)
                if prev is not None:
                    offsets: set[int] = set()
                    min_len = min(len(prev), len(data))
                    for k in range(min_len):
                        if prev[k] != data[k]:
                            offsets.add(start + k)
                    changed_offsets[group.key] = offsets
                else:
                    changed_offsets[group.key] = set()

            self.call_from_thread(self._on_data_received, results, changed_offsets)

            if self._rules_engine is not None:
                self._apply_rules(results)
        except Exception as e:
            log = self.query_one("#log-panel", RichLog)
            self.call_from_thread(log.write, f"[red]Read error: {e}[/red]")
            self.call_from_thread(self._update_connection_state)

    def _apply_rules(self, buffers: dict[str, tuple[bytearray, int]]) -> None:
        if self._rules_engine is None:
            return
        self._rules_engine.apply(self._connection, {}, buffers)

    def trigger_pulse(self, target: str) -> None:
        if self._rules_engine is None:
            raise KeyError(f"No pulse rule for {target!r} (no rules configured)")
        self._rules_engine.trigger_pulse(target)

    def _group_key_for_var(self, var) -> str:
        """Get the read group key for a variable."""
        return str(var.source)

    def _apply_cell_updates(
        self, updates: list[tuple[DataTable, str, str, object]]
    ) -> None:
        """Apply batched cell updates, refreshing each table once."""
        touched: set[DataTable] = set()
        for table, row_key_str, col_key_str, value in updates:
            rk = RowKey(row_key_str)
            ck = ColumnKey(col_key_str)
            if rk not in table._row_locations or ck not in table._column_locations:
                continue
            table._data[rk][ck] = value
            touched.add(table)
        for table in touched:
            table._update_count += 1
        # Throttle refresh to at most once per poll interval
        now = time.monotonic()
        if now - getattr(self, '_last_table_refresh', 0.0) > self._poll_interval:
            self._last_table_refresh = now
            for table in touched:
                table.refresh()

    def _on_data_received(
        self,
        results: dict[str, tuple[bytearray, int]],
        changed_offsets: dict[str, set[int]],
    ) -> None:
        """Process received data from all groups on the main thread."""
        self._current_data = results
        self._poll_count += 1

        # Persist data for next poll's diff
        for group_key, (data, _) in results.items():
            self._previous_hex_data[group_key] = bytearray(data)

        # Build hex dump — use pre-computed changed offsets, no byte loop
        hex_groups: list[tuple[str, bytearray, int]] = []
        all_changed_abs: set[int] = set()
        for group in self._read_groups:
            entry = results.get(group.key)
            if entry is None:
                continue
            data, start = entry
            hex_groups.append((group.label, data, start))
            offsets = changed_offsets.get(group.key)
            if offsets:
                all_changed_abs.update(offsets)

        all_interesting = self._all_interesting_abs or None

        hd = self._hex_dump
        assert hd is not None
        # Skip hex dump refresh when nothing changed and no stale flash
        if all_changed_abs or not hd._group_data:
            hd.set_data(hex_groups, all_changed_abs, interesting_abs_offsets=all_interesting or None)
        elif hd._changed_abs_offsets:
            hd._changed_abs_offsets = set()
            hd.refresh(layout=False)

        # Update connection status
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.poll_count = self._poll_count

        # Quick exit when nothing changed, no flash to clear, and
        # values already populated (don't skip first poll).
        if not all_changed_abs and not self._flash_active and self._current_values:
            return

        # Update variable tables — only process variables in groups where
        # bytes actually changed.  On first poll (_current_values is empty)
        # process all variables unconditionally.
        is_first = not self._current_values
        self._previous_values = dict(self._current_values)
        cell_updates: list[tuple[DataTable, str, str, object]] = []

        for var in self._variables:
            group_key = self._group_key_for_var(var)
            var_offsets = changed_offsets.get(group_key)
            if var_offsets is None:
                continue
            if not is_first and not var_offsets:
                # No byte changes — only process to clear flash
                if var.spec not in self._flash_active:
                    continue
            elif not is_first:
                # Bytes changed — skip if this var's range doesn't overlap
                var_end = var.offset + var.byte_size
                if not any(var.offset <= o < var_end for o in var_offsets):
                    continue

            group_data = results.get(group_key)
            if group_data is None:
                continue
            data, data_start = group_data
            try:
                value = extract_value(var, data, data_start)
                formatted = var.format_value(value)
                self._current_values[var.spec] = formatted

                local_offset = var.offset - data_start
                raw_bytes = data[local_offset : local_offset + var.byte_size]
                raw_hex = " ".join(f"{b:02X}" for b in raw_bytes)

                prev = self._previous_values.get(var.spec)
                changed = prev is not None and prev != formatted

                # Log change to file
                if changed and self._data_logger is not None:
                    self._data_logger.log(LogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        variable=var.display_name,
                        type=var.type.value,
                        area=str(var.source),
                        offset=var.offset,
                        old_value=prev or "",
                        new_value=formatted,
                        raw_hex=raw_hex,
                    ))

                row_key = self._row_keys.get(id(var))
                if row_key is None:
                    continue
                side = self._var_side(var)
                table = self._tables[side]
                if prev is None:
                    cell_updates.append((table, row_key, self.COL_VALUE, formatted))
                    cell_updates.append((table, row_key, self.COL_RAW_HEX, raw_hex))
                    self._flash_active.discard(var.spec)
                elif changed:
                    cell_updates.append((table, row_key, self.COL_VALUE, Text(formatted, style="bold yellow")))
                    cell_updates.append((table, row_key, self.COL_RAW_HEX, raw_hex))
                    self._flash_active.add(var.spec)
                elif var.spec in self._flash_active:
                    cell_updates.append((table, row_key, self.COL_VALUE, formatted))
                    cell_updates.append((table, row_key, self.COL_RAW_HEX, raw_hex))
                    self._flash_active.discard(var.spec)

            except Exception as e:
                row_key = self._row_keys.get(id(var))
                if row_key is not None:
                    side = self._var_side(var)
                    cell_updates.append((self._tables[side], row_key, self.COL_VALUE, Text(f"ERR: {e}", style="red")))

        self._apply_cell_updates(cell_updates)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        var = self._row_key_to_var.get(event.row_key.value)
        if var is None:
            return
        group_key = self._group_key_for_var(var)
        if group_key not in self._current_data:
            return
        hd = self._hex_dump
        if hd is None or hd.collapsed:
            return
        hd.set_selected_offsets(group_key, set(range(var.offset, var.offset + var.byte_size)))

    def _update_connection_state(self) -> None:
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.state = self._connection.state

    def _check_write_allowed(self) -> bool:
        """Check if writes are permitted in the current mode. Logs if blocked."""
        if self.write_mode == WriteMode.DISABLED:
            log = self.query_one("#log-panel", RichLog)
            log.write("[dim red]Writes disabled — press W to change write mode[/dim red]")
            return False
        return True

    def _focused_table(self) -> DataTable | None:
        for table in self._tables.values():
            if table.has_focus:
                return table
        return None

    def action_edit_variable(self) -> None:
        """Open edit dialog for the selected variable."""
        if not self._check_write_allowed():
            return
        table = self._focused_table()
        if table is None or table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        var = self._row_key_to_var.get(row_key.value)
        if var is None:
            return
        current = self._current_values.get(var.spec, "")
        self.push_screen(EditVariableScreen(var, current), self._on_edit_result)

    def _on_edit_result(self, result: str | None) -> None:
        """Handle the result of editing a variable."""
        if result is None:
            return  # Cancelled
        table = self._focused_table()
        if table is None:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        var = self._row_key_to_var.get(row_key.value)
        if var is None:
            return
        self._prepare_variable_write(var, result)

    @work(thread=True)
    def _prepare_variable_write(self, var: Variable, text: str) -> None:
        """Prepare a variable write and show confirmation dialog."""
        log = self.query_one("#log-panel", RichLog)
        try:
            parsed = var.parse_input(text)

            if var.type == DataType.BIT:
                if not isinstance(parsed, bool):
                    raise TypeError("Bit writes require a boolean value")
                result = self._connection.read_source(var.source, var.offset, 1)
                encoded = var.encode_bit(result.data[0], parsed)
            else:
                encoded = var.encode(parsed)

            pending = PendingWrite(
                description=f"Set {var.display_name} = {parsed}",
                source=var.source,
                offset=var.offset,
                data=encoded,
                detail=f"Variable: {var.spec} ({var.type.value})",
            )
            self.call_from_thread(self._confirm_and_write, pending)
        except Exception as e:
            log_error(f"Encode failed for {var.display_name}: {e}")
            self.call_from_thread(log.write, f"[red]Encode failed for {var.display_name}: {e}[/red]")

    def _confirm_and_write(self, pending: PendingWrite) -> None:
        """Route write through confirmation or execute directly based on write mode."""
        if self.write_mode == WriteMode.DISABLED:
            log = self.query_one("#log-panel", RichLog)
            log.write("[dim red]Write blocked — writes are disabled[/dim red]")
            return
        if self.write_mode == WriteMode.ALLOWED:
            self._execute_write(pending)
            return
        # WriteMode.CONFIRM — show confirmation dialog
        self._pending_write = pending
        self.push_screen(ConfirmWriteScreen(pending), self._on_confirm_result)

    def _on_confirm_result(self, confirmed: bool | None) -> None:
        """Handle confirmation dialog result."""
        if not confirmed or self._pending_write is None:
            log = self.query_one("#log-panel", RichLog)
            log.write("[yellow]Write cancelled[/yellow]")
            self._pending_write = None
            return
        self._execute_write(self._pending_write)
        self._pending_write = None

    @work(thread=True)
    def _execute_write(self, pending: PendingWrite) -> None:
        """Execute a confirmed write to the PLC."""
        log = self.query_one("#log-panel", RichLog)
        try:
            self._connection.write_source(pending.source, pending.offset, pending.data)
            hex_str = " ".join(f"{b:02X}" for b in pending.data)
            self.call_from_thread(log.write, f"[green]✓ {pending.description} [{hex_str}][/green]")
            self.call_from_thread(self._do_read)
        except Exception as e:
            self.call_from_thread(log.write, f"[red]Write failed: {e}[/red]")

    def action_toggle_bit(self) -> None:
        """Toggle a Bit variable with confirmation."""
        if not self._check_write_allowed():
            return
        table = self._focused_table()
        if table is None or table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        var = self._row_key_to_var.get(row_key.value)
        if var is None or var.type != DataType.BIT:
            log = self.query_one("#log-panel", RichLog)
            log.write("[yellow]Toggle only works on Bit variables[/yellow]")
            return
        current = self._current_values.get(var.spec, "0")
        new_val = "0" if current == "1" else "1"
        self._prepare_variable_write(var, new_val)

    def action_command_bar(self) -> None:
        """Open the command bar for raw operations."""
        if not self._check_write_allowed():
            return
        self.push_screen(CommandBarScreen(), self._on_command_result)

    def _on_command_result(self, result: str | None) -> None:
        """Handle command bar input."""
        if result is None:
            return
        self._execute_command(result)

    @work(thread=True)
    def _execute_command(self, cmd_text: str) -> None:
        """Parse a command and prepare write with confirmation, or execute read."""
        log = self.query_one("#log-panel", RichLog)
        parts = cmd_text.strip().split()
        if not parts:
            return

        command = parts[0].lower()

        if command == "write" and len(parts) >= 4:
            # write <db> <offset> <hex_bytes...>
            try:
                db = int(parts[1])
                offset = int(parts[2])
                hex_bytes = bytearray(int(b, 16) for b in parts[3:])
                pending = PendingWrite(
                    description=f"Raw write to DB{db} at offset {offset}",
                    source=DataSource.s7_db(db),
                    offset=offset,
                    data=hex_bytes,
                )
                self.call_from_thread(self._confirm_and_write, pending)
            except Exception as e:
                log_error(f"Write command failed: {e}")
                self.call_from_thread(log.write, f"[red]Write command parse error: {e}[/red]")

        elif command == "set" and len(parts) >= 3:
            # set <var_spec> <value>  (supports DB and area specs)
            try:
                var = Variable.parse(parts[1])
                value_text = " ".join(parts[2:])
                parsed = var.parse_input(value_text)

                if var.type == DataType.BIT:
                    if not isinstance(parsed, bool):
                        raise TypeError("Bit writes require a boolean value")
                    result = self._connection.read_source(var.source, var.offset, 1)
                    encoded = var.encode_bit(result.data[0], parsed)
                else:
                    encoded = var.encode(parsed)

                pending = PendingWrite(
                    description=f"Set {var.spec} = {parsed}",
                    source=var.source,
                    offset=var.offset,
                    data=encoded,
                    detail=f"Variable: {var.spec} ({var.type.value})",
                )
                self.call_from_thread(self._confirm_and_write, pending)
            except Exception as e:
                log_error(f"Set command failed: {e}")
                self.call_from_thread(log.write, f"[red]Set command failed: {e}[/red]")

        elif command == "read" and len(parts) >= 4:
            # read <db> <offset> <size>  — reads are safe, no confirmation needed
            try:
                db = int(parts[1])
                offset = int(parts[2])
                size = int(parts[3])
                result = self._connection.read_source(DataSource.s7_db(db), offset, size)
                hex_str = " ".join(f"{b:02X}" for b in result.data)
                self.call_from_thread(log.write, f"DB{db}[{offset}:{offset+size}]: {hex_str}")
            except Exception as e:
                self.call_from_thread(log.write, f"[red]Read command failed: {e}[/red]")

        elif command == "pulse" and len(parts) >= 2:
            target = parts[1]
            try:
                self.trigger_pulse(target)
                self.call_from_thread(log.write, f"[green]Pulsed {target!r}[/green]")
            except KeyError as e:
                self.call_from_thread(log.write, f"[red]{e}[/red]")
            except Exception as e:
                log_error(f"Pulse command failed: {e}")
                self.call_from_thread(log.write, f"[red]Pulse failed: {e}[/red]")

        else:
            self.call_from_thread(
                log.write,
                f"[yellow]Unknown command: {cmd_text}. Try: write | set | read | pulse <target>[/yellow]",
            )

    def action_force_refresh(self) -> None:
        """Force an immediate data read."""
        self._do_read()

    def action_cycle_write_mode(self) -> None:
        """Cycle through write modes: disabled → confirm → allowed → disabled."""
        cycle = {
            WriteMode.DISABLED: WriteMode.CONFIRM,
            WriteMode.CONFIRM: WriteMode.ALLOWED,
            WriteMode.ALLOWED: WriteMode.DISABLED,
        }
        self.write_mode = cycle[self.write_mode]
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.write_mode = self.write_mode
        log = self.query_one("#log-panel", RichLog)
        labels = {
            WriteMode.DISABLED: "[dim red]Write mode: disabled (read-only)[/dim red]",
            WriteMode.CONFIRM: "[yellow]Write mode: confirm (Y/N prompt)[/yellow]",
            WriteMode.ALLOWED: "[green]Write mode: allowed (no confirmation)[/green]",
        }
        log.write(labels[self.write_mode])

    def action_toggle_pause(self) -> None:
        """Toggle polling pause."""
        self.paused = not self.paused
        log = self.query_one("#log-panel", RichLog)
        if self.paused:
            log.write("[yellow]Polling paused[/yellow]")
        else:
            log.write("[green]Polling resumed[/green]")

    def action_toggle_hex(self) -> None:
        """Toggle hex dump collapse."""
        hd = self._hex_dump
        assert hd is not None
        hd.collapsed = not hd.collapsed
        log = self.query_one("#log-panel", RichLog)
        if hd.collapsed:
            log.write("[dim]Hex dump collapsed[/dim]")
        else:
            log.write("[dim]Hex dump expanded[/dim]")

    def action_reconnect(self) -> None:
        """Reconnect to the PLC."""
        log = self.query_one("#log-panel", RichLog)
        log.write("Reconnecting…")
        self._connection.disconnect()
        self._update_connection_state()
        self._connect_and_poll()

    def on_unmount(self) -> None:
        """Clean up resources when the app exits."""
        if self._data_logger is not None:
            self._data_logger.close()
        try:
            self._connection.disconnect()
        except Exception:
            pass
