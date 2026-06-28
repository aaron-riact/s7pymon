# Ethernet/IP Scanner Mode + Output Rules

## Status

Implemented (June 2026) — see §8 for reality vs. plan.

## Summary

Add Ethernet/IP passive (scanner) mode to busfactor, enabling it to act as an
EtherNet/IP scanner that connects to an adapter (PLC, drive, I/O block), reads
its input assemblies, and writes its output assemblies.  Along the way we
introduce an **output-rules** engine that automates common patterns (follow,
toggle, pulse) without needing a real PLC program.

The architecture refactoring is deliberately minimal — the existing pipeline
(read → decode → display) stays untouched; we add a parallel (apply rules →
send outputs) step behind the same connection abstraction.

---

## 1. Protocol Connection Abstraction

### Current state

`S7Connection` (connection.py) is a concrete class with no abstract base.
`MonitorEngine` (engine.py) takes an `S7Connection` directly.  `DemoConnection`
(demo.py) proves duck-typing works, but there is no interface to implement.

### Target state

A `Connection` ABC in a new `protocols.py` that both `S7Connection` and
`EIPConnection` implement:

```python
class Connection(ABC):
    protocol: ClassVar[str]  # "s7" | "eip"

    @property
    def state(self) -> ConnectionState: ...
    @property
    def connected(self) -> bool: ...
    @property
    def config(self) -> ConnectionConfig: ...

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    def read_groups(self, groups: list[ReadGroup]) -> dict[str, tuple[bytearray, int]]: ...
    def write_assembly(self, target: str, data: bytearray) -> None: ...
```

`write_assembly` is the key new method — EIP writes always send a full output
assembly image.  `read_groups` replaces per-call `area_read` for efficiency
(EIP can read input + config in one call or separate calls depending on the
library; the grouping is an implementation detail).

### ConnectionConfig

Made generic with a `protocol` discriminator:

```python
@dataclass
class ConnectionConfig:
    protocol: str = "s7"
    address: str = ""
    tcp_port: int = 102       # S7 default
    timeout_ms: int = 3000
    # EIP-specific
    eip_port: int = 44818
    input_assembly: int = 101
    output_assembly: int = 100
    config_assembly: int = 102
    rpi_ms: int = 50
```

---

## 2. Variable Spec — EIP Addressing

### Format

```
EIP.<Assembly>.<Type><Offset>[.<Extra>][:Label]
```

Where `<Assembly>` is one of `Input`, `Output`, `Config`, or a numeric assembly
instance number that maps to `Input`/`Output`/`Config` via the config mapping.

### Examples

```yaml
variables:
  - EIP.Input.Byte0:heartbeat       # assembly 101 (input), byte 0
  - EIP.Input.Int4:temperature       # assembly 101, signed int at offset 4
  - EIP.Output.DWord8:setpoint       # assembly 100 (output), dword at offset 8
  - EIP.Input.Bit0.3:limit_switch    # assembly 101, bit 3 of byte 0
  - EIP.Input.String100.32:message   # assembly 101, string at offset 100
  - EIP.Config.Byte0:config_val      # assembly 102 (configuration)
```

Numeric aliases also work when the config defines the mapping:

```
EIP.101.Byte0                       # explicitly assembly 101 byte 0
```

### Parsing

A new regex in `variable.py` alongside the existing S7 patterns:

```python
_EIP_VAR_PATTERN = re.compile(
    r"^EIP\.(Input|Output|Config|\d+)\.(Byte|Int|DInt|Word|DWord|Real|Bit|String)"
    r"(\d+)(?:\.(\d+))?$",
    re.IGNORECASE,
)
```

### Internal representation

A new `Variable` dataclass that replaces `S7Variable` as the universal type,
with `S7Variable` becoming an alias or subclass:

```python
@dataclass(frozen=True)
class Variable:
    protocol: str          # "s7" | "eip"
    area: str              # "DB" | "EB" | "EIP.Input" | "EIP.101" | …
    db: int                # 0 for non-DB (S7) / assembly number decode
    type: DataType         # Byte | Int | DInt | Word | DWord | Real | Bit | String
    offset: int
    extra: int | None      # bit number or string max length
    label: str | None
```

`S7Type` is renamed to `DataType` — the types are universal across industrial
protocols and don't need an S7 prefix.

---

## 3. Output Rules

### Semantics

Every write to an output assembly goes through a rule engine.  Rules are
evaluated **after** each read and **before** shipping the output assembly.
Rules have **exclusive ownership** of their target — if a rule governs
`Output.Bit2.0`, manual writes to that spec are blocked with a clear error.

### Rule types

