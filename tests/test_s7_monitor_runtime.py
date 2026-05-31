"""Tests for the shared config->runtime resolver in cli.py."""

import pytest

from s7pymon.cli import (
    ResolvedRuntime,
    RuntimeConfigError,
    load_merged_config,
    resolve_runtime,
)
from s7pymon.config import S7MonitorConfig
from s7pymon.engine import WriteMode
from s7pymon.logging import LogFormat
from s7pymon.variable import S7Area


def cfg(**kw):
    return S7MonitorConfig(**kw)


class TestResolveRuntime:
    def test_requires_address(self):
        with pytest.raises(RuntimeConfigError, match="ADDRESS is required"):
            resolve_runtime(cfg(variables=["DB210.Byte0"]))

    def test_requires_variables_or_range(self):
        with pytest.raises(RuntimeConfigError, match="variable specs"):
            resolve_runtime(cfg(address="10.0.0.1"))

    def test_basic_variables(self):
        rt = resolve_runtime(cfg(address="10.0.0.1", variables=["DB210.Byte0", "DB210.Int4"]))
        assert isinstance(rt, ResolvedRuntime)
        assert rt.connection.config.address == "10.0.0.1"
        assert [v.spec for v in rt.variables] == ["DB210.Byte0", "DB210.Int4"]
        assert len(rt.read_groups) == 1
        assert rt.read_groups[0].area == S7Area.DB

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
