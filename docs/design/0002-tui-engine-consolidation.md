# Consolidate TUI (`app.py`) onto `MonitorEngine` (`engine.py`)

## Status

Proposed — not yet implemented.

## Summary

The TUI (`S7MonitorApp` in `app.py`) and the headless `MonitorEngine`
(`engine.py`) currently contain **two parallel implementations** of the same
core logic — reading PLC data, decoding variables, detecting changes, applying
output rules, and writing values. The engine was extracted *from* the TUI early
in the project to power the web dashboard, but the TUI was never retrofitted to
use it.

This document proposes consolidating the TUI onto the engine, eliminating
~180 lines of duplicated code and making both frontends share a single source
of truth for polling, decoding, change detection, data logging, and rules
application.

The web dashboard (`web.py`) already uses the engine — it is not affected.

---

## 1. Terminology

These terms are used consistently throughout the codebase:

| Term | Definition | Example |
|------|-----------|---------|
| **spec** | A human-readable variable identifier string | `"DB210.Byte0"`, `"EIP.Input.Byte0"` |
| **parse** | Convert a spec string into a typed variable object | `S7Variable.parse("DB210.Byte0")` → `S7Variable(db=210, type=BYTE, offset=0)` |
| **decode** | Convert raw bytes into a Python value using a variable's type | `var.decode(b"\x2A")` → `42` (for Byte) |
| **encode** | Convert a Python value into raw bytes | `var.encode(42)` → `bytearray(b"\x2A")` |
| **format** | Convert a Python value into a display string | `var.format_value(3.1415)` → `"3.1415"` |
| **parse input** | Convert a user-entered text string into a Python value | `var.parse_input("0xFF")` → `255` |
| **encode_bit** | Set or clear a specific bit in a byte (read-modify-write) | `var.encode_bit(0x08, False)` → `bytearray(b"\x00")` |
| **read_group** | A group of variables read together in one `read_source` call | `ReadGroup(source=DataSource("DB210"), start=0, size=16)` |
| **buffer** | Raw bytearray from `read_source`, plus the start offset | `(bytearray(b"..."), 0)` |
| **Snapshot** | Structured result of one poll cycle — readings, groups, state | `Snapshot(timestamp=..., readings=[...], groups=[...])` |
| **poll** | One complete read-decode-detect-rules cycle | `engine.poll()` → `Snapshot` |

---

## 2. Current Execution Models

### 2a. Web Dashboard (`web.py` + `engine.py`)

```
Thread: _Poller (daemon thread)
────────────────────────────────────────────────
loop every poll_interval:
  1. engine.poll()
     a. read_source() for each group    ── blocks on socket
     b. decode each variable            ── no I/O
     c. apply rules (may write_source)  ── blocks on socket
     d. detect changes, log to file
     e. return Snapshot
  2. broadcaster.publish(snapshot.to_dict())

Thread: HTTP handler (per-request thread)
────────────────────────────────────────────────
POST /api/write:
  engine.write_variable(spec, value)
    a. parse spec → S7Variable/EIPVariable
    b. parse_input(text) → Python value
    c. encode(value) → bytearray
    d. (for bits) read_source → modify  ── blocks on socket
    e. write_source(encoded)            ── blocks on socket
    f. return WriteResult
```

Data flows one way: engine → broadcaster → SSE subscribers.
Control (writes, pause, reconnect) comes via HTTP POST handlers.

**No locking needed** — `_Poller` is the only writer to engine state. HTTP
handlers call engine methods on separate threads but share only the `Connection`
(serialised by single-socket + GIL). `Broadcaster` has its own `threading.Lock`
for subscriber management.

### 2b. TUI (`app.py`)

```
Event loop thread (Textual's asyncio event loop)
────────────────────────────────────────────────
set_interval(poll_interval, _poll_tick)
_poll_tick():
  _do_read()  ── @work(thread=True) spawns worker thread

Worker thread (from @work decorator)
────────────────────────────────────────────────
_do_read():
  1. read_source() for each group       ── blocks on socket
  2. _apply_rules(results)              ── may write_source (blocks)
  3. call_from_thread(_on_data_received, results)

Event loop thread (call_from_thread)
────────────────────────────────────────────────
_on_data_received(results):
  1. build hex dump from raw buffers
  2. for each variable:
     a. extract_value(var, data)        ── struct.unpack, no I/O
     b. format_value(value)
     c. detect change vs _previous_values
     d. log to file
     e. update widget (bold yellow on change)
  3. update connection status widget
```

**Write flow** (multi-step async chain):

