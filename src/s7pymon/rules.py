"""Output rules for automatic assembly management.

Rules run between the read and write phases of each poll cycle:

* **Follow** — copies an input value to an output variable every cycle.
* **Toggle** — alternates a bit every N cycles (heartbeat / watchdog).
* **Pulse** — sets a bit high for N cycles when explicitly triggered.

Rules are protocol-agnostic: source and target can be S7 DBs, EIP assemblies,
or mixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Sequence

from .protocols import Connection
from .variable import DataType, Variable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutputRule:
    target: str


@dataclass(frozen=True)
class FollowRule(OutputRule):
    source: str


@dataclass(frozen=True)
class ToggleRule(OutputRule):
    period: int = 1


@dataclass(frozen=True)
class PulseRule(OutputRule):
    duration: int = 1


@dataclass
class _RuleState:
    """What the engine remembers about one rule between poll cycles.

    The rules themselves are frozen config, so the target is parsed once
    here and the counters live beside it instead of in dicts keyed by the
    rule object.
    """

    rule: OutputRule
    target: Variable
    counter: int = 0  # toggle: cycles since the last flip
    toggle_on: bool = False
    pulse_remaining: int = 0


class RulesEngine:
    def __init__(self, rules: Sequence[OutputRule]):
        self._states = [_RuleState(rule, Variable.parse(rule.target)) for rule in rules]

    @property
    def rules(self) -> list[OutputRule]:
        return [state.rule for state in self._states]

    def trigger_pulse(self, target: str) -> None:
        for state in self._states:
            if isinstance(state.rule, PulseRule) and state.rule.target == target:
                state.pulse_remaining = state.rule.duration
                return
        raise KeyError(f"No pulse rule for {target!r}")

    def apply(
        self,
        connection: Connection,
        current_values: dict[str, str],
        buffers: dict[str, tuple[bytearray, int]] | None = None,
    ) -> None:
        log.debug("apply() with %d rules, %d values", len(self._states), len(current_values))
        for state in self._states:
            rule = state.rule
            if isinstance(rule, FollowRule):
                self._apply_follow(rule, state.target, connection, current_values)
            elif isinstance(rule, ToggleRule):
                self._apply_toggle(rule, state, connection, buffers)
            elif isinstance(rule, PulseRule):
                self._apply_pulse(state, connection)

    def _apply_follow(
        self,
        rule: FollowRule,
        target_var: Variable,
        connection: Connection,
        current_values: dict[str, str],
    ) -> None:
        formatted = current_values.get(rule.source)
        if formatted is None:
            log.debug("follow %s <- %s: source not in current_values, skipping", rule.target, rule.source)
            return
        log.debug("follow %s <- %s: value=%s", rule.target, rule.source, formatted)
        parsed = target_var.parse_input(formatted)
        if target_var.type == DataType.BIT:
            if not isinstance(parsed, bool):
                return
            current = connection.read_source(
                target_var.source, target_var.offset, 1
            )
            encoded = target_var.encode_bit(current.data[0], parsed)
        else:
            encoded = target_var.encode(parsed)
        connection.write_source(target_var.source, target_var.offset, encoded)

    def _apply_toggle(
        self,
        rule: ToggleRule,
        state: _RuleState,
        connection: Connection,
        buffers: dict[str, tuple[bytearray, int]] | None = None,
    ) -> None:
        state.counter += 1
        log.debug("toggle %s period=%d counter=%d/%d", rule.target, rule.period, state.counter, rule.period)
        if state.counter < rule.period:
            return
        state.counter = 0
        state.toggle_on = not state.toggle_on
        log.debug("toggle %s -> firing, new_state=%s", rule.target, state.toggle_on)
        self._write_bit_state(connection, state.target, state.toggle_on, buffers)

    def _write_bit_state(
        self,
        connection: Connection,
        var: Variable,
        state: bool,
        buffers: dict[str, tuple[bytearray, int]] | None = None,
    ) -> None:
        if var.type == DataType.BIT:
            # Use the buffer from the poll cycle instead of a separate read
            current_byte = None
            if buffers is not None:
                entry = buffers.get(str(var.source))
                if entry is not None:
                    data, data_start = entry
                    current_byte = data[var.offset - data_start]
            if current_byte is None:
                current = connection.read_source(var.source, var.offset, 1)
                current_byte = current.data[0]
            encoded = var.encode_bit(current_byte, state)
        else:
            encoded = var.encode(1 if state else 0)
        connection.write_source(var.source, var.offset, encoded)

    def _apply_pulse(self, state: _RuleState, connection: Connection) -> None:
        if state.pulse_remaining > 0:
            state.pulse_remaining -= 1
            self._write_bit_state(connection, state.target, True, None)
        else:
            self._write_bit_state(connection, state.target, False, None)
