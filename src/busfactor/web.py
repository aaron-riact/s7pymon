"""A dependency-free web dashboard for busfactor.

This serves a live browser UI for monitoring and writing industrial controller data using
only the Python standard library: :class:`http.server.ThreadingHTTPServer`
for HTTP, **Server-Sent Events** (``text/event-stream``) for the live telemetry
feed, and JSON ``POST`` endpoints for control/writes. No web framework is
pulled in, which keeps the project dependency-light and trivially testable.

Architecture
------------
* A single :class:`MonitorEngine` owns the PLC connection and decoding.
* A background :class:`_Poller` thread calls ``engine.poll()`` on the configured
  interval and hands each snapshot to a :class:`Broadcaster`.
* The broadcaster fans every message out to all connected SSE clients (one
  ``queue.Queue`` per client) and remembers the last message so a freshly
  connected browser renders immediately instead of waiting a full interval.

Endpoints
---------
* ``GET  /``            → the dashboard HTML
* ``GET  /static/<f>``  → JS/CSS assets
* ``GET  /api/state``   → static handshake metadata (:meth:`MonitorEngine.describe`)
* ``GET  /api/stream``  → SSE live feed of poll/status snapshots
* ``POST /api/write``   → ``{"spec","value"}`` | ``{"raw":{"db","offset","bytes"}}``
* ``POST /api/control`` → ``{"action":"pause|resume|reconnect|write_mode", ...}``
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import click

from .config import S7MonitorConfig
from .engine import MonitorEngine, WriteBlockedError, WriteMode
from .errors import dump_errors, log_error
from .logging import DataLogger, SessionMetadata

WEBUI_DIR = Path(__file__).parent / "webui"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".map": "application/json",
}
_SAFE_NAME = re.compile(r"^[\w.-]+$")
_SENTINEL = object()


class Broadcaster:
    """Fans messages out to all subscribed SSE clients.

    Each subscriber gets its own bounded queue; if a slow client's queue fills
    up the oldest message is dropped rather than blocking the poller. The most
    recent message is cached and replayed to new subscribers for instant render.
    """

    def __init__(self, maxsize: int = 16):
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._last: str | None = None
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
            last = self._last
        if last is not None:
            q.put_nowait(last)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, message: str) -> None:
        with self._lock:
            self._last = message
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except queue.Empty:
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close(self) -> None:
        """Wake every subscriber so streaming handlers can exit."""
        with self._lock:
            targets = list(self._subscribers)
            self._subscribers.clear()
        for q in targets:
            try:
                q.put_nowait(_SENTINEL)
            except queue.Full:
                pass


class _Poller(threading.Thread):
    """Drives the engine on a fixed interval and broadcasts each snapshot."""

    def __init__(self, server: "S7WebServer"):
        super().__init__(name="busfactor-poller", daemon=True)
        self._server = server
        self._stop = threading.Event()

    def run(self) -> None:
        engine = self._server.engine
        self._server.publish(engine.status_snapshot().to_dict(), "status")
        while not self._stop.is_set():
            if engine.paused or not engine.connection.connected:
                self._server.publish(engine.status_snapshot().to_dict(), "status")
            else:
                self._server.publish(engine.poll().to_dict(), "poll")
            self._stop.wait(engine.poll_interval)

    def stop(self) -> None:
        self._stop.set()


class _Handler(BaseHTTPRequestHandler):
    server_version = "busfactor/web"

    # Quieter logging; the default handler logs every request to stderr.
    def log_message(self, format: str, *args) -> None:
        pass

    @property
    def _server(self) -> "S7WebServer":
        return cast("_Server", self.server).app

    # ----------------------------------------------------------------- routing
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_asset("index.html")
        elif path.startswith("/static/"):
            self._serve_asset(path[len("/static/"):])
        elif path == "/api/state":
            self._send_json(200, self._server.engine.describe())
        elif path == "/api/stream":
            self._serve_stream()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/write":
            self._handle_write()
        elif path == "/api/control":
            self._handle_control()
        else:
            self._send_json(404, {"error": "not found"})

    # -------------------------------------------------------------- assets/sse
    def _serve_asset(self, name: str) -> None:
        if not _SAFE_NAME.match(name):
            self._send_json(400, {"error": "bad asset name"})
            return
        file = (WEBUI_DIR / name).resolve()
        if WEBUI_DIR.resolve() not in file.parents or not file.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        q = self._server.broadcaster.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                item = q.get()
                if item is _SENTINEL:
                    break
                self.wfile.write(f"data: {item}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            self._server.broadcaster.unsubscribe(q)

    # ---------------------------------------------------------------- requests
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON body: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _handle_write(self) -> None:
        engine = self._server.engine
        try:
            body = self._read_json()
            if "raw" in body:
                raw = body["raw"]
                data = bytearray(int(b, 16) for b in str(raw["bytes"]).split())
                result = engine.write_raw(int(raw["db"]), int(raw["offset"]), data)
            else:
                spec = str(body["spec"])
                value = str(body["value"])
                result = engine.write_variable(spec, value)
        except WriteBlockedError as e:
            self._send_json(403, {"error": str(e)})
            return
        except (KeyError, ValueError, TypeError) as e:
            self._send_json(400, {"error": str(e)})
            return
        except Exception as e:  # connection / PLC errors
            log_error(f"Web write failed: {e}")
            self._send_json(502, {"error": str(e)})
            return
        # Push a fresh read so every client sees the new value at once.
        if not engine.paused and engine.connection.connected:
            self._server.publish(engine.poll().to_dict(), "poll")
        self._send_json(200, {"ok": True, "result": {
            "spec": result.spec,
            "description": result.description,
            "bytes_hex": result.bytes_hex,
            "target": result.target,
            "offset": result.offset,
        }})

    def _handle_control(self) -> None:
        engine = self._server.engine
        try:
            body = self._read_json()
            action = str(body.get("action", ""))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        if action == "pause":
            engine.paused = True
        elif action == "resume":
            engine.paused = False
        elif action == "reconnect":
            try:
                engine.reconnect()
            except Exception as e:
                log_error(f"Web reconnect failed: {e}")
                self._send_json(502, {"error": str(e)})
                return
        elif action == "write_mode":
            mode = body.get("mode")
            if mode is None:
                engine.cycle_write_mode()
            else:
                try:
                    engine.write_mode = WriteMode(str(mode).lower())
                except ValueError as e:
                    self._send_json(400, {"error": f"unknown write mode: {mode}"})
                    return
        elif action == "pulse":
            target = body.get("target")
            if not target:
                self._send_json(400, {"error": "pulse requires a 'target' field"})
                return
            try:
                engine.trigger_pulse(str(target))
            except KeyError as e:
                self._send_json(400, {"error": str(e)})
                return
        else:
            self._send_json(400, {"error": f"unknown action: {action!r}"})
            return

        status = {
            "paused": engine.paused,
            "write_mode": engine.write_mode.value,
            "connection_state": engine.connection.state.value,
        }
        # Broadcast the new status so all clients update their controls.
        self._server.publish(engine.status_snapshot().to_dict(), "status")
        self._send_json(200, {"ok": True, "status": status})

    # ------------------------------------------------------------------ helper
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries a typed back-reference to the app."""

    app: "S7WebServer"