| Type | Behaviour | Config |
|------|-----------|--------|
| **follow** | Copy a source variable's value to the target. | `source` spec, `invert`, `scale` |
| **toggle** | Invert the target value at a regular interval. | `interval_ms` |
| **pulse** | When a source bit transitions, set the target high for N ms. | `source` spec, `duration_ms`, `edge` |

### Config format

```yaml
output_rules:
  - target: EIP.Output.Bit2.0
    follow: EIP.Input.Bit3.1
    invert: true

  - target: EIP.Output.Bit2.1
    toggle_ms: 500

  - target: EIP.Output.Bit2.0
    pulse: EIP.Input.Bit3.2
    duration_ms: 200
    edge: rising
```

### Internal representation

```python
@dataclass
class OutputRule:
    target_spec: str   # e.g. "EIP.Output.Bit2.0"
    type: str          # "follow" | "toggle" | "pulse"
    source_spec: str | None = None
    invert: bool = False
    toggle_ms: int = 0
    duration_ms: int = 0
    edge: str = "rising"   # "rising" | "falling" | "both"
```

### Integration into the poll cycle

The engine's poll becomes:

```
1. read_groups()                    → raw assembly buffers
2. decode(readings_from_assemblies) → VariableReading[]
3. apply_output_rules(buffers)      → mutates output assembly buffer
4. write_assembly(output_buffer)    → sends output to adapter
5. return Snapshot(readings, ...)
```

`apply_output_rules` is a method on `MonitorEngine` that:
- Evaluates follow rules (read source value from buffer, transform, write target)
- Evaluates toggle rules (track last-toggle timestamp, flip bit when due)
- Evaluates pulse rules (track rising/falling edges on source, set/reset timer)

---

## 4. Pulse Behaviour

### Definition

A **pulse** sets a target bit high for a fixed duration, then resets it.
It triggers on an **edge** of a source bit (or optionally a manual trigger).

```yaml
output_rules:
  - target: EIP.Output.Bit2.0
    pulse: EIP.Input.Bit3.2
    duration_ms: 200
    edge: rising
```

- When `Input.Bit3.2` transitions from 0→1:
  - `Output.Bit2.0` is set to 1 immediately
  - A timer is started for 200 ms
  - When the timer fires, `Output.Bit2.0` is set back to 0
  - The rising edge is consumed; no new pulse until another 0→1

### Manual pulse (UI)

A keyboard/mouse action on any writable bit triggers a one-shot pulse:

```
pulse Output.Bit2.0 150ms
```

This is a command-bar command, not a config rule — it fires once and is not
persisted.  The implementation reuses the same timer mechanism.

### Pulse state tracking

The engine maintains a `PulseState` dict keyed by target spec:

```python
@dataclass
class _PulseState:
    active: bool = False
    expiry: float = 0.0       # time.monotonic() deadline
    edge_armed: bool = True   # ready to catch the next edge
```

---

## 5. Config & CLI Changes

### YAML config

```yaml
protocol: eip
address: 192.168.1.100
port: 44818
interval: 0.05              # 50 ms for fast I/O
write_mode: confirm
eip:
  input_assembly: 101
  output_assembly: 100
  config_assembly: 102
  rpi_ms: 50
variables:
  - EIP.Input.Byte0:heartbeat
  - EIP.Input.Int4:temperature
  - EIP.Output.DWord8:setpoint
output_rules:
  - target: EIP.Output.Bit2.0
    follow: EIP.Input.Bit3.1
    invert: true
  - target: EIP.Output.Bit2.1
    toggle_ms: 500
```

### CLI

```bash
# Explicit protocol flag
busfactor --protocol eip --input-assembly 101 --output-assembly 100 \
  --rpi 50 192.168.1.100 EIP.Input.Byte0 EIP.Output.Int4

# Default is still S7 for backward compat
busfactor 192.168.1.100 DB210.Byte0
```

### Proposed CLI flags (documented, future)

```
--output-rule "target=EIP.Output.Bit2.0 follow=EIP.Input.Bit3.1 invert"
--output-rule "target=EIP.Output.Bit2.1 toggle_ms=500"
--pulse-ms 150                       # default pulse duration for manual pulses
```

These are **not implemented in the first pass** — only config-file rules for now.
They are documented here so the spec format is settled.

---

## 6. Implementation Plan (commit order)

Each commit is a small, testable unit.