```
action_edit_variable()            [main thread]
  → open EditVariableScreen

_on_edit_result(text)             [main thread]
  → _@work_ prepare_variable_write(var, text)

_@work_ prepare_variable_write()  [worker thread]
  var.parse_input(text) → Python value
  var.encode(value) → bytearray
  (for bits) read_source → encode_bit
  create PendingWrite(description, source, offset, data)
  call_from_thread(_confirm_and_write, pending)

_confirm_and_write()              [main thread]
  if ALLOWED: @work _execute_write(pending)
  if CONFIRM: push_screen(ConfirmWriteScreen)

_@work_ execute_write()           [worker thread]
  connection.write_source()
  call_from_thread(log.write, success)
  call_from_thread(_do_read)       ── re-read to show new value
```

**No explicit locking** — `@work` serialises calls. `_current_values` and
`_previous_values` are only accessed on the main thread (inside
`_on_data_received`). The worker thread only touches `_connection`.

---

## 3. Proposed Execution Model

The core change: the TUI calls `engine.poll()` in its worker thread instead
of implementing its own read/decode/detect cycle.

```
Thread: Textual event loop (main thread)
────────────────────────────────────────────────
set_interval(poll_interval, _poll_tick)
_poll_tick():
  if not paused: _do_poll()  ── @work(thread=True)

Worker thread (@work)
────────────────────────────────────────────────
_do_poll():
  1. snapshot = engine.poll()
     a. read_source() for each group   ── blocks on socket
     b. decode all variables           ── no I/O
     c. apply rules (may write_source) ── blocks on socket
     d. detect changes, log to file
     e. return Snapshot
  2. call_from_thread(_on_snapshot, snapshot)

Main thread (call_from_thread)
────────────────────────────────────────────────
_on_snapshot(snapshot: Snapshot):
  1. build hex dump from snapshot.groups
  2. for each reading in snapshot.readings:
     a. apply change styling (bold yellow if reading.changed)
     b. update table cell
  3. update connection status from snapshot.connection_state
```

**Key difference:** The engine now owns ALL decode, change detection, and
logging. The TUI's `_on_snapshot` receives an already-processed `Snapshot` and
does nothing but widget updates.

### Write flow (proposed)

```
Main thread:
  engine.prepare_encode(spec, text)
    → Preview(parsed_value, encoded, description, source, offset)

  if ALLOWED: @work _do_write(preview)
  if CONFIRM: ConfirmWriteScreen → @work _do_write(preview)

Worker thread:
  engine.write_encoded(source, offset, data)
    → connection.write_source()
  call_from_thread(log.write)
  call_from_thread(_do_poll)   ── re-read
```

The TUI keeps its own encode step to show the preview in the confirmation
dialog. The actual transport delegates to `engine.write_encoded()`. The
duplicate encode is ~5 lines and nanoseconds — not worth optimising.

---

## 4. Key Methods

### New on `MonitorEngine`

```python
def poll(self) -> Snapshot:
    """Read all groups, decode variables, detect changes, apply rules.
    Returns a Snapshot with all readings pre-computed.
    Thread-safe: no mutable state shared between calls.
    Already exists — unchanged, but TUI now starts using it."""

def write_encoded(self, source: DataSource, offset: int, data: bytearray) -> WriteResult:
    """Write pre-encoded bytes to a source.
    Skips the encode step — caller (TUI) already encoded for preview.
    Generalises existing write_raw() to accept any DataSource."""

def prepare_encode(self, spec: str, value_text: str) -> Preview:
    """Parse spec, parse_input, encode — return preview info without writing.
    Encapsulates the encode chain the TUI currently does inline."""
```

### `Preview` dataclass (new, shared)

```python
@dataclass
class Preview:
    spec: str
    display_name: str
    parsed_value: Union[int, float, bool, str]
    encoded: bytearray
    description: str  # e.g. "Set heartbeat = 42"
    source: DataSource
    offset: int
```

### What changes in `S7MonitorApp`

| Current | Proposed | Notes |
|---------|----------|-------|
| `_do_read()` ~70 lines | `_do_poll()` ~15 lines | `engine.poll()` then `call_from_thread` |
| `_apply_rules()` ~15 lines | **Deleted** | Engine does it in `poll()` |
| `_on_data_received()` ~70 lines | `_on_snapshot()` ~70 lines | Widget updates only, no decode |
| `_current_data/values/previous` | **Deleted** | Engine owns these |
| `_prepare_variable_write()` ~25 lines | `_prepare_write()` ~15 lines | Calls `engine.prepare_encode()` |
| `_execute_write()` ~10 lines | `_do_write()` ~5 lines | Calls `engine.write_encoded()` |
| `_execute_command()` encode logic | Delegated to engine | `prepare_encode` + `write_encoded` |
| `action_cycle_write_mode()` | `engine.cycle_write_mode()` | Delegate logic |
| `action_reconnect()` | `engine.reconnect()` | Delegate logic |
| `_update_connection_state()` | From `snapshot.connection_state` | No separate call |

