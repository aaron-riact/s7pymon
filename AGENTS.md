# busfactor — Agent instructions

## Commit workflow

- **Small, focused commits with tests** — one concern per commit, no scope creep. If you need a change to a recent commit, dont mix it with other concerns, make a ---fixup commit instead
- **Pedantic about commit boundaries** — never mix unrelated changes in the same commit. Check `git status`/`git diff` before staging. Only stage intended files.
- **Both type checkers must pass** (`ty check .` + `pyright .`) before committing, on source + tests.
- **Plain language** — avoid technobabble, avoid vague adjectives, and colloquial verbs.  Use direct plain concrete language in commit messages.  Only use abstractions when it clarifies.
- **Smallest working change, then refactor** — Make the smallest change to enable the feature request, add tests and commit it.  Afterward consider the "best" approach, ask the user if necessary and refactor to well structured code

## Coding approach
- Performance is the ultimate user experience.  We strive to make this app do **nothing** most of the time.  And when it has to do work, we do it in the most efficient way
- The intent of the code must be clear, obvious and boring even to human readers
- Use well established guidelines from known good experienced development teams as, well, guidelines


## Type checking and testing

```bash
ty check .          # primary type checker (0 errors required)
pyright .           # also passes cleanly
pytest tests/ -q    # all unit tests (no PLC needed)
pytest tests/foo.py -x -v  # single file
```

Both `ty check .` and `pyright` must pass on source + tests before committing.

## Project setup

```bash
pip install -e .            # editable install
uv sync --group dev         # dev deps (pytest, ty)
```

No Makefile. Build system is `uv_build`.

## Entry points (from pyproject.toml)

| Command | Source |
|---------|--------|
| `busfactor` | `cli.py:cli` |
| `busfactor-web` | `web.py:web_cli` |
| `busfactor-demo` | `demo.py:demo_web_cli` |
| `busfactor-replay` | `replay.py:replay_cli` |

`resolve_runtime()` in `cli.py` is the shared wiring function — turns `S7MonitorConfig` into a `ResolvedRuntime` (connection, variables, read groups, rules). Used by both TUI and web.

## Architecture

- **`Connection`** (`protocols.py`): abstract base that owns the connection state machine (state, error, lock, `connect()`/`disconnect()`). A driver implements `_open()`, `_close()`, `read_source(source, offset, size)` and `write_source(source, offset, data)`. Drivers: `S7Connection` (`connection.py`), `EIPConnection` (`eip.py`), `ModbusConnection` (`modbus.py`), `DemoConnection` (`demo.py`). What a failed read means is the driver's call: S7 marks the connection ERROR, Modbus lets the next poll retry.
- **`DataSource`** (`protocols.py`): frozen dataclass with factory methods — `DataSource.s7_db(210)`, `DataSource.eip("Input")`, `DataSource.modbus("Holding")`, `DataSource.s7_area("EB")`. `str(ds)` gives the wire string.
- **`Variable`** (`variable.py`): abstract frozen dataclass (`type`, `offset`, `extra`, `label`, `byte_order`) holding everything that depends only on the data type: `byte_size`, `decode`, `encode`, `encode_bit`, `format_value`, `parse_input`. `S7Variable`, `EIPVariable` and `ModbusVariable` add their address fields and implement `spec`, `source` and `is_input`. `Variable.parse()` is the factory for every spec form. `encode_for_write()` is the one read-modify-write path for bits.
- **`ReadGroup`** (`engine.py`): `ReadGroup(source, start, size, label="")` — one byte range read in one request; `key` is `str(source)`, which is how a variable finds its buffer. `build_read_groups()` (`cli.py`) groups variables by source; `build_eip_read_groups(config)` (`eip.py`) always reads the whole Input and Output assemblies.
- **`MonitorEngine`** (`engine.py`): protocol-agnostic core — reads groups, decodes variables, detects changes, applies rules. No protocol knowledge. `WriteMode.next()` is the disabled → confirm → allowed cycle.
- **Rules** (`rules.py`): `FollowRule`, `ToggleRule`, `PulseRule` are frozen config; `RulesEngine` keeps a `_RuleState` per rule (parsed target, counters, a pending delayed follow write). Poll-cycle counters (not wall-clock time) except the follow delay. Pulse is manual-trigger only. A bad target spec fails when the config loads.
- **Field vars** (`field_vars.py`): register-map expansion for EIP assemblies and Modbus tables. Uses `base_register` + `register_width_bits` to compute absolute byte offsets.
- **Config → runtime** (`cli.py`): `S7MonitorConfig.merge_cli()` applies CLI overrides with `dataclasses.replace`, so a field without a CLI option cannot be dropped. `build_connection()` picks the driver and fills protocol defaults; an unknown protocol is a `RuntimeConfigError`. `monitor_options` is the shared click option list for `busfactor` and `busfactor-web`.
- **Debug output**: standard library `logging`, one logger per module; `-v` installs a stderr handler at DEBUG (`configure_debug_logging()`). `datalog.py` is the *data* log (CSV/JSONL of value changes), unrelated to `logging`.
- **`HexDumpDisplay`** (`app.py`): Textual Line API widget; per-line rebuild and region refresh. A byte is keyed by `(group label, absolute offset)`, because two groups can cover the same offsets.
- **Known duplication**: the TUI (`app.py`) still runs its own poll/decode/change-detection loop beside `MonitorEngine`; `docs/design/0002-tui-engine-consolidation.md` is the plan to move it onto the engine.

## Variable spec conventions

- **Byte-offset addressing**: `Word0` = bytes 0–1, `Word4` = bytes 4–5 (S7 convention).
- **EIP default byte order**: LITTLE endian. S7 default: BIG endian. Override with `.be`/`.le`/`.big`/`.little` suffix on spec type (e.g. `Int0.le`, `DWord4.be`). WARNING: `.b` → byte-order suffix, not hex digit.
- **`Chars` type**: raw null-padded ASCII (no S7 length prefix). Decoded with `rstrip(b"\x00")`.
- **Hex bit parsing**: hex letters → hex; else decimal (backward compatible).

## Testing

- `tests/fakes.py` provides `BaseFakeConnection(Connection)` (implements `_open`/`_close`; `state` is settable) with a `_buffer_key()` hook — override to key buffers by `str(source)` for EIP tests (see `test_rules.py`).
- TUI tests use Textual's `app.run_test()` with `asyncio.run()` (no `pytest-asyncio`). Set `poll_interval=3600` to suppress the background poller in headless tests.
- When writing `BaseFakeConnection` subclasses for non-S7 protocols, override `_buffer_key()` to return a protocol-appropriate `Hashable` key.