class S7WebServer:
    """Owns the HTTP server, broadcaster and background poller."""

    def __init__(self, engine: MonitorEngine, host: str = "127.0.0.1", port: int = 8731):
        self.engine = engine
        self.broadcaster = Broadcaster()
        self._httpd = _Server((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.app = self
        self._poller = _Poller(self)

    @property
    def address(self) -> tuple[str, int]:
        return cast("tuple[str, int]", self._httpd.server_address)

    @property
    def url(self) -> str:
        host, port = self.address
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{port}/"

    def publish(self, snapshot: dict, kind: str) -> None:
        snapshot = dict(snapshot)
        snapshot["type"] = kind
        self.broadcaster.publish(json.dumps(snapshot))

    def start(self) -> None:
        self._poller.start()

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._poller.stop()
        self.broadcaster.close()
        self._httpd.shutdown()
        self._httpd.server_close()
        self.engine.close()


def _build_logger(runtime) -> DataLogger | None:
    if not runtime.log_file:
        return None
    metadata = SessionMetadata(
        started=datetime.now(timezone.utc).isoformat(),
        address=runtime.connection.config.display,
        variables=[v.spec for v in runtime.variables],
        poll_interval=runtime.poll_interval,
        format=runtime.log_format.value,
    )
    logger = DataLogger(runtime.log_file, runtime.log_format, metadata)
    logger.open()
    return logger


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("address", required=False, default=None)
@click.argument("variables", nargs=-1)
@click.option("-c", "--config", "config_file", default=None, type=click.Path(), help="YAML config file.")
@click.option("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1).")
@click.option("-P", "--http-port", "http_port", default=8731, type=int, help="HTTP port (default: 8731).")
@click.option("--open", "open_browser", is_flag=True, default=False, help="Open the dashboard in a browser.")
@click.option("-r", "--rack", default=None, type=int, help="Rack number (default: 0).")
@click.option("-s", "--slot", default=None, type=int, help="Slot number (default: 2).")
@click.option("-p", "--port", default=None, type=int, help="PLC TCP port (default: 102).")
@click.option("-t", "--timeout", default=None, type=int, help="Connection timeout in ms (default: 3000).")
@click.option("-i", "--interval", default=None, type=float, help="Poll interval in seconds (default: 1.0).")
@click.option("--db", "db_number", default=None, type=int, help="DB number for raw range mode.")
@click.option("--start", "db_start", default=None, type=int, help="Start offset for raw range mode.")
@click.option("--size", "db_size", default=None, type=int, help="Number of bytes for raw range mode.")
@click.option(
    "-w", "--write-mode", "write_mode",
    type=click.Choice(["disabled", "confirm", "allowed"], case_sensitive=False),
    default=None, help="Write permission mode (default: disabled).",
)
@click.option("-l", "--log-file", "log_file", default=None, type=click.Path(), help="Log data changes to file.")
@click.option(
    "--log-format", "log_format",
    type=click.Choice(["csv", "jsonl"], case_sensitive=False),
    default=None, help="Log file format (default: csv).",
)
def web_cli(
    address: str | None,
    variables: tuple[str, ...],
    config_file: str | None,
    host: str,
    http_port: int,
    open_browser: bool,
    rack: int | None,
    slot: int | None,
    port: int | None,
    timeout: int | None,
    interval: float | None,
    db_number: int | None,
    db_start: int | None,
    db_size: int | None,
    write_mode: str | None,
    log_file: str | None,
    log_format: str | None,
) -> None:
    """busfactor-web — Live industrial protocol monitor in your browser.

    Accepts the same ADDRESS / VARIABLES / connection options as the TUI, then
    serves an ultra-modern dashboard over HTTP with a live Server-Sent Events
    feed. Open the printed URL in Chrome to monitor and (optionally) write.
    """
    from .cli import RuntimeConfigError, load_merged_config, resolve_runtime

    cfg = load_merged_config(
        config_file, address=address, rack=rack, slot=slot, port=port,
        timeout=timeout, interval=interval, write_mode=write_mode,
        db_number=db_number, db_start=db_start, db_size=db_size,
        variables=variables, log_file=log_file, log_format=log_format,
    )
    try:
        runtime = resolve_runtime(cfg)
    except RuntimeConfigError as e:
        click.echo(f"Error: {e}", err=True)
        if "variable specs" in str(e):
            click.echo("Try: busfactor-web --help", err=True)
        sys.exit(1)

    logger = _build_logger(runtime)
    engine = MonitorEngine(
        connection=runtime.connection,
        variables=runtime.variables,
        read_groups=runtime.read_groups,
        poll_interval=runtime.poll_interval,
        write_mode=runtime.write_mode,
        logger=logger,
        rules_engine=runtime.rules_engine,
    )

    try:
        engine.connect()
        click.echo(f"Connected to {runtime.connection.config.display}")
    except Exception as e:
        log_error(f"Initial connection failed: {e}")
        click.echo(f"Warning: initial connection failed: {e}", err=True)
        click.echo("Starting anyway — use the Reconnect button in the UI.", err=True)

    server = S7WebServer(engine, host=host, port=http_port)
    server.start()
    click.echo(f"busfactor-web serving at {server.url}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nShutting down…")
    finally:
        server.shutdown()
    dump_errors()


if __name__ == "__main__":
    web_cli()
