import pytest
from click.testing import CliRunner

from s7pymon.cli import (
    build_default_variables,
    main,
    parse_variable_arg,
)
from s7pymon.variable import S7Type, S7Variable


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


class TestCLIHelp:
    def test_help_exits_0(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "S7 Monitor" in result.output

    def test_no_args_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code != 0

    def test_no_variables_no_db_shows_error(self):
        runner = CliRunner()
        # Providing address but no variables and no --db/--size
        result = runner.invoke(main, ["192.168.1.1"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "Provide variable specs" in result.output

    def test_invalid_variable_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(main, ["192.168.1.1", "INVALID"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "Error parsing variable" in result.output

    def test_multiple_dbs_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["192.168.1.1", "DB100.Byte0", "DB200.Byte0"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "same DB" in result.output

    def test_conflicting_db_flag_shows_error(self):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["192.168.1.1", "--db", "999", "DB210.Byte0"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "conflicts" in result.output
