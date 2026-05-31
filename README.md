# s7pymon — S7 PLC Monitor TUI

A modern terminal UI for live-monitoring and writing Siemens S7 PLC data.
Built with **Textual** + **python-snap7**.

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)

## Features

- **Live hex dump** + decoded variable table, refreshed at a configurable interval
- **Multi-area monitoring** — DB, EB (inputs), AB (outputs), MB (merkers), CT (counters), TM (timers)
- **Named variables** — label any address with `:name` syntax
- **Write with confirmation** — edit values, toggle bits, or use the command bar; all writes require explicit confirmation
- **Keyboard-driven** — no mouse needed
- **Web dashboard** — an ultra-modern browser UI streaming live data over Server-Sent Events (`s7pymon-web`), with zero extra dependencies
- **Built-in demo** — launch the full browser dashboard with synthetic live PLC data using `s7pymon-demo`

## Requirements

```
python-snap7 >= 2.0
textual >= 3.0
rich >= 13.0
click >= 8.0
```

## Installation

### Editable install (development)

```bash
pip install -e .
```

### Without installing the package

```bash
python -m s7pymon.cli --help

# or add src/ to PYTHONPATH:
PYTHONPATH=src:$PYTHONPATH python -c "from s7pymon.cli import main; main()" --help
```

## Usage

```
s7pymon [OPTIONS] ADDRESS [VARIABLES...]
```

### Quick start

```bash
# Monitor a raw DB range (auto-creates Byte variables)
s7pymon 192.168.1.100 --db 210 --start 0 --size 18

# Monitor specific variables
s7pymon 192.168.1.100 DB210.Byte0 DB210.Int4 DB210.Real8

# Named variables
s7pymon 192.168.1.100 DB210.Byte0:heartbeat DB210.Byte1:status DB210.Bit1.0:e_stop

# Monitor DB + process inputs together
s7pymon 192.168.1.100 DB210.Byte0 EB.Byte0 EB.Byte1

# Process outputs and merker flags
s7pymon 192.168.1.100 AB.Byte0:output0 MB.Byte0:flag0

# Custom connection settings
s7pymon 192.168.1.100 --rack 0 --slot 2 --port 1102 --interval 0.25 DB210.Byte0
```

### Variable spec format

```
<Area>.<Type><Offset>[.<Extra>][:Label]
```

| Area | Description | Example |
|------|-------------|---------|
| `DB<n>` | Data Block | `DB210.Byte0` |
| `EB` | Process Image Input | `EB.Byte0` |
| `AB` | Process Image Output | `AB.Word2` |
| `MB` | Merkers / Flags | `MB.Bit0.3` |
| `CT` | Counters | `CT.Word0` |
| `TM` | Timers | `TM.Word0` |

| Type | Size | Notes |
|------|------|-------|
| `Byte` | 1 byte | Unsigned 0–255 |
| `Int` | 2 bytes | Signed 16-bit |
| `DInt` | 4 bytes | Signed 32-bit |
| `Word` | 2 bytes | Unsigned 16-bit |
| `DWord` | 4 bytes | Unsigned 32-bit |
| `Real` | 4 bytes | 32-bit float |
| `Bit` | 1 byte | Requires bit number: `Bit0.3` = byte 0, bit 3 |
| `String` | N+2 bytes | Requires max length: `String0.32` |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-r`, `--rack` | `0` | Rack number |
| `-s`, `--slot` | `2` | Slot number |
| `-p`, `--port` | `102` | TCP port |
| `-t`, `--timeout` | `3000` | Connection timeout (ms) |
| `-i`, `--interval` | `1.0` | Poll interval (seconds) |
| `--db` | — | DB number for raw range mode |
| `--start` | `0` | Start offset for raw range mode |
| `--size` | — | Byte count for raw range mode |

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `e` | Edit selected variable |
| `Space` | Toggle selected bit variable |
| `:` | Open command bar |
| `r` | Force refresh |
| `p` | Pause / resume polling |
| `c` | Reconnect to PLC |
| `q` | Quit |

### Command bar

Press `:` to open the command bar. Supported commands:

```
write <var> <value>   — Write a value (e.g. write DB210.Byte0 42)
set <var> <value>     — Alias for write
read                  — Force a read cycle
```

All write operations pop up a confirmation dialog showing the exact bytes
that will be written. Press **Y** to confirm or **N** / **Escape** to cancel.

## Web interface

In addition to the terminal UI, `s7pymon` ships a live browser dashboard with
an ultra-modern neon/glassmorphism theme. It reuses the same monitoring core
(connection, decode, change-detection, logging, write modes) as the TUI and
adds **no new Python dependencies** — the server is built on the standard
library `http.server` and pushes live telemetry over **Server-Sent Events**.

```bash
# Same arguments as the TUI, plus web options
s7pymon-web 192.168.1.100 --db 210 --start 0 --size 18 --open
s7pymon-web 192.168.1.100 DB210.Byte0:heartbeat DB210.Int4:temp --http-port 8730
```

### Quick browser demo

If you want to show the UI without a PLC, start the built-in demo:

```bash
# installed package
s7pymon-demo --open

# or from a checkout
uv run s7pymon-demo --open
```

That starts the same dashboard at `http://127.0.0.1:8731/`, but feeds it with
synthetic `DB210` values (`heartbeat`, `temperature`, `pressure`, `e_stop`,
`running`, `cycles`). The data changes continuously, writes are safe, and the
default write mode is **allowed** so the demo is immediately interactive.

Useful demo options:

| Option | Description |
|--------|-------------|
| `--open` | Open the dashboard in your browser automatically |
| `-P`, `--http-port PORT` | Serve the demo on another HTTP port |
| `-i`, `--interval SECONDS` | Slow down or speed up the fake PLC |
| `-w`, `--write-mode MODE` | Override the demo write mode (`disabled`, `confirm`, `allowed`) |
| `--seed N` | Make the synthetic values reproducible |

### Web options

| Option | Description |
|--------|-------------|
| `--host HOST` | Interface to bind the HTTP server to (default `127.0.0.1`) |
| `-P`, `--http-port PORT` | HTTP port to serve on (default `8731`) |
| `--open` | Open the dashboard in your default browser on start |

All the standard connection/variable options (`--rack`, `--slot`, `--port`,
`--interval`, `--db`, `--start`, `--size`, config files, `:label` syntax) work
exactly as they do for the TUI.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | The dashboard (HTML/CSS/JS) |
| `GET` | `/api/state` | One-shot snapshot + variable/connection description |
| `GET` | `/api/stream` | Server-Sent Events live feed of poll/status snapshots |
| `POST` | `/api/write` | Write a variable (`{spec, value}` or raw bytes) |
| `POST` | `/api/control` | `pause` / `resume` / `reconnect` / `write_mode` |

Writes honour the active **write mode**: when writes are disabled the server
returns `403`; the dashboard's native `<dialog>` acts as the confirmation step.

### Browser shortcuts

| Key | Action |
|-----|--------|
| `Space` | Pause / resume polling |
| `C` | Reconnect to the PLC |
| `W` | Cycle write mode (disabled → confirm → allowed) |
| `/` | Focus the command bar |

The dashboard uses modern web-platform features (ES modules, custom elements,
native `<dialog>`, View Transitions, and CSS `@layer`/nesting/`oklch()`/
container queries) and targets recent Chromium-based browsers.

## Testing

```bash
python -m pytest test/test_s7_monitor_*.py -v
```
