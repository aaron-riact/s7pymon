"""Output rules for automatic assembly management.

Rules run between the read and write phases of each poll cycle:

* **Follow** — copies an input value to an output variable every cycle.
* **Toggle** — alternates a bit every N cycles (heartbeat / watchdog).
* **Pulse** — sets a bit high for N cycles when explicitly triggered.

Rules are protocol-agnostic: source and target can be S7 DBs, EIP assemblies,
or mixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocols import Connection
from .variable import DataType, S7Variable


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


_RuleKey = int


class RulesEngine:
    def __init__(self, rules: list[OutputRule]):
        self._rules = rules
        self._counters: dict[_RuleKey, int] = {}
        self._toggle_state: dict[_RuleKey, bool] = {}
        self._pulse_remaining: dict[_RuleKey, int] = {}

    @property
    def rules(self) -> list[OutputRule]:
        return list(self._rules)

    def trigger_pulse(self, target: str) -> None:
        for rule in self._rules:
            if isinstance(rule, PulseRule) and rule.target == target:
                self._pulse_remaining[id(rule)] = rule.duration
                return
        raise KeyError(f"No pulse rule for {target!r}")

    def apply(
        self, connection: Connection, current_values: dict[str, str]
    ) -> None:
        for rule in self._rules:
            if isinstance(rule, FollowRule):
                self._apply_follow(rule, connection, current_values)
            elif isinstance(rule, ToggleRule):
                self._apply_toggle(rule, connection)
            elif isinstance(rule, PulseRule):
                self._apply_pulse(rule, connection)

    def _apply_follow(
        self,
        rule: FollowRule,
        connection: Connection,
        current_values: dict[str, str],
    ) -> None:
        formatted = current_values.get(rule.source)
        if formatted is None:
            return
        target_var = S7Variable.parse(rule.target)
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

    def _apply_toggle(self, rule: ToggleRule, connection: Connection) -> None:
        key = id(rule)
        counter = self._counters.get(key, 0) + 1
        target_var = S7Variable.parse(rule.target)

        if counter >= rule.period:
            self._counters[key] = 0
            state = self._toggle_state.get(key, False)
            self._toggle_state[key] = not state
            self._write_toggle_state(connection, target_var, not state)
        else:
            self._counters[key] = counter

    def _write_toggle_state(
        self,
        connection: Connection,
        var: Any,
        state: bool,
    ) -> None:
        if var.type == DataType.BIT:
            current = connection.read_source(var.source, var.offset, 1)
            encoded = var.encode_bit(current.data[0], state)
        else:
            encoded = var.encode(1 if state else 0)
        connection.write_source(var.source, var.offset, encoded)

    def _apply_pulse(self, rule: PulseRule, connection: Connection) -> None:
        key = id(rule)
        remaining = self._pulse_remaining.get(key, 0)
        target_var = S7Variable.parse(rule.target)

        if remaining > 0:
            self._pulse_remaining[key] = remaining - 1
            self._write_toggle_state(connection, target_var, True)
        else:
            self._write_toggle_state(connection, target_var, False)
