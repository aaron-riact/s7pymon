import pytest
from click.testing import CliRunner

from s7pymon.app import WriteMode
from s7pymon.cli import (
    build_default_variables,
    build_read_groups,
    main,
    parse_variable_arg,
)
from s7pymon.variable import S7Area, S7Type, S7Variable


class TestParseVariableArg:
    def test_simple_spec(self):
        v = parse_variable_arg("DB210.Byte0")
        assert v.db == 210
        assert v.type == S7Type.BYTE
        assert v.label is None

    def test_spec_with_label(self):
        v = parse_variable_arg("DB210.Byte0:heartbeat")
        assert v.db == 210
        assert v.type == S7Type.BYTE
        assert v.label == "heartbeat"
        assert v.display_name == "heartbeat"

    def test_bit_with_label(self):
        v = parse_variable_arg("DB210.Bit1.0:e_stop")
        assert v.type == S7Type.BIT
        assert v.offset == 1
        assert v.extra == 0
        assert v.label == "e_stop"

    def test_invalid_spec(self):
        with pytest.raises(ValueError):
            parse_variable_arg("NOPE")

    def test_area_spec(self):
        v = parse_variable_arg("EB.Byte0:input0")
        assert v.area == S7Area.EB
        assert v.label == "input0"


class TestBuildDefaultVariables:
    def test_creates_byte_variables(self):
        vars = build_default_variables(db=210, start=0, size=3)
        assert len(vars) == 3
        assert all(v.type == S7Type.BYTE for v in vars)
        assert [v.offset for v in vars] == [0, 1, 2]
        assert vars[0].label == "byte_0"
        assert vars[0].db == 210

    def test_with_offset_start(self):
        vars = build_default_variables(db=100, start=5, size=2)
        assert [v.offset for v in vars] == [5, 6]


class TestBuildReadGroups:
    def test_single_db_group(self):
        vars = [
            S7Variable.parse("DB210.Byte0"),
            S7Variable.parse("DB210.Byte5"),
        ]
        groups = build_read_groups(vars)
        assert len(groups) == 1
        assert groups[0].area == S7Area.DB
        assert groups[0].db == 210
        assert groups[0].start == 0
        assert groups[0].size == 6

    def test_mixed_areas(self):
        vars = [
            S7Variable.parse("DB210.Byte0"),
            S7Variable.parse("EB.Byte0"),
        ]
        groups = build_read_groups(vars)
        assert len(groups) == 2
        areas = {g.area for g in groups}
        assert areas == {S7Area.DB, S7Area.EB}

    def test_multiple_dbs(self):
        vars = [
            S7Variable.parse("DB100.Byte0"),
            S7Variable.parse("DB200.Byte0"),
        ]
        groups = build_read_groups(vars)
        assert len(groups) == 2


class TestCLIHelp:
    def test_help_exits_0(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "s7pymon" in result.output

    def test_no_args_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code != 0

    def test_no_variables_no_db_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(main, ["192.168.1.1"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "Provide variable specs" in result.output

    def test_invalid_variable_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(main, ["192.168.1.1", "INVALID"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "Error parsing variable" in result.output

    def test_conflicting_db_flag_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["192.168.1.1", "--db", "999", "DB210.Byte0"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "conflicts" in result.output


class TestWriteMode:
    def test_write_mode_enum_values(self):
        assert WriteMode.DISABLED.value == "disabled"
        assert WriteMode.CONFIRM.value == "confirm"
        assert WriteMode.ALLOWED.value == "allowed"

    def test_write_mode_from_string(self):
        assert WriteMode("disabled") == WriteMode.DISABLED
        assert WriteMode("confirm") == WriteMode.CONFIRM
        assert WriteMode("allowed") == WriteMode.ALLOWED

    def test_help_shows_write_mode_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "--write-mode" in result.output
        assert "disabled" in result.output
