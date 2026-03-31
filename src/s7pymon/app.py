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
from .variable import S7Type, S7Variable, compute_read_range, extract_value


@dataclass
class PendingWrite:
    """Describes a write operation awaiting confirmation."""

    description: str  # Human-readable summary
    db: int
    offset: int
    data: bytearray
    # For display purposes
    detail: str = ""


def format_hex_dump(data: bytearray, start_offset: int = 0, bytes_per_line: int = 16) -> str:
    """Format raw bytes as a hex dump with offset, hex values, and ASCII."""
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i : i + bytes_per_line]
        offset = start_offset + i
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        # Add gap in middle
        if len(chunk) > 8:
            hex_left = " ".join(f"{b:02X}" for b in chunk[:8])
            hex_right = " ".join(f"{b:02X}" for b in chunk[8:])
            hex_part = f"{hex_left}  {hex_right}"
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
        hex_padded = hex_part.ljust(3 * bytes_per_line + 1)
        lines.append(f"  {offset:04X} │ {hex_padded}│ {ascii_part}")
    return "\n".join(lines)


class ConnectionStatus(Static):
    """Displays connection state in the header area."""

    state: reactive[ConnectionState] = reactive(ConnectionState.DISCONNECTED)
    config_text: reactive[str] = reactive("")
    poll_count: reactive[int] = reactive(0)

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
            yield Label(self._pending.description, id="confirm-detail")
            yield Label(
                f"DB{self._pending.db} offset {self._pending.offset}: [{hex_str}] ({len(self._pending.data)} bytes)",
                id="confirm-bytes",
            )
            if self._pending.detail:
                yield Label(self._pending.detail, id="confirm-extra")
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
    ]

    paused: reactive[bool] = reactive(False)
    _pending_write: PendingWrite | None = None

    def __init__(
        self,
        connection: S7Connection,
        variables: list[S7Variable],
        db: int,
        start: int,
        size: int,
        poll_interval: float = 1.0,
    ):
        super().__init__()
        self._connection = connection
        self._variables = variables
        self._db = db
        self._start = start
        self._size = size
        self._poll_interval = poll_interval
        self._current_data: bytearray | None = None
        self._previous_data: bytearray | None = None
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
        table.add_column("Variable", key=self.COL_VARIABLE)
        table.add_column("Type", key=self.COL_TYPE)
        table.add_column("Offset", key=self.COL_OFFSET)
        table.add_column("Value", key=self.COL_VALUE)
        table.add_column("Raw Hex", key=self.COL_RAW_HEX)

        for var in self._variables:
            table.add_row(
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

        hex_dump = self.query_one("#hex-dump", HexDumpDisplay)
        hex_dump.db_label = f"DB{self._db}"

        log = self.query_one("#log-panel", RichLog)
        log.write("[bold]S7 Monitor[/bold] ready. Connecting…")

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
        """Read DB data in a worker thread."""
        if not self._connection.connected:
            return
        try:
            result = self._connection.db_read(self._db, self._start, self._size)
            self.call_from_thread(self._on_data_received, result.data)
        except Exception as e:
            log = self.query_one("#log-panel", RichLog)
            self.call_from_thread(log.write, f"[red]Read error: {e}[/red]")
            self.call_from_thread(self._update_connection_state)

    def _on_data_received(self, data: bytearray) -> None:
        """Process received data on the main thread."""
        self._previous_data = self._current_data
        self._current_data = data
        self._poll_count += 1

        # Update hex dump
        hex_dump = self.query_one("#hex-dump", HexDumpDisplay)
        hex_dump.hex_content = format_hex_dump(data, self._start)

        # Update variable table
        self._previous_values = dict(self._current_values)
        table = self.query_one("#var-table", DataTable)

        for var in self._variables:
            try:
                value = extract_value(var, data, self._start)
                formatted = var.format_value(value)
                self._current_values[var.spec] = formatted

                # Get raw hex for this variable's bytes
                local_offset = var.offset - self._start
                raw_bytes = data[local_offset : local_offset + var.byte_size]
                raw_hex = " ".join(f"{b:02X}" for b in raw_bytes)

                # Determine change styling
                changed = self._previous_values.get(var.spec) != formatted and self._previous_values.get(var.spec) is not None
                style = "bold yellow" if changed else ""

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

    def action_edit_variable(self) -> None:
        """Open edit dialog for the selected variable."""
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
                result = self._connection.db_read(self._db, var.offset, 1)
                encoded = var.encode_bit(result.data[0], parsed)
            else:
                encoded = var.encode(parsed)

            pending = PendingWrite(
                description=f"Set {var.display_name} = {parsed}",
                db=self._db,
                offset=var.offset,
                data=encoded,
                detail=f"Variable: {var.spec} ({var.type.value})",
            )
            self.call_from_thread(self._confirm_and_write, pending)
        except Exception as e:
            self.call_from_thread(log.write, f"[red]Encode failed for {var.display_name}: {e}[/red]")

    def _confirm_and_write(self, pending: PendingWrite) -> None:
        """Show confirmation dialog then execute write if confirmed."""
        self._pending_write = pending
        self.push_screen(ConfirmWriteScreen(pending), self._on_confirm_result)

    def _on_confirm_result(self, confirmed: bool) -> None:
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
            self._connection.db_write(pending.db, pending.offset, pending.data)
            hex_str = " ".join(f"{b:02X}" for b in pending.data)
            self.call_from_thread(log.write, f"[green]✓ {pending.description} [{hex_str}][/green]")
            self.call_from_thread(self._do_read)
        except Exception as e:
            self.call_from_thread(log.write, f"[red]Write failed: {e}[/red]")

    def action_toggle_bit(self) -> None:
        """Toggle a Bit variable with confirmation."""
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
                    db=db,
                    offset=offset,
                    data=hex_bytes,
                )
                self.call_from_thread(self._confirm_and_write, pending)
            except Exception as e:
                self.call_from_thread(log.write, f"[red]Write command parse error: {e}[/red]")

        elif command == "set" and len(parts) >= 3:
            # set <var_spec> <value>
            try:
                var = S7Variable.parse(parts[1])
                value_text = " ".join(parts[2:])
                parsed = var.parse_input(value_text)

                if var.type == S7Type.BIT:
                    result = self._connection.db_read(var.db, var.offset, 1)
                    encoded = var.encode_bit(result.data[0], parsed)
                else:
                    encoded = var.encode(parsed)

                pending = PendingWrite(
                    description=f"Set {var.spec} = {parsed}",
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
