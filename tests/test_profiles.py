"""Tests for the shipped device profiles.

These load the real YAML files. A typo in a profile is otherwise only found
by pointing busfactor at hardware, which is the worst place to find it.
"""

from pathlib import Path

import pytest

from busfactor.cli import resolve_runtime
from busfactor.config import S7MonitorConfig
from busfactor.engine import WriteMode
from busfactor.variable import ModbusVariable

PROFILES = Path(__file__).parent.parent / "profiles"


def load(name: str):
    return resolve_runtime(S7MonitorConfig.from_yaml(PROFILES / name))


def modbus_variables(name: str) -> list[ModbusVariable]:
    """A profile's variables, which are all Modbus ones."""
    variables = load(name).variables
    assert all(isinstance(v, ModbusVariable) for v in variables)
    return [v for v in variables if isinstance(v, ModbusVariable)]


@pytest.mark.parametrize("name", [p.name for p in sorted(PROFILES.glob("*.yaml"))])
def test_profile_resolves(name):
    runtime = load(name)
    assert runtime.connection.protocol == "modbus"
    assert runtime.variables
    assert runtime.read_groups


class TestOnRobotStatusProfile:
    def test_bus_settings_match_the_dobot_flange_gateway(self):
        config = load("onrobot-rg-status.yaml").connection.config
        assert config.tcp_port == 60000
        assert config.slave_id == 65
        assert config.framer == "rtu"

    def test_is_read_only(self):
        assert load("onrobot-rg-status.yaml").write_mode == WriteMode.DISABLED

    def test_read_window_stays_inside_the_implemented_registers(self):
        group = load("onrobot-rg-status.yaml").read_groups[0]
        assert str(group.source) == "MB.Holding"
        # Registers 267..275, as bytes.
        assert group.start == 534
        assert group.size == 18

    def test_status_bits_are_named(self):
        labels = {v.label for v in load("onrobot-rg-status.yaml").variables}
        assert "busy" in labels
        assert "grip detected" in labels
        assert "safety error - a switch was pushed at power on" in labels

    def test_safety_error_reads_bit_6_of_register_268(self):
        variables = modbus_variables("onrobot-rg-status.yaml")
        safety = next(v for v in variables if (v.label or "").startswith("safety error"))
        assert safety.register == 268
        assert safety.extra == 6

    def test_width_decodes_big_endian(self):
        variables = modbus_variables("onrobot-rg-status.yaml")
        width = next(v for v in variables if v.label == "actual width 0.1mm")
        assert width.register == 267
        assert width.decode(bytearray([0x01, 0x6C])) == 364


class TestOnRobotCommandProfile:
    def test_writes_need_confirmation(self):
        assert load("onrobot-rg-command.yaml").write_mode == WriteMode.CONFIRM

    def test_covers_the_three_command_registers(self):
        group = load("onrobot-rg-command.yaml").read_groups[0]
        assert group.start == 0
        assert group.size == 6

    def test_command_registers_are_in_order(self):
        variables = modbus_variables("onrobot-rg-command.yaml")
        assert [v.register for v in variables] == [0, 1, 2]
