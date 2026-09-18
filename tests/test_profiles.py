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

    def test_read_window_is_two_registers(self):
        group = load("onrobot-rg-status.yaml").read_groups[0]
        assert str(group.source) == "MB.Holding"
        # Registers 267..268, as bytes. Short reads survive a lossy gateway.
        assert group.start == 534
        assert group.size == 4

    def test_every_byte_read_is_decoded_by_a_variable(self):
        runtime = load("onrobot-rg-status.yaml")
        group = runtime.read_groups[0]
        covered = set()
        for var in runtime.variables:
            covered.update(range(var.offset, var.offset + var.byte_size))
        assert covered == set(range(group.start, group.start + group.size))

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


class TestOnRobot3FG15StatusProfile:
    def test_shares_the_flange_bus_settings_with_the_rg(self):
        three = load("onrobot-3fg15-status.yaml").connection.config
        rg = load("onrobot-rg-status.yaml").connection.config
        # The slave id is the flange tool address, not the gripper model.
        assert (three.tcp_port, three.slave_id, three.framer) == (
            rg.tcp_port, rg.slave_id, rg.framer)

    def test_is_read_only(self):
        assert load("onrobot-3fg15-status.yaml").write_mode == WriteMode.DISABLED

    def test_window_is_the_block_the_plugin_reads(self):
        group = load("onrobot-3fg15-status.yaml").read_groups[0]
        # Registers 256..258, as bytes.
        assert group.start == 512
        assert group.size == 6

    def test_status_bits_are_named(self):
        labels = {v.label for v in load("onrobot-3fg15-status.yaml").variables}
        assert labels >= {"busy", "grip detected", "force grip detected",
                          "calibration ok"}

    def test_busy_is_bit_0_of_register_256(self):
        variables = modbus_variables("onrobot-3fg15-status.yaml")
        busy = next(v for v in variables if v.label == "busy")
        assert busy.register == 256
        assert busy.extra == 0

    def test_diameter_is_signed(self):
        variables = modbus_variables("onrobot-3fg15-status.yaml")
        diameter = next(v for v in variables if v.label == "diameter 0.1mm")
        assert diameter.register == 258
        # The fingertip offset can drive it below zero.
        assert diameter.decode(bytearray([0xFF, 0xFF])) == -1

    def test_raw_diameter_is_unsigned(self):
        variables = modbus_variables("onrobot-3fg15-status.yaml")
        raw = next(v for v in variables if v.label == "raw diameter 0.1mm")
        assert raw.decode(bytearray([0x02, 0x46])) == 582


class TestOnRobot3FG15CommandProfile:
    def test_writes_need_confirmation(self):
        assert load("onrobot-3fg15-command.yaml").write_mode == WriteMode.CONFIRM

    def test_covers_the_four_command_registers(self):
        group = load("onrobot-3fg15-command.yaml").read_groups[0]
        assert group.start == 0
        assert group.size == 8

    def test_command_registers_are_in_order(self):
        variables = modbus_variables("onrobot-3fg15-command.yaml")
        assert [v.register for v in variables] == [0, 1, 2, 3]
