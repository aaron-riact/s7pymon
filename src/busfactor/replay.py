"""Log viewer for replaying busfactor session logs.

Displays a previously recorded session log file in a Textual TUI,
showing session metadata and a scrollable table of all value changes.

Usage:
    busfactor-replay <log_file>
"""

from __future__ import annotations

import click
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Label, Static
from textual.containers import Vertical

from .logging import LogEntry, SessionMetadata, load_log_file


class SessionInfoDisplay(Static):
    """Shows session metadata at the top."""

    def __init__(self, metadata: SessionMetadata | None, entry_count: int, **kwargs):
        super().__init__(**kwargs)
        self._metadata = metadata
        self._entry_count = entry_count

    def render(self) -> Text:
        if self._metadata is None:
            return Text("  No session metadata found", style="dim")
        m = self._metadata
        result = Text("  Session: ", style="bold cyan")
        result.append(m.started, style="")
        result.append(f"  │  {m.address}", style="dim")
        result.append(f"  │  {len(m.variables)} variables", style="dim")
        result.append(f"  │  {self._entry_count} changes", style="dim")
        result.append(f"  │  interval: {m.poll_interval}s", style="dim")
        return result


class LogReplayApp(App):
    """TUI for viewing busfactor session logs."""

    TITLE = "busfactor — Log Replay"

    CSS = """
    Screen {
        background: $surface;
    }
    SessionInfoDisplay {
        height: 1;
        background: $primary-background;
        color: $text;
    }
    #replay-table {
        height: 1fr;
        margin: 1;
    }
    #var-summary {
        height: auto;
        max-height: 4;
        margin: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("home", "scroll_top", "Top"),
        Binding("end", "scroll_bottom", "Bottom"),
    ]

    def __init__(
        self,
        metadata: SessionMetadata | None,
        entries: list[LogEntry],
    ):
        super().__init__()
        self._metadata = metadata
        self._entries = entries

    def compose(self) -> ComposeResult:
        yield Header()
        yield SessionInfoDisplay(self._metadata, len(self._entries), id="session-info")
        if self._metadata and self._metadata.variables:
            yield Label(
                f"  Variables: {', '.join(self._metadata.variables)}",
                id="var-summary",
            )
        yield DataTable(id="replay-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#replay-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        table.add_column("Timestamp", key="ts")
        table.add_column("Variable", key="var")
        table.add_column("Type", key="type")
        table.add_column("Area", key="area")
        table.add_column("Offset", key="off")
        table.add_column("Old", key="old")
        table.add_column("New", key="new")
        table.add_column("Raw Hex", key="hex")

        for entry in self._entries:
            # Format timestamp to be more readable (strip date if same day)
            ts_display = entry.timestamp
            if "T" in ts_display:
                ts_display = ts_display.split("T")[1]
                if "+" in ts_display:
                    ts_display = ts_display.split("+")[0]

            table.add_row(
                ts_display,
                entry.variable,
                entry.type,
                entry.area,
                str(entry.offset),
                entry.old_value,
                Text(entry.new_value, style="bold yellow"),
                entry.raw_hex,
            )

    def action_scroll_top(self) -> None:
        table = self.query_one("#replay-table", DataTable)
        table.move_cursor(row=0)

    def action_scroll_bottom(self) -> None:
        table = self.query_one("#replay-table", DataTable)
        table.move_cursor(row=table.row_count - 1)


@click.command()
@click.argument("log_file", type=click.Path(exists=True))
def replay_main(log_file: str) -> None:
    """Replay a busfactor session log.

    LOG_FILE is a CSV or JSONL file written by busfactor --log-file.
    """
    try:
        metadata, entries = load_log_file(log_file)
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error loading log file: {e}", err=True)
        raise SystemExit(1)

    if not entries:
        click.echo("Log file contains no data change entries.", err=True)
        raise SystemExit(1)

    app = LogReplayApp(metadata=metadata, entries=entries)
    app.run()


def replay_cli():
    """Entry point wrapper for setuptools console_scripts."""
    replay_main()
