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

from .connection import ConnectionConfig, ConnectionState, S7Connection
from .engine import WriteMode, format_hex_dump
from .logging import DataLogger, LogEntry, LogFormat, SessionMetadata
from .variable import S7Area, S7Type, S7Variable, compute_read_range, extract_value

__all__ = ["S7MonitorApp", "WriteMode", "format_hex_dump", "ReadGroup"]


@dataclass
class PendingWrite:
    """Describes a write operation awaiting confirmation."""

    description: str  # Human-readable summary
    area: S7Area
    db: int  # 0 for non-DB areas
    offset: int
    data: bytearray
    # For display purposes
    detail: str = ""

    @property
    def target_label(self) -> str:
        if self.area == S7Area.DB:
            return f"DB{self.db}"
        return self.area.value


@dataclass
class ReadGroup:
    """A group of variables in the same area/DB to be read together."""

    area: S7Area
    db: int  # 0 for non-DB areas
    start: int
    size: int

    @property
    def label(self) -> str:
        if self.area == S7Area.DB:
            return f"DB{self.db}"
        return f"{self.area.value} ({self.area.description})"

    @property
    def key(self) -> str:
        if self.area == S7Area.DB:
            return f"DB{self.db}"
        return self.area.value


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
    """Live hex dump of the DB contents."""

    hex_content: reactive[str] = reactive("  No data yet")
    db_label: reactive[str] = reactive("DB???")

    def render(self) -> Text:
        title = Text(f"  ─── {self.db_label} ", style="bold cyan")
        title.append("─" * max(0, 62 - len(self.db_label) - 6), style="dim cyan")
        return Text.assemble(title, "\n", Text(self.hex_content, style=""), "\n")


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

    def __init__(self, variable: S7Variable, current_value: str):
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
            if self._variable.type == S7Type.BIT:
                hint += " · Values: 0/1, true/false, on/off"
            elif self._variable.type in (S7Type.BYTE, S7Type.WORD, S7Type.DWORD):
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
        max-height: 12;
        margin: 0 0 1 0;
    }
    #var-table {
        height: 1fr;
        min-height: 5;
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
    ]

    paused: reactive[bool] = reactive(False)
    write_mode: reactive[WriteMode] = reactive(WriteMode.DISABLED)
    _pending_write: PendingWrite | None = None

    def __init__(
        self,
        connection: S7Connection,
        variables: list[S7Variable],
        read_groups: list[ReadGroup],
        poll_interval: float = 1.0,
        write_mode: WriteMode = WriteMode.DISABLED,
        log_file: str | None = None,
        log_format: LogFormat = LogFormat.CSV,
    ):
        super().__init__()
        self._connection = connection
        self._variables = variables
        self._read_groups = read_groups
        self._poll_interval = poll_interval
        self.write_mode = write_mode
        self._log_file = log_file
        self._log_format = log_format
        self._data_logger: DataLogger | None = None
        self._current_data: dict[str, tuple[bytearray, int]] = {}  # keyed by group label
        self._current_values: dict[str, str] = {}
        self._previous_values: dict[str, str] = {}
        self._poll_count = 0
        self._poll_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield ConnectionStatus(id="conn-status")
        with Vertical(id="main-container"):
            yield HexDumpDisplay(id="hex-dump")
            yield DataTable(id="var-table")
            yield RichLog(id="log-panel", highlight=True, markup=True)
        yield Footer()

    # Column key constants for DataTable
    COL_AREA = "col_area"
    COL_VARIABLE = "col_variable"
    COL_TYPE = "col_type"
    COL_OFFSET = "col_offset"
    COL_VALUE = "col_value"
    COL_RAW_HEX = "col_raw_hex"

    def on_mount(self) -> None:
        # Set up the variable table
        table = self.query_one("#var-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Area", key=self.COL_AREA)
        table.add_column("Variable", key=self.COL_VARIABLE)
        table.add_column("Type", key=self.COL_TYPE)
        table.add_column("Offset", key=self.COL_OFFSET)
        table.add_column("Value", key=self.COL_VALUE)
        table.add_column("Raw Hex", key=self.COL_RAW_HEX)

        for var in self._variables:
            area_label = f"DB{var.db}" if var.area == S7Area.DB else var.area.value
            table.add_row(
                area_label,
                var.display_name,
                var.type.value,
                str(var.offset) + (f".{var.extra}" if var.type == S7Type.BIT else ""),
                "—",
                "—",
                key=var.spec,
            )

        # Set connection info
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.config_text = self._connection.config.display
        conn_status.state = self._connection.state
        conn_status.write_mode = self.write_mode

        hex_dump = self.query_one("#hex-dump", HexDumpDisplay)
        group_labels = ", ".join(g.label for g in self._read_groups)
        hex_dump.db_label = group_labels

        log = self.query_one("#log-panel", RichLog)
        log.write("[bold]S7 Monitor[/bold] ready. Connecting…")

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
            results: dict[str, tuple[bytearray, int]] = {}
            for group in self._read_groups:
                result = self._connection.area_read(group.area, group.start, group.size, db=group.db)
                results[group.key] = (result.data, group.start)
            self.call_from_thread(self._on_data_received, results)
        except Exception as e:
            log = self.query_one("#log-panel", RichLog)
            self.call_from_thread(log.write, f"[red]Read error: {e}[/red]")
            self.call_from_thread(self._update_connection_state)

    def _group_key_for_var(self, var: S7Variable) -> str:
        """Get the read group key for a variable."""
        if var.area == S7Area.DB:
            return f"DB{var.db}"
        return var.area.value

    def _on_data_received(self, results: dict[str, tuple[bytearray, int]]) -> None:
        """Process received data from all groups on the main thread."""
        self._current_data = results
        self._poll_count += 1

        # Build combined hex dump
        hex_dump = self.query_one("#hex-dump", HexDumpDisplay)
        hex_parts = []
        for group in self._read_groups:
            if group.key in results:
                data, start = results[group.key]
                hex_parts.append(f"  ─── {group.label} ───")
                hex_parts.append(format_hex_dump(data, start))
        hex_dump.hex_content = "\n".join(hex_parts) if hex_parts else "  No data yet"

        # Update variable table
        self._previous_values = dict(self._current_values)
        table = self.query_one("#var-table", DataTable)

        for var in self._variables:
            group_key = self._group_key_for_var(var)
            group_data = results.get(group_key)
            if group_data is None:
                continue
            data, data_start = group_data
            try:
                value = extract_value(var, data, data_start)
                formatted = var.format_value(value)
                self._current_values[var.spec] = formatted

                # Get raw hex for this variable's bytes
                local_offset = var.offset - data_start
                raw_bytes = data[local_offset : local_offset + var.byte_size]
                raw_hex = " ".join(f"{b:02X}" for b in raw_bytes)

                # Determine change styling
                prev = self._previous_values.get(var.spec)
                changed = prev is not None and prev != formatted
                style = "bold yellow" if changed else ""

                # Log change to file
                if changed and self._data_logger is not None:
                    area_label = f"DB{var.db}" if var.area == S7Area.DB else var.area.value
                    self._data_logger.log(LogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        variable=var.display_name,
                        type=var.type.value,
                        area=area_label,
                        offset=var.offset,
                        old_value=prev,
                        new_value=formatted,
                        raw_hex=raw_hex,
                    ))

                # Update row
                value_display = Text(formatted, style=style) if changed else formatted
                raw_display = Text(raw_hex, style=style) if changed else raw_hex

                row_key = var.spec
                # Update existing row values
                table.update_cell(row_key, self.COL_VALUE, value_display)
                table.update_cell(row_key, self.COL_RAW_HEX, raw_display)

            except Exception as e:
                table.update_cell(var.spec, self.COL_VALUE, Text(f"ERR: {e}", style="red"))

        # Update connection status poll count
        conn_status = self.query_one("#conn-status", ConnectionStatus)
        conn_status.poll_count = self._poll_count

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

    def action_edit_variable(self) -> None:
        """Open edit dialog for the selected variable."""
        if not self._check_write_allowed():
            return
        table = self.query_one("#var-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        # Find the variable
        var = next((v for v in self._variables if v.spec == row_key.value), None)
        if var is None:
            return
        current = self._current_values.get(var.spec, "")
        self.push_screen(EditVariableScreen(var, current), self._on_edit_result)

    def _on_edit_result(self, result: str | None) -> None:
        """Handle the result of editing a variable."""
        if result is None:
            return  # Cancelled
        table = self.query_one("#var-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        var = next((v for v in self._variables if v.spec == row_key.value), None)
        if var is None:
            return
        self._prepare_variable_write(var, result)

    @work(thread=True)
    def _prepare_variable_write(self, var: S7Variable, text: str) -> None:
        """Prepare a variable write and show confirmation dialog."""
        log = self.query_one("#log-panel", RichLog)
        try:
            parsed = var.parse_input(text)

            if var.type == S7Type.BIT:
                if not isinstance(parsed, bool):
                    raise TypeError("Bit writes require a boolean value")
                result = self._connection.area_read(var.area, var.offset, 1, db=var.db)
                encoded = var.encode_bit(result.data[0], parsed)
            else:
                encoded = var.encode(parsed)

            pending = PendingWrite(
                description=f"Set {var.display_name} = {parsed}",
                area=var.area,
                db=var.db,
                offset=var.offset,
                data=encoded,
                detail=f"Variable: {var.spec} ({var.type.value})",
            )
            self.call_from_thread(self._confirm_and_write, pending)
        except Exception as e:
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
            self._connection.area_write(pending.area, pending.offset, pending.data, db=pending.db)
            hex_str = " ".join(f"{b:02X}" for b in pending.data)
            self.call_from_thread(log.write, f"[green]✓ {pending.description} [{hex_str}][/green]")
            self.call_from_thread(self._do_read)
        except Exception as e:
            self.call_from_thread(log.write, f"[red]Write failed: {e}[/red]")

    def action_toggle_bit(self) -> None:
        """Toggle a Bit variable with confirmation."""
        if not self._check_write_allowed():
            return
        table = self.query_one("#var-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        var = next((v for v in self._variables if v.spec == row_key.value), None)
        if var is None or var.type != S7Type.BIT:
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
                    area=S7Area.DB,
                    db=db,
                    offset=offset,
                    data=hex_bytes,
                )
                self.call_from_thread(self._confirm_and_write, pending)
            except Exception as e:
                self.call_from_thread(log.write, f"[red]Write command parse error: {e}[/red]")

        elif command == "set" and len(parts) >= 3:
            # set <var_spec> <value>  (supports DB and area specs)
            try:
                var = S7Variable.parse(parts[1])
                value_text = " ".join(parts[2:])
                parsed = var.parse_input(value_text)

                if var.type == S7Type.BIT:
                    if not isinstance(parsed, bool):
                        raise TypeError("Bit writes require a boolean value")
                    result = self._connection.area_read(var.area, var.offset, 1, db=var.db)
                    encoded = var.encode_bit(result.data[0], parsed)
                else:
                    encoded = var.encode(parsed)

                pending = PendingWrite(
                    description=f"Set {var.spec} = {parsed}",
                    area=var.area,
                    db=var.db,
                    offset=var.offset,
                    data=encoded,
                    detail=f"Variable: {var.spec} ({var.type.value})",
                )
                self.call_from_thread(self._confirm_and_write, pending)
            except Exception as e:
                self.call_from_thread(log.write, f"[red]Set command failed: {e}[/red]")

        elif command == "read" and len(parts) >= 4:
            # read <db> <offset> <size>  — reads are safe, no confirmation needed
            try:
                db = int(parts[1])
                offset = int(parts[2])
                size = int(parts[3])
                result = self._connection.db_read(db, offset, size)
                hex_str = " ".join(f"{b:02X}" for b in result.data)
                self.call_from_thread(log.write, f"DB{db}[{offset}:{offset+size}]: {hex_str}")
            except Exception as e:
                self.call_from_thread(log.write, f"[red]Read command failed: {e}[/red]")

        else:
            self.call_from_thread(
                log.write,
                "[yellow]Commands: write <db> <offset> <hex...> │ set <spec> <value> │ read <db> <offset> <size>[/yellow]",
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
