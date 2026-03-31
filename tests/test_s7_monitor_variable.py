import struct
import pytest

from s7pymon.variable import (
    S7Type,
    S7Variable,
    compute_read_range,
    extract_value,
)


class TestS7VariableParsing:
    def test_parse_byte(self):
        v = S7Variable.parse("DB200.Byte0")
        assert v.db == 200
        assert v.type == S7Type.BYTE
        assert v.offset == 0
        assert v.extra is None
        assert v.spec == "DB200.Byte0"

    def test_parse_int(self):
        v = S7Variable.parse("DB210.Int4")
        assert v.db == 210
        assert v.type == S7Type.INT
        assert v.offset == 4
        assert v.byte_size == 2

    def test_parse_dint(self):
        v = S7Variable.parse("DB100.DInt8")
        assert v.db == 100
        assert v.type == S7Type.DINT
        assert v.offset == 8
        assert v.byte_size == 4

    def test_parse_word(self):
        v = S7Variable.parse("DB5.Word2")
        assert v.db == 5
        assert v.type == S7Type.WORD
        assert v.offset == 2
        assert v.byte_size == 2

    def test_parse_dword(self):
        v = S7Variable.parse("DB1.DWord6")
        assert v.type == S7Type.DWORD
        assert v.byte_size == 4

    def test_parse_real(self):
        v = S7Variable.parse("DB200.Real12")
        assert v.type == S7Type.REAL
        assert v.offset == 12
        assert v.byte_size == 4

    def test_parse_bit(self):
        v = S7Variable.parse("DB200.Bit0.3")
        assert v.type == S7Type.BIT
        assert v.offset == 0
        assert v.extra == 3
        assert v.byte_size == 1

    def test_parse_bit_requires_bit_number(self):
        with pytest.raises(ValueError, match="requires bit number"):
            S7Variable.parse("DB200.Bit0")

    def test_parse_bit_validates_range(self):
        with pytest.raises(ValueError, match="must be 0-7"):
            S7Variable.parse("DB200.Bit0.8")

    def test_parse_string(self):
        v = S7Variable.parse("DB200.String50.20")
        assert v.type == S7Type.STRING
        assert v.offset == 50
        assert v.extra == 20
        assert v.byte_size == 22  # 20 + 2 header bytes

    def test_parse_string_requires_length(self):
        with pytest.raises(ValueError, match="requires max length"):
            S7Variable.parse("DB200.String50")

    def test_parse_case_insensitive(self):
        v = S7Variable.parse("db200.byte0")
        assert v.db == 200
        assert v.type == S7Type.BYTE

    def test_parse_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid variable spec"):
            S7Variable.parse("INVALID")

    def test_parse_with_label(self):
        v = S7Variable.parse("DB200.Byte0", label="heartbeat")
        assert v.display_name == "heartbeat"

    def test_display_name_falls_back_to_spec(self):
        v = S7Variable.parse("DB200.Byte0")
        assert v.display_name == "DB200.Byte0"

    def test_spec_with_extra(self):
        v = S7Variable.parse("DB200.Bit0.3")
        assert v.spec == "DB200.Bit0.3"

    def test_spec_without_extra(self):
        v = S7Variable.parse("DB200.Byte5")
        assert v.spec == "DB200.Byte5"


