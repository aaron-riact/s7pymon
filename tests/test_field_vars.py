"""Tests for register-map field variable expansion."""

import pytest

from s7pymon.field_vars import expand_field_vars
from s7pymon.variable import DataType, EIPVariable


class TestExpandFieldVars:
    """Unit tests for expand_field_vars()."""

    def test_single_register_bits(self):
        cfg = {
            "EIP.Input": {
                "base_register": 18178,
                "register_width_bits": 16,
                "fields": [
                    {18178: ["Bit0.0:heartbeat", "Bit0.1:machine_ready"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 2
        assert result[0].type == DataType.BIT
        assert result[0].offset == 0
        assert result[0].extra == 0
        assert result[0].label == "heartbeat"
        assert result[1].offset == 0
        assert result[1].extra == 1
        assert result[1].label == "machine_ready"

    def test_register_nonzero_base_offset(self):
        """Register 18179 maps to assembly offset (18179-18178)*2 = 2."""
        cfg = {
            "EIP.Input": {
                "base_register": 18178,
                "register_width_bits": 16,
                "fields": [
                    {18179: ["Bit0.0:seat_detect_1"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 1
        assert result[0].offset == 2
        assert result[0].extra == 0

    def test_multiple_registers_spread(self):
        """Variables at registers 18178 (offset 0) and 18200 (offset 44)."""
        cfg = {
            "EIP.Input": {
                "base_register": 18178,
                "register_width_bits": 16,
                "fields": [
                    {18178: ["Bit0.0:heartbeat"]},
                    {18200: ["Chars0.32:program"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 2
        assert result[0].offset == 0
        assert result[0].type == DataType.BIT
        assert result[0].label == "heartbeat"
        assert result[1].offset == 44
        assert result[1].type == DataType.CHARS
        assert result[1].extra == 32
        assert result[1].label == "program"

    def test_register_width_32_bits(self):
        """register_width_bits=32 means 4-byte step."""
        cfg = {
            "EIP.Input": {
                "base_register": 0,
                "register_width_bits": 32,
                "fields": [
                    {0: ["DWord0:ctrl"]},
                    {1: ["DWord0:status"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 2
        assert result[0].offset == 0
        assert result[0].label == "ctrl"
        assert result[1].offset == 4
        assert result[1].label == "status"

    def test_base_register_non_zero(self):
        """When base_register=100, register 100 → offset 0, 101 → offset 2."""
        cfg = {
            "EIP.Input": {
                "base_register": 100,
                "register_width_bits": 16,
                "fields": [
                    {100: ["Byte0:first"]},
                    {101: ["Byte0:second"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert result[0].offset == 0
        assert result[0].label == "first"
        assert result[1].offset == 2
        assert result[1].label == "second"

    def test_no_label_spec(self):
        """Var spec without :label still works."""
        cfg = {
            "EIP.Input": {
                "base_register": 18178,
                "register_width_bits": 16,
                "fields": [
                    {18178: ["Bit0.0"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 1
        assert result[0].label is None
        assert result[0].offset == 0

    def test_output_assembly(self):
        """field_vars works with EIP.Output too."""
        cfg = {
            "EIP.Output": {
                "base_register": 18178,
                "register_width_bits": 16,
                "fields": [
                    {18178: ["Byte0:output_byte"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 1
        assert result[0].assembly == "Output"
        assert result[0].offset == 0

    def test_multiple_assemblies(self):
        """Multiple assembly keys are expanded independently."""
        cfg = {
            "EIP.Input": {
                "base_register": 100,
                "register_width_bits": 16,
                "fields": [{100: ["Byte0:in"]}],
            },
            "EIP.Output": {
                "base_register": 200,
                "register_width_bits": 16,
                "fields": [{200: ["Byte0:out"]}],
            },
        }
        result = expand_field_vars(cfg)
        assert len(result) == 2
        assert result[0].assembly == "Input"
        assert result[1].assembly == "Output"

    def test_int_type(self):
        """Int type at register offset works correctly."""
        cfg = {
            "EIP.Input": {
                "base_register": 0,
                "register_width_bits": 16,
                "fields": [
                    {0: ["Int0:position"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 1
        assert result[0].type == DataType.INT
        assert result[0].offset == 0
        assert result[0].label == "position"

    def test_spec_preserves_byte_order(self):
        """Byte-order suffix on spec is preserved through expansion."""
        cfg = {
            "EIP.Input": {
                "base_register": 10,
                "register_width_bits": 16,
                "fields": [
                    {10: ["Word0.be:big_word"]},
                ],
            }
        }
        result = expand_field_vars(cfg)
        assert len(result) == 1
        assert result[0].byte_order.value == "big"
        assert result[0].label == "big_word"

    # --- validation tests ---

    def test_invalid_base_register_type(self):
        with pytest.raises(ValueError, match="base_register"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": "not_an_int",
                    "register_width_bits": 16,
                    "fields": [],
                }
            })

    def test_invalid_register_width(self):
        with pytest.raises(ValueError, match="register_width_bits"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": 0,
                    "register_width_bits": 7,
                    "fields": [],
                }
            })

    def test_register_below_base(self):
        with pytest.raises(ValueError, match="less than base_register"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": 100,
                    "register_width_bits": 16,
                    "fields": [{50: ["Byte0:bad"]}],
                }
            })

    def test_invalid_spec_string(self):
        with pytest.raises(ValueError, match="Error parsing field var"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": 0,
                    "register_width_bits": 16,
                    "fields": [{0: ["not_a_spec"]}],
                }
            })

    def test_non_dict_assembly_cfg(self):
        with pytest.raises(ValueError, match="Expected dict"):
            expand_field_vars({"EIP.Input": "not_a_dict"})

    def test_fields_not_list(self):
        with pytest.raises(ValueError, match="fields must be a list"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": 0,
                    "register_width_bits": 16,
                    "fields": "not_a_list",
                }
            })

    def test_field_entry_not_dict(self):
        with pytest.raises(ValueError, match="single key"):
            expand_field_vars({
                "EIP.Input": {
                    "base_register": 0,
                    "register_width_bits": 16,
                    "fields": ["not_a_dict"],
                }
            })

    def test_empty_config(self):
        assert expand_field_vars({}) == []


class TestExpandFieldVarsIntegration:
    """Integration tests: field_vars expanded within resolve_runtime."""

    def test_field_vars_and_flat_variables_merge(self):
        """field_vars and variables are merged into one list via resolve_runtime."""
        from s7pymon.cli import resolve_runtime
        from s7pymon.config import S7MonitorConfig

        cfg = S7MonitorConfig(
            address="10.0.0.1",
            protocol="eip",
            variables=["EIP.Input.Byte0:flat_var"],
            input_size=64,
            field_vars={
                "EIP.Input": {
                    "base_register": 18178,
                    "register_width_bits": 16,
                    "fields": [
                        {18178: ["Bit0.0:field_bit"]},
                    ],
                }
            },
        )
        rt = resolve_runtime(cfg)
        specs = [v.spec for v in rt.variables]
        assert "EIP.Input.Byte0" in specs
        assert "EIP.Input.Bit0.0" in specs

    def test_field_vars_without_flat_variables(self):
        """field_vars alone (no flat variables) works."""
        from s7pymon.cli import resolve_runtime
        from s7pymon.config import S7MonitorConfig

        cfg = S7MonitorConfig(
            address="10.0.0.1",
            protocol="eip",
            input_size=64,
            field_vars={
                "EIP.Input": {
                    "base_register": 18178,
                    "register_width_bits": 16,
                    "fields": [
                        {18178: ["Byte0:field_byte"]},
                    ],
                }
            },
        )
        rt = resolve_runtime(cfg)
        assert len(rt.variables) == 1
        assert rt.variables[0].label == "field_byte"