| # | Commit | Files | Tests |
|---|--------|-------|-------|
| 1 | Rename `S7Type` → `DataType`, extract shared type constants. | `variable.py` | existing pass |
| 2 | Add `Connection` ABC + `ConnectionConfig` to new `protocols.py`. | `protocols.py`, `connection.py` | `test_protocols.py` |
| 3 | Add EIP variable spec parsing. | `variable.py` | `test_variable.py` |
| 4 | Implement `EIPConnection` (scaffold + tests). | `eip.py`, `connection.py` | `test_eip.py` |
| 5 | Add `OutputRule` dataclass + rules engine. | `engine.py`, `rules.py` | `test_rules.py` |
| 6 | Wire rules into `MonitorEngine.poll()`. | `engine.py` | `test_engine.py` |
| 7 | Update `Config` / `CLI` for `--protocol` + EIP options. | `config.py`, `cli.py` | `test_config.py`, `test_cli.py` |
| 8 | Pulse command-bar command + UI bits. | `app.py`, `web.py` | manual |
| 9 | Update README / help text. | `README.md`, `cli.py` | — |

---

## 7. Open Questions

- **EIP library**: cpppo, pyepics, or a lightweight minimal implementation?
  The design assumes only `connect(address, port)`, `forward_open(...)`,
  `read_assembly(n)`, `write_assembly(n, data)`, `forward_close()` — a ~200
  line wrapper, not a full library.
  **Resolution**: Used Sebastian Block's `ethernetip` library (v1.1.2) which
  provides scanner-mode UDP I/O with background threads and bit-list buffers.
  `EIPConnection` wraps it behind the `Connection` ABC's byte-oriented
  `read_source`/`write_source` interface. Available locally at
  `/home/aaron/development/aaron/ethernetip-stuff/python-ethernetip`.
- **RPI vs. polled reads**: If using UDP I/O, data arrives asynchronously.
  For simplicity, v1 can use polled explicit messaging (read assembly on
  demand) — 50ms poll gives similar behaviour without the async complexity.
  Native UDP I/O can be a future optimisation.
  **Resolution**: The `ethernetip` library uses UDP I/O with background
  threads. `EIPConnection.read_source`/`write_source` work with the library's
  byte-buffer API (`input_bits`/`output_bits`). The polling pattern is the
  same as S7 — the engine's `poll()` loop reads assemblies synchronously.
  RPI is configured at connection time via `ConnectionConfig.rpi_ms`.
- **Multiple adapters**: Not in scope for v1. One adapter per process.

---

## 8. Reality vs. Plan

This section documents where the implementation deviated from the design
document, why, and what the implications are if we ever want to bring things
back inline with the original plan.

### 8.1 Connection abstraction

| Design | Reality |
|--------|---------|
| `read_groups()` → `dict` | `read_source(source, offset, size)` → `ReadResult` |
| `write_assembly(target, data)` | `write_source(source, offset, data)` |

**Why**: The design proposed protocol-level methods (`read_groups`,
`write_assembly`) but during implementation it became simpler to expose
byte-range `read_source`/`write_source` — same granularity as S7's
`area_read`/`area_write`. This maps naturally to both protocols (S7 reads a DB
range, EIP reads a slice of an assembly buffer) and lets the engine and
frontends stay completely protocol-agnostic.

**Implication**: If we want `write_assembly` (writing an entire assembly in one
shot for EIP atomicity), we can add it as an optional optimisation — the
`EIPConnection` already buffers the full assembly internally via
`_output_buffer`.

### 8.2 Variable spec

| Design | Reality |
|--------|---------|
| Universal `Variable` dataclass replacing both S7 & EIP | `S7Variable` and `EIPVariable` are separate frozen dataclasses |

**Why**: The two types have different fields (`S7Variable` has `area`/`db`,
`EIPVariable` has `assembly`). Forcing a single class would require either a
union type for `area`/`db`/`assembly` or a generic `protocol` + `source` string
approach. Keeping them separate gives clear type-checking and no ambiguity.
Both implement the same interface (`.source`, `.decode()`, `.encode()`,
`.parse_input()`, `.format_value()`), so the engine never needs to know which
type it's holding — it just calls `var.source`, `var.decode(bytes)`, etc.

**Implication**: Code that explicitly branches on `isinstance(v, S7Variable)`
needs updating when adding a new protocol. In practice the engine and frontends
never do this — they use the interface. The only place that branches is
`build_read_groups` where we check `hasattr(first, "area")` to preserve the S7
specific fields on `ReadGroup`.

### 8.3 Output rules — config format

| Design | Reality |
|--------|---------|
| `output_rules` list with `target` field + type-specific keys | `rules` dict keyed by target spec |
| `invert: true` | Not implemented |
| `toggle_ms: 500` → timer-based | `toggle: 2` → cycle-count-based |
| `pulse: source` → edge-triggered | `pulse: 5` → manual-trigger, duration in cycles |
| `edge: rising` | Not implemented |

