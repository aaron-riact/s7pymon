"""Tests for the shared config->runtime resolver in cli.py."""

import pytest

from busfactor.cli import (
    ResolvedRuntime,
    RuntimeConfigError,
    load_merged_config,
    resolve_runtime,
)
from busfactor.config import S7MonitorConfig
from busfactor.engine import WriteMode
from busfactor.protocols import DataSource
from busfactor.datalog import LogFormat
from busfactor.variable import S7Area


def cfg(**kw):
    return S7MonitorConfig(**kw)


class TestResolveRuntime:
    def test_requires_address(self):
        with pytest.raises(RuntimeConfigError, match="ADDRESS is required"):
            resolve_runtime(cfg(variables=["DB210.Byte0"]))

    def test_requires_variables_or_range(self):
        with pytest.raises(RuntimeConfigError, match="variable specs"):
            resolve_runtime(cfg(address="10.0.0.1"))

    def test_unknown_protocol_is_an_error(self):
        # Defaulting a misspelt protocol to S7 would point the wrong driver at the device.
        with pytest.raises(RuntimeConfigError, match="Unknown protocol"):
            resolve_runtime(cfg(protocol="modbuss", address="10.0.0.1", variables=["MB.Holding.Word0"]))

    def test_basic_variables(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0", "DB210.Int4"]))
        assert isinstance(rt, ResolvedRuntime)
        assert rt.connection.config.address == "10.0.0.1"
        assert [v.spec for v in rt.variables] == ["DB210.Byte0", "DB210.Int4"]
        assert len(rt.read_groups) == 1
        assert rt.read_groups[0].source == DataSource.s7_db(210)

    def test_defaults(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0"]))
        assert rt.connection.config.rack == 0
        assert rt.connection.config.slot == 2
        assert rt.connection.config.tcp_port == 102
        assert rt.poll_interval == 1.0
        assert rt.write_mode == WriteMode.DISABLED
        assert rt.log_format == LogFormat.CSV

    def test_scalar_overrides(self):
        rt = resolve_runtime(cfg(
            address="10.0.0.1", rack=1, slot=0, port=1102, timeout=5000,
            interval=0.25, write_mode="allowed", variables=["DB210.Byte0"],
            log_file="out.csv", log_format="jsonl",
        ))
        assert rt.connection.config.rack == 1
        assert rt.connection.config.slot == 0
        assert rt.connection.config.tcp_port == 1102
        assert rt.connection.config.timeout_ms == 5000
        assert rt.poll_interval == 0.25
        assert rt.write_mode == WriteMode.ALLOWED
        assert rt.log_file == "out.csv"
        assert rt.log_format == LogFormat.JSONL

    def test_raw_range_mode(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", db=210, start=0, size=4))
        assert len(rt.variables) == 4
        assert rt.read_groups[0].size == 4

    def test_db_size_extends_range(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0"], size=18))
        assert rt.read_groups[0].size == 18

    def test_db_conflict(self):
        with pytest.raises(RuntimeConfigError, match="conflicts"):
            resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0"], db=99, size=4))

    def test_bad_variable(self):
        with pytest.raises(RuntimeConfigError, match="Error parsing variable"):
            resolve_runtime(cfg(address="10.0.0.1", variables=["not-a-spec"]))

    def test_labels_preserved(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0:heartbeat"]))
        assert rt.variables[0].label == "heartbeat"


class TestLoadMergedConfig:
    def test_cli_overrides_only(self):
        merged = load_merged_config(
            None, address="1.2.3.4", rack=2, slot=None, port=None, timeout=None,
            interval=None, write_mode=None, db_number=None, db_start=None,
            db_size=None, variables=("DB1.Byte0",), log_file=None, log_format=None,
        )
        assert merged.address == "1.2.3.4"
        assert merged.rack == 2
        assert merged.variables == ["DB1.Byte0"]


class TestResolveModbusRuntime:
    def test_selects_the_modbus_connection(self):
        rt = resolve_runtime(cfg(
            protocol="modbus", address="192.168.0.119", variables=["MB.Holding.Word534"]))
        assert rt.connection.protocol == "modbus"

    def test_defaults_to_port_502_slave_1_socket_framing(self):
        rt = resolve_runtime(cfg(
            protocol="modbus", address="10.0.0.1", variables=["MB.Holding.Word0"]))
        assert rt.connection.config.tcp_port == 502
        assert rt.connection.config.slave_id == 1
        assert rt.connection.config.framer == "socket"

    def test_port_slave_and_framer_come_from_config(self):
        rt = resolve_runtime(cfg(
            protocol="modbus", address="192.168.0.119", port=60000,
            slave_id=65, framer="rtu", variables=["MB.Holding.Word534"]))
        assert rt.connection.config.tcp_port == 60000
        assert rt.connection.config.slave_id == 65
        assert rt.connection.config.framer == "rtu"

    def test_read_group_covers_the_monitored_registers(self):
        rt = resolve_runtime(cfg(
            protocol="modbus", address="10.0.0.1",
            variables=["MB.Holding.Word534", "MB.Holding.Word536"]))
        assert len(rt.read_groups) == 1
        group = rt.read_groups[0]
        assert str(group.source) == "MB.Holding"
        assert group.start == 534
        assert group.size == 4

    def test_separate_tables_get_separate_read_groups(self):
        rt = resolve_runtime(cfg(
            protocol="modbus", address="10.0.0.1",
            variables=["MB.Holding.Word0", "MB.Coil.Bit0.0"]))
        assert {str(g.source) for g in rt.read_groups} == {"MB.Holding", "MB.Coil"}