---

## 5. Threading & Synchronization

### Before

```
TUI today:
  Main thread: reads/writes _current_values, _previous_values, widgets
  Worker thread: reads/writes connection, PLC

  Shared state with implicit ordering (no locks):
    - _connection (GIL-serialised socket)
    - _rules_engine (mutated in worker only)
    - _current_values (written on main thread, read by worker for rules)
```

### After

```
Proposed TUI:
  Main thread: reads/writes snapshot, widgets ONLY
  Worker thread: engine.poll() — all I/O, decode, rules in one call

  Only shared object: Connection (socket — GIL-serialised)
  Everything else is created fresh per poll call inside engine.poll().
  engine._current_values and engine._previous_values are written inside
  poll() and never read concurrently — poll() is synchronous and returns
  before the next call.

  Rules engine state: owned by engine._rules_engine, mutated only
  inside engine.poll() → rules_engine.apply(). No concurrent access.

  Result: NO LOCKS NEEDED beyond what Python's GIL provides.
```

**Critical invariant:** `engine.poll()` is never called concurrently. The
`@work` decorator serialises calls (Textual queues them). Even if the poll
interval is shorter than a slow read, the next `_poll_tick` just queues
another `@work` which runs after the previous one finishes.

### Web dashboard after consolidation

```
Same as today:
  _Poller thread: engine.poll() in loop
  HTTP handler threads: engine.write_variable() / prepare_encode()

  Concurrent calls to engine from _Poller and HTTP handlers:
    - poll() reads connection, writes engine._current_values
    - write_variable() reads connection, writes connection
    - prepare_encode() does NO I/O

  Risks:
    - poll() and write_variable() could interleave on connection
    - Mitigation: GIL + single blocking socket serialise naturally
    - If needed later: threading.Lock on engine._lock
    - This is the SAME level of concurrency as today — no regression.
```

---

## 6. Data Flow Diagrams

### Poll cycle (both frontends, after consolidation)

```
                     engine.poll()
                    ┌──────────────┐
  read_groups ──────┤ read_source  ├── buffers dict
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  decode all   │── readings (decoded values)
                    │  variables    │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  apply rules  │── writes to PLC (side effect)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  detect       │── changed flags
                    │  changes      │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  build        │── Snapshot (readings + groups + state)
                    │  Snapshot     │
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     ┌──────────┐   ┌──────────┐   ┌──────────┐
     │  TUI     │   │  Web     │   │  Demo    │
     │ widget   │   │ JSON     │   │  print   │
     │ updates  │   │ broadcast│   │  console │
     └──────────┘   └──────────┘   └──────────┘
```

### Write flow (TUI, after consolidation)

```
User presses 'e' on a variable
  │
  ▼
action_edit_variable()          [main thread]
  push_screen(EditVariableScreen)
  │
  ▼
_on_edit_result(text)           [main thread]
  engine.prepare_encode(spec, text)
    │
    ▼
  Preview {
    parsed_value = var.parse_input(text)
    encoded = var.encode(parsed_value)
    description = "Set heartbeat = 42"
  }
  │
  ├── write_mode == ALLOWED ──► _@work_do_write(preview)
  ├── write_mode == CONFIRM ──► ConfirmWriteScreen
  │                               │
  │                               ▼ Y pressed
  │                             _on_confirm_result(True)  [main thread]
  │                               _@work_do_write(preview)
  │
  ▼
_@work_do_write(preview)        [worker thread]
  engine.write_encoded(preview.source, preview.offset, preview.data)
    connection.write_source(...)  ── blocks on socket
  call_from_thread(log.write, success)
  call_from_thread(_do_poll)      ── re-read
```

---

## 7. Lines Removed from `app.py`

| Lines | Code | Removed because |
|-------|------|-----------------|
| 35–47 | `PendingWrite` dataclass | Replaced by `Preview` in engine |
| 354 | `self._current_data` | Snapshot carries all data |
| 355 | `self._current_values` | Engine owns change state |
| 356 | `self._previous_values` | Engine owns change state |
| 465–483 | `_do_read()` | `_do_poll()` calls `engine.poll()` |
| 485–499 | `_apply_rules()` | Engine applies rules in `poll()` |
| 506–508 | `_group_key_for_var()` | Not needed |
| 510–577 | `_on_data_received()` | `_on_snapshot()` iterates snapshot.readings |
| 617–641 | `_prepare_variable_write()` | `engine.prepare_encode()` |
| 666–676 | `_execute_write()` | `engine.write_encoded()` |
| 707–775 | `_execute_command()` encode logic | Delegated to `engine.prepare_encode()` |

