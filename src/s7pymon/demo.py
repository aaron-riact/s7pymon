"""Built-in browser demo for s7pymon.

This module exposes a first-class ``s7pymon-demo`` command that starts the same
web dashboard used for real PLCs, but backed by a synthetic DB buffer with
plausible changing values. That makes it easy to demo the UI without a Siemens
controller on hand.
"""

from __future__ import annotations

import math
import random
import struct
import threading

import click

from .engine import MonitorEngine, ReadGroup, WriteMode
from .protocols import Connection, ConnectionConfig, ConnectionState, DataSource, ReadResult
from .variable import S7Area, S7Variable
from .web import S7WebServer

DEMO_DB = 210
DEMO_ADDRESS = "192.168.0.50"
DEMO_VARIABLES = (
    ("DB210.Byte0", "heartbeat"),
    ("DB210.Int4", "temperature"),
    ("DB210.Real8", "pressure"),
    ("DB210.Bit2.0", "e_stop"),
    ("DB210.Bit2.1", "running"),
    ("DB210.Word12", "cycles"),
)


class DemoConnection(Connection):
    """A tiny in-memory PLC that produces changing data for the dashboard."""

    protocol = "demo"

    def __init__(self, *, tick_interval: float = 0.7, seed: int | None = None):
        self._config = ConnectionConfig(address=DEMO_ADDRESS)
        self._state = ConnectionState.DISCONNECTED
        self._error = ""
        self._tick_interval = tick_interval
        self._rng = random.Random(seed)
        self._buffers: dict[tuple[S7Area, int], bytearray] = {(S7Area.DB, DEMO_DB): bytearray(16)}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="s7pymon-demo", daemon=True)
        self._tick = 0
        with self._lock:
            self._advance_locked()

    @property
    def config(self) -> ConnectionConfig:
        return self._config

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    def connect(self) -> None:
        with self._lock:
            self._state = ConnectionState.CONNECTED
            self._error = ""
            if not self._thread.is_alive():
                self._thread.start()

    def disconnect(self) -> None:
        with self._lock:
            self._state = ConnectionState.DISCONNECTED
            self._error = ""

    def close(self) -> None:
        self._stop.set()
        self.disconnect()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def area_read(self, area: str, start: int, size: int, db: int = 0) -> ReadResult:
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            s7_area = S7Area(area)
            buf = self._buffer_for_locked(s7_area, db)
            end = start + size
            self._grow_locked(buf, end)
            return ReadResult(
                data=bytearray(buf[start:end]),
                area=area,
                db=db,
                start=start,
                size=size,
            )

    def area_write(self, area: str, start: int, data: bytearray, db: int = 0) -> None:
        with self._lock:
            if not self.connected:
                raise ConnectionError("Not connected")
            s7_area = S7Area(area)
            buf = self._buffer_for_locked(s7_area, db)
            end = start + len(data)
            self._grow_locked(buf, end)
            buf[start:end] = data

    def _run(self) -> None:
        while not self._stop.wait(self._tick_interval):
            with self._lock:
                if self.connected:
                    self._advance_locked()

    def _advance_locked(self) -> None:
        self._tick += 1
        buf = self._buffer_for_locked(S7Area.DB, DEMO_DB)

        heartbeat = self._tick % 256
        temperature = max(-32768, min(32767, int(205 + 22 * math.sin(self._tick / 3) + self._rng.randint(-3, 3))))
        pressure = 3.0 + 1.1 * math.sin(self._tick / 4) + self._rng.uniform(-0.08, 0.08)
        pressure = max(-50.0, min(50.0, pressure))
        e_stop = self._tick % 17 == 0
        running = not e_stop and self._tick % 11 not in (0, 1)
        cycles = struct.unpack_from(">H", buf, 12)[0]
        if running:
            cycles = (cycles + 7) % 65536

        buf[0] = heartbeat
        buf[2] = (0x01 if e_stop else 0x00) | (0x02 if running else 0x00)
        struct.pack_into(">h", buf, 4, temperature)
        struct.pack_into(">f", buf, 8, float(pressure))
        struct.pack_into(">H", buf, 12, cycles)

    def _buffer_for_locked(self, area: S7Area, db: int) -> bytearray:
        return self._buffers.setdefault((area, db), bytearray())

    @staticmethod
    def _grow_locked(buf: bytearray, size: int) -> None:
        if size > len(buf):
            buf.extend(b"\x00" * (size - len(buf)))


def build_demo_engine(
    *,
    poll_interval: float = 0.7,
    write_mode: WriteMode = WriteMode.ALLOWED,
    seed: int | None = None,
) -> tuple[MonitorEngine, DemoConnection]:
    """Create a demo connection and engine for the browser dashboard."""

    connection = DemoConnection(tick_interval=poll_interval, seed=seed)
    variables = [S7Variable.parse(spec, label=label) for spec, label in DEMO_VARIABLES]
    engine = MonitorEngine(
        connection=connection,
        variables=variables,
        read_groups=[ReadGroup(DataSource.s7_db(DEMO_DB), start=0, size=16)],
        poll_interval=poll_interval,
        write_mode=write_mode,
    )
    return engine, connection


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1).")
@click.option("-P", "--http-port", "http_port", default=8731, type=int, help="HTTP port (default: 8731).")
@click.option("--open", "open_browser", is_flag=True, default=False, help="Open the dashboard in a browser.")
@click.option("-i", "--interval", default=0.7, type=float, help="Poll interval in seconds (default: 0.7).")
@click.option(
    "-w", "--write-mode", "write_mode",
    type=click.Choice(["disabled", "confirm", "allowed"], case_sensitive=False),
    default="allowed",
    show_default=True,
    help="Write permission mode for the demo PLC.",
)
@click.option("--seed", default=None, type=int, help="Optional random seed for repeatable demo values.")
def demo_web_cli(
    host: str,
    http_port: int,
    open_browser: bool,
    interval: float,
    write_mode: str,
    seed: int | None,
) -> None:
    """s7pymon-demo — Launch the browser dashboard with simulated PLC data."""

    engine, connection = build_demo_engine(
        poll_interval=interval,
        write_mode=WriteMode(write_mode.lower()),
        seed=seed,
    )
    engine.connect()
    server = S7WebServer(engine, host=host, port=http_port)
    server.start()
    click.echo("Starting built-in demo PLC for the web dashboard.")
    click.echo(f"Demo source: {connection.config.display}  (synthetic DB{DEMO_DB} values)")
    click.echo(f"s7pymon-demo serving at {server.url}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nShutting down…")
    finally:
        server.shutdown()
        connection.close()
