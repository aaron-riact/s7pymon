# s7pymon — Live industrial protocol monitor (S7 + EtherNet/IP)

A modern terminal UI and web dashboard for live-monitoring and writing
industrial controller data. Supports **Siemens S7** (via python-snap7) and
**EtherNet/IP** (via python-ethernetip) protocols.

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)

## Features

- **Live hex dump** + decoded variable table, refreshed at a configurable interval
- **Multi-area monitoring** — DB, EB (inputs), AB (outputs), MB (merkers), CT (counters), TM (timers), EIP assemblies
- **Named variables** — label any address with `:name` syntax
- **Write with confirmation** — edit values, toggle bits, or use the command bar; all writes require explicit confirmation
- **Output rules** — Follow (copy input to output), Toggle (heartbeat), Pulse (one-shot) for automatic assembly management
- **Keyboard-driven** — no mouse needed
- **Web dashboard** — an ultra-modern browser UI streaming live data over Server-Sent Events (`s7pymon-web`), with zero extra dependencies
- **Built-in demo** — launch the full browser dashboard with synthetic live PLC data using `s7pymon-demo`
- **Cross-protocol** — monitor S7 DBs and EIP assemblies side-by-side; Follow rules can copy between protocols

## Requirements

```
# S7 mode
python-snap7 >= 2.0

# EIP mode (optional)
ethernetip >= 1.1.2

# Both modes
textual >= 3.0
rich >= 13.0
click >= 8.0
pyyaml >= 6.0
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

### EtherNet/IP variable spec

```
EIP.<Assembly>.<Type><Offset>[.<Extra>][:Label]
```

Where `<Assembly>` is `Input`, `Output`, or `Config`.

| Example | Description |
|---------|-------------|
| `EIP.Input.Byte0:heartbeat` | Input assembly byte 0, labelled "heartbeat" |
| `EIP.Input.Int4:temperature` | Input assembly, signed 16-bit int at offset 4 |
| `EIP.Output.DWord8:setpoint` | Output assembly, unsigned 32-bit at offset 8 |
| `EIP.Input.Bit0.3:limit_switch` | Input assembly, bit 3 of byte 0 |
| `EIP.Output.Bit0.0:watchdog` | Output assembly, bit 0 of byte 0 |

### Config files

Connection settings and variables can be stored in a YAML config file and
shared between the TUI and web dashboard.

**S7 example** (`monitor.yaml`):

```yaml
address: 192.168.1.100
rack: 0
slot: 2
port: 102
interval: 0.5
write_mode: confirm
variables:
  - DB210.Byte0:heartbeat
  - DB210.Byte1:status
  - DB210.Bit1.0:e_stop
  - EB.Byte0:input0
```

**EIP example** (`eip-monitor.yaml`):

```yaml
protocol: eip
address: 192.168.1.200
interval: 0.05               # 50 ms for fast I/O
output_assembly: 100
input_assembly: 101
input_size: 32
output_size: 32
rpi_ms: 50
variables:
  - EIP.Input.Byte0:heartbeat
  - EIP.Input.Int4:temperature
  - EIP.Output.DWord8:setpoint
```

Usage:

```bash
s7pymon -c monitor.yaml
s7pymon-web -c eip-monitor.yaml --open
```

### Output rules

Output rules run automatically on every poll cycle. They are configured in the
YAML config file under a `rules:` key that maps target variables to rule
definitions.

| Rule | Behaviour | Example |
|------|-----------|---------|
| **follow** | Copy a source variable's value to the target each cycle | `target: { follow: source }` |
| **toggle** | Alternate a bit every N cycles (heartbeat/watchdog) | `target: { toggle: 2 }` |
| **pulse** | Set a bit high for N cycles when triggered | `target: { pulse: 5 }` |

Cross-protocol follow is supported — source and target can be on different
assemblies or protocols (e.g. S7 DB → EIP output).

**Example** with rules:

```yaml
protocol: eip
address: 192.168.1.200
variables:
  - EIP.Input.Byte0:heartbeat
  - EIP.Output.Byte0:output0
  - EIP.Output.Bit0.0:watchdog
rules:
  # Copy input byte 0 to output byte 0 every cycle
  EIP.Output.Byte0:
    follow: EIP.Input.Byte0
  # Toggle bit 0 of output byte 0 every 2 cycles (heartbeat)
  EIP.Output.Bit0.0:
    toggle: 2
```

Manual pulse is available via the command bar:

```
pulse EIP.Output.Bit0.0   # trigger a pulse rule for 1 cycle
pulse EIP.Output.Bit0.0 5 # trigger a pulse rule for 5 cycles
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-c`, `--config` | — | YAML config file path |
| `-r`, `--rack` | `0` | Rack number (S7) |
| `-s`, `--slot` | `2` | Slot number (S7) |
| `-p`, `--port` | `102` (S7) / `44818` (EIP) | TCP port |
| `-t`, `--timeout` | `3000` | Connection timeout (ms) |
| `-i`, `--interval` | `1.0` | Poll interval (seconds) |
| `--db` | — | DB number for raw range mode (S7) |
| `--start` | `0` | Start offset for raw range mode (S7) |
| `--size` | — | Byte count for raw range mode (S7) |

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
write <var> <value>      — Write a value (e.g. write DB210.Byte0 42)
set <var> <value>        — Alias for write
read                     — Force a read cycle
pulse <target> [cycles]  — Trigger a pulse rule (default 1 cycle)
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
| `POST` | `/api/control` | `pause` / `resume` / `reconnect` / `write_mode` / `pulse` |

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