**Why**: The list-of-dicts format requires iterating to find a rule by target;
the dict-keyed format (`rules: { target: { follow: source } }`) is a simpler
YAML structure and makes target-lookup O(1). Invert/edge/scale were deferred
to keep the first pass small. Toggle and pulse use poll-cycle counts instead
of wall-clock milliseconds because (a) the engine doesn't own a timer,
(b) cycle-count is deterministic across poll-interval changes, and (c) it keeps
the rules engine stateless with respect to real time.

**Implication**: To add `invert`, `edge`, `scale`, `toggle_ms`, `duration_ms`
later, the rule dataclasses gain new fields with `None` defaults (backward
compat). The YAML parser in `build_rules_engine()` would check for the new
keys. Timer-based pulse would require threading or `asyncio` — the current
cycle-count approach is simpler and adequate for typical short pulses (1–5
cycles).

### 8.4 Pulse trigger

| Design | Reality |
|--------|---------|
| Edge-triggered on source bit + manual command | Manual trigger only (`trigger_pulse(target)`) |

**Why**: Edge detection requires maintaining previous-source-value state and
a timer deadline. The manual-trigger approach (called programmatically via
`trigger_pulse()` or potentially from a command-bar `pulse` command) covers
the primary use case — one-shot output pulses — without the complexity of
per-source edge tracking.

**Implication**: Adding source-edge triggering later means maintaining a
`_previous_source: dict[str, bool]` in `RulesEngine` and calling trigger_pulse
on rising/falling edges detected during `_apply_follow`-style value reads.

### 8.5 Rule target exclusivity

| Design | Reality |
|--------|---------|
| Rules have exclusive ownership of their target; manual writes blocked | No exclusivity enforcement |

**Why**: Enforcing exclusivity requires tracking which specs are rule-owned and
checking every write, which adds coupling between the rule engine and the write
path. The first pass trusts the user not to configure conflicting rules.

**Implication**: To add exclusivity, store a `set[str]` of owned targets on the
`RulesEngine`, check it in `MonitorEngine._write()` and the TUI's write path,
and raise a clear error like `Cannot write to {spec}: it is managed by an
output rule ({rule_type})`.

### 8.6 CLI `--protocol` flag

| Design | Reality |
|--------|---------|
| `--protocol`, `--input-assembly`, `--output-assembly`, `--rpi` CLI flags | Config-file only |

**Why**: The CLI command already has 15+ options; adding EIP flags would add
another 6. EIP is expected to be configured via YAML files (which support
comments, rules, and assembly mappings). The `--protocol` flag can be added
when a user requests it.

**Implication**: Adding CLI flags later requires updating `load_merged_config`,
`main()` and `web_cli()` click decorators, and the `merge_cli` method on
`S7MonitorConfig`. The config-file path already works end-to-end.

### 8.7 ReadGroup

The original design didn't anticipate `ReadGroup` needing EIP support. The
dataclass was S7-specific (`area: S7Area`, `db: int`). In the implementation
we added an optional `_source: DataSource | None` field; when set it overrides
`area`/`db` for the `source`, `key`, and `label` properties. This is backward
compatible — all existing S7 code creates `ReadGroup(area=..., db=...)` which
leaves `_source=None`.

### 8.8 What was built extra

- **DataSource frozen dataclass** — Not in the original design. Provides a
  type-safe identifier (`DataSource("DB210")`, `DataSource("EIP.Input")`) with
  protocol-specific factory methods. The engine keys decode groups and variables
  by `str(var.source)` instead of protocol-specific labels, making it
  naturally protocol-agnostic.

- **EIPVariable as a separate class** — See §8.2 above.

- **RulesEngine as an injected dependency** — `MonitorEngine` and
  `S7MonitorApp` accept `rules_engine: RulesEngine | None = None` instead of
  the engine owning the rules. This lets tests inject a mock, or users run
  rules-free.

- **`_source` field on ReadGroup** — See §8.7 above.

### 8.9 Current test coverage

All tests pass: **253 tests**, covering:

| Area | Tests |
|------|-------|
| S7 variable parsing | 65 |
| EIP variable parsing | 15 |
| EIP connection (mock library) | 29 |
| Engine (poll, write, groups) | 21 |
| Rules engine (follow/toggle/pulse) | 16 |
| Config (YAML + merge) | 14 |
| CLI (parse, build_read_groups) | 19 |
| Web (SSE, write, control) | 18 |
| Demo, replay, logging, runtime | rest |

Missing: end-to-end integration tests with real or simulated hardware for both
S7 and EIP.
