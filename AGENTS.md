# s7pymon — Agent instructions

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
| `s7pymon` | `cli.py:cli` |
| `s7pymon-web` | `web.py:web_cli` |
| `s7pymon-demo` | `demo.py:demo_web_cli` |
| `s7pymon-replay` | `replay.py:replay_cli` |

`resolve_runtime()` in `cli.py` is the shared wiring function — turns `S7MonitorConfig` into a `ResolvedRuntime` (connection, variables, read groups, rules). Used by both TUI and web.

## Architecture

- **`Connection` ABC** (`protocols.py`): `read_source(source, offset, size)`, `write_source(source, offset, data)`. Implemented by `S7Connection` (`connection.py`) and `EIPConnection` (`eip.py`).
- **`DataSource`** (`protocols.py`): frozen dataclass with factory methods — `DataSource.s7_db(210)`, `DataSource.eip("Input")`, `DataSource.s7_area("EB")`. `str(ds)` gives the wire string.
- **Variables**: `S7Variable` and `EIPVariable` are separate `@dataclass` (not subclassed). `S7Variable.parse()` handles both S7 and EIP spec strings and returns the correct type.
- **`MonitorEngine`** (`engine.py`): protocol-agnostic core — reads groups, decodes variables, detects changes. No protocol knowledge.
- **`ReadGroup._source`** (underscore-prefixed): conflicts with the `source` property, hence the underscore.
- **Rules** (`rules.py`): `FollowRule`, `ToggleRule`, `PulseRule`. Poll-cycle counters (not wall-clock time). Pulse is manual-trigger only.
- **Field vars** (`field_vars.py`): register-map expansion for EIP assemblies. Uses `base_register` + `register_width_bits` to compute absolute byte offsets.
- **`build_eip_read_groups()`** (`eip.py`): creates Input + Output read groups from config (not from variable coverage).

## Variable spec conventions

- **Byte-offset addressing**: `Word0` = bytes 0–1, `Word4` = bytes 4–5 (S7 convention).
- **EIP default byte order**: LITTLE endian. S7 default: BIG endian. Override with `.be`/`.le`/`.big`/`.little` suffix on spec type (e.g. `Int0.le`, `DWord4.be`). WARNING: `.b` → byte-order suffix, not hex digit.
- **`Chars` type**: raw null-padded ASCII (no S7 length prefix). Decoded with `rstrip(b"\x00")`.
- **Hex bit parsing**: hex letters → hex; else decimal (backward compatible).

## Testing

- `tests/fakes.py` provides `BaseFakeConnection(Connection)` with a `_buffer_key()` hook — override to key buffers by `str(source)` for EIP tests (see `test_rules.py`).
- TUI tests use Textual's `app.run_test()` with `asyncio.run()` (no `pytest-asyncio`). Set `poll_interval=3600` to suppress the background poller in headless tests.
- When writing `BaseFakeConnection` subclasses for non-S7 protocols, override `_buffer_key()` to return a protocol-appropriate `Hashable` key.