class TestS7VariableDecode:
    def test_decode_byte(self):
        v = S7Variable.parse("DB1.Byte0")
        assert v.decode(b"\x2a") == 42

    def test_decode_int_positive(self):
        v = S7Variable.parse("DB1.Int0")
        # Big-endian signed 16-bit: 0x00FF = 255
        assert v.decode(b"\x00\xff") == 255

    def test_decode_int_negative(self):
        v = S7Variable.parse("DB1.Int0")
        # Big-endian signed 16-bit: 0xFFFF = -1
        assert v.decode(b"\xff\xff") == -1

    def test_decode_dint(self):
        v = S7Variable.parse("DB1.DInt0")
        assert v.decode(b"\x00\x00\x00\x01") == 1

    def test_decode_word(self):
        v = S7Variable.parse("DB1.Word0")
        # Unsigned 16-bit
        assert v.decode(b"\xff\xff") == 65535

    def test_decode_dword(self):
        v = S7Variable.parse("DB1.DWord0")
        assert v.decode(b"\x00\x00\x01\x00") == 256

    def test_decode_real(self):
        v = S7Variable.parse("DB1.Real0")
        raw = struct.pack(">f", 3.14)
        result = v.decode(raw)
        assert abs(result - 3.14) < 0.001

    def test_decode_bit_set(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.decode(b"\x08") is True  # bit 3 set

    def test_decode_bit_clear(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.decode(b"\x00") is False

    def test_decode_bit_other_bits_set(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.decode(b"\xf7") is False  # all except bit 3

    def test_decode_string(self):
        v = S7Variable.parse("DB1.String0.10")
        data = bytearray(12)
        data[0] = 10  # max length
        data[1] = 5  # actual length
        data[2:7] = b"Hello"
        assert v.decode(data) == "Hello"

    def test_decode_insufficient_data(self):
        v = S7Variable.parse("DB1.Int0")
        with pytest.raises(ValueError, match="Need 2 bytes"):
            v.decode(b"\x00")


class TestS7VariableEncode:
    def test_encode_byte(self):
        v = S7Variable.parse("DB1.Byte0")
        assert v.encode(42) == bytearray(b"\x2a")

    def test_encode_int(self):
        v = S7Variable.parse("DB1.Int0")
        assert v.encode(-1) == bytearray(b"\xff\xff")

    def test_encode_real(self):
        v = S7Variable.parse("DB1.Real0")
        result = v.encode(3.14)
        decoded = struct.unpack(">f", result)[0]
        assert abs(decoded - 3.14) < 0.001

    def test_encode_string(self):
        v = S7Variable.parse("DB1.String0.10")
        result = v.encode("Hi")
        assert result[0] == 10  # max length
        assert result[1] == 2  # actual length
        assert result[2:4] == b"Hi"

    def test_encode_bit_raises(self):
        v = S7Variable.parse("DB1.Bit0.3")
        with pytest.raises(ValueError, match="encode_bit"):
            v.encode(True)

    def test_encode_bit_set(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.encode_bit(0x00, True) == bytearray([0x08])

    def test_encode_bit_clear(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.encode_bit(0xFF, False) == bytearray([0xF7])

    def test_encode_bit_preserves_other_bits(self):
        v = S7Variable.parse("DB1.Bit0.3")
        assert v.encode_bit(0x05, True) == bytearray([0x0D])  # 0x05 | 0x08


class TestFormatAndParse:
    def test_format_bit(self):
        v = S7Variable.parse("DB1.Bit0.0")
        assert v.format_value(True) == "1"
        assert v.format_value(False) == "0"

    def test_format_real(self):
        v = S7Variable.parse("DB1.Real0")
        assert v.format_value(3.14159) == "3.1416"

    def test_format_int(self):
        v = S7Variable.parse("DB1.Int0")
        assert v.format_value(42) == "42"

    def test_parse_input_bit(self):
        v = S7Variable.parse("DB1.Bit0.0")
        assert v.parse_input("1") is True
        assert v.parse_input("true") is True
        assert v.parse_input("0") is False
        assert v.parse_input("false") is False

    def test_parse_input_hex(self):
        v = S7Variable.parse("DB1.Byte0")
        assert v.parse_input("0xFF") == 255

    def test_parse_input_int(self):
        v = S7Variable.parse("DB1.Int0")
        assert v.parse_input("42") == 42

    def test_parse_input_real(self):
        v = S7Variable.parse("DB1.Real0")
        assert abs(v.parse_input("3.14") - 3.14) < 0.001

    def test_parse_input_string(self):
        v = S7Variable.parse("DB1.String0.10")
        assert v.parse_input("hello") == "hello"

    def test_parse_input_invalid_bit(self):
        v = S7Variable.parse("DB1.Bit0.0")
        with pytest.raises(ValueError, match="Invalid bit value"):
            v.parse_input("maybe")


class TestComputeReadRange:
    def test_single_variable(self):
        v = S7Variable.parse("DB200.Byte5")
        start, size = compute_read_range([v])
        assert start == 5
        assert size == 1

    def test_multiple_variables(self):
        vars = [
            S7Variable.parse("DB200.Byte0"),
            S7Variable.parse("DB200.Int4"),
            S7Variable.parse("DB200.Real12"),
        ]
        start, size = compute_read_range(vars)
        assert start == 0
        assert size == 16  # 0..15 (Real12 is 4 bytes)

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="No variables"):
            compute_read_range([])

    def test_multiple_dbs_raises(self):
        vars = [
            S7Variable.parse("DB100.Byte0"),
            S7Variable.parse("DB200.Byte0"),
        ]
        with pytest.raises(ValueError, match="multiple DBs"):
            compute_read_range(vars)


class TestExtractValue:
    def test_extract_from_buffer(self):
        v = S7Variable.parse("DB200.Byte5")
        data = bytearray(10)
        data[5] = 42
        assert extract_value(v, data, data_start=0) == 42

    def test_extract_with_offset_start(self):
        v = S7Variable.parse("DB200.Byte5")
        data = bytearray([42, 0, 0])
        assert extract_value(v, data, data_start=5) == 42

    def test_extract_out_of_range(self):
        v = S7Variable.parse("DB200.Byte5")
        data = bytearray(3)
        with pytest.raises(ValueError, match="not within read range"):
            extract_value(v, data, data_start=0)