**Net removal: ~180 lines** of duplicated logic from `app.py`.

---

## 8. Lines Kept in `app.py` (unchanged)

| Lines | Code | Purpose |
|-------|------|---------|
| 50–102 | `ConnectionStatus` widget | Render connection state |
| 93–102 | `HexDumpDisplay` widget | Render hex dump |
| 105–285 | Modal screens | Edit, command bar, confirmation dialogs |
| 321–330 | `BINDINGS` | Keyboard shortcuts |
| 380–433 | `on_mount()` | Widget setup, initial connect |
| 435–457 | `_connect_and_poll()` + `_start_polling()` | Connection lifecycle + timer |
| 459–463 | `_poll_tick()` | Timer callback |
| 579–581 | `_update_connection_state()` | Widget state sync |
| 583–589 | `_check_write_allowed()` | Write guard |
| 591–604 | `action_edit_variable()` | Open edit dialog |
| 606–615 | `_on_edit_result()` | Handle edit dialog result |
| 643–654 | `_confirm_and_write()` | Route write through confirmation |
| 656–664 | `_on_confirm_result()` | Handle confirmation result |
| 678–693 | `action_toggle_bit()` | Toggle bit logic |
| 695–699 | `action_command_bar()` | Open command bar |
| 701–705 | `_on_command_result()` | Handle command bar result |
| 759–769 | `_execute_command()` read command | Raw byte read (no engine equivalent) |
| 777–779 | `action_force_refresh()` | Force re-read |
| 781–797 | `action_cycle_write_mode()` | Cycle + update widgets |
| 799–806 | `action_toggle_pause()` | Pause/resume |
| 808–814 | `action_reconnect()` | Reconnect |
| 816–819 | `on_unmount()` | Cleanup |

---

## 9. Implementation Plan (commit order)

| # | Commit | Files | Tests |
|---|--------|-------|-------|
| 1 | Add `Preview` dataclass + `prepare_encode()` to engine | `engine.py` | `test_engine.py`: verify preview has correct fields |
| 2 | Generalise `write_raw()` to `write_encoded()` for any DataSource | `engine.py` | `test_engine.py`: verify writes correct source |
| 3 | Add `_on_snapshot()` widget-update method to TUI | `app.py` | Manual — run TUI, verify rendering |
| 4 | Replace `_do_read()` with `_do_poll()` using `engine.poll()` | `app.py` | Run full test suite |
| 5 | Route TUI writes through `engine.prepare_encode()` + `write_encoded()` | `app.py` | Existing write tests |
| 6 | Delete stale state fields (`_current_data`, values, previous) | `app.py` | Verify no AttributeError |
| 7 | Delete `_apply_rules()`, `_group_key_for_var()`, `PendingWrite` | `app.py` | Full test suite |
| 8 | Delegate `action_cycle_write_mode()`, `action_reconnect()` to engine | `app.py` | Existing tests |

---

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| TUI widget rendering breaks because `_on_snapshot` data shape differs | Visual glitches, missing data | Add `_on_snapshot` (commit 3) before replacing `_do_read` (commit 4). Run TUI manually between each commit. |
| Engine's `_read_variable` returns `"—"` for missing groups; TUI currently skips silently | Table shows `"—"` | Accept the new behaviour — it's more correct. Or change engine to match TUI expectation in one line. |
| Engine's change detection marks first poll as unchanged; TUI does the same | No difference | Verified — both check `prev is not None`. |
| Engine's `poll()` blocks the worker thread for full duration | Same as today's `_do_read()` | No regression. |
| Engine's `Snapshot.readings` has no styling info | TUI needs to re-derive bold yellow | `_on_snapshot` checks `reading.changed` boolean and applies style. |

---

## 11. Open Questions

- **Should `prepare_encode()` live on `MonitorEngine` or be a standalone helper?**
  Putting it on the engine means callers always have a single import for
  encode-related operations. Standing alone means the TUI doesn't need the
  engine just to show a preview. Recommendation: on the engine, for consistency.

- **Thread safety of `prepare_encode()` when called from main thread while
  `poll()` runs on the worker thread.** `prepare_encode()` does no I/O —
  pure CPU (struct.pack, bit masking). Safe under GIL. The `Connection` is
  only touched inside `write_encoded()`, which runs on the worker thread.

- **Do we keep `PendingWrite` as a TUI-internal type or replace it entirely
  with `Preview`?** Recommendation: remove `PendingWrite` and use `Preview`
  everywhere. They have the same fields; `Preview` adds `parsed_value` and
  `display_name` that the TUI's confirmation dialog can use.
