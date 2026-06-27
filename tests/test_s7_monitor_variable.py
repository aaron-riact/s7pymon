import struct
import pytest

from s7pymon.variable import (
    ByteOrder,
    S7Area,
    DataType,
    S7Variable,
    EIPVariable,
    compute_read_range,
    extract_value,
)


class TestS7VariableParsing:
    def test_parse_byte(self):
        v = S7Variable.parse("DB200.Byte0")
        assert isinstance(v, S7Variable)
        assert v.db == 200
        assert v.type == DataType.BYTE
        assert v.offset == 0
        assert v.extra is None
        assert v.spec == "DB200.Byte0"

    def test_parse_int(self):
        v = S7Variable.parse("DB210.Int4")
        assert isinstance(v, S7Variable)
        assert v.db == 210
        assert v.type == DataType.INT
        assert v.offset == 4
        assert v.byte_size == 2

    def test_parse_dint(self):
        v = S7Variable.parse("DB100.DInt8")
        assert isinstance(v, S7Variable)
        assert v.db == 100
        assert v.type == DataType.DINT
        assert v.offset == 8
        assert v.byte_size == 4

    def test_parse_word(self):
        v = S7Variable.parse("DB5.Word2")
        assert isinstance(v, S7Variable)
        assert v.db == 5
        assert v.type == DataType.WORD
        assert v.offset == 2
        assert v.byte_size == 2

    def test_parse_dword(self):
        v = S7Variable.parse("DB1.DWord6")
        assert v.type == DataType.DWORD
        assert v.byte_size == 4

    def test_parse_real(self):
        v = S7Variable.parse("DB200.Real12")
        assert v.type == DataType.REAL
        assert v.offset == 12
        assert v.byte_size == 4

    def test_parse_bit(self):
        v = S7Variable.parse("DB200.Bit0.3")
        assert v.type == DataType.BIT
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
        assert v.type == DataType.STRING
        assert v.offset == 50
        assert v.extra == 20
        assert v.byte_size == 22  # 20 + 2 header bytes

    def test_parse_string_requires_length(self):
        with pytest.raises(ValueError, match="requires max length"):
            S7Variable.parse("DB200.String50")

    def test_parse_case_insensitive(self):
        v = S7Variable.parse("db200.byte0")
        assert isinstance(v, S7Variable)
        assert v.db == 200
        assert v.type == DataType.BYTE

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
        assert isinstance(result, float)
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
        result = v.parse_input("3.14")
        assert isinstance(result, float)
        assert abs(result - 3.14) < 0.001

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
        with pytest.raises(ValueError, match="multiple sources"):
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


class TestS7AreaParsing:
    def test_parse_eb(self):
        v = S7Variable.parse("EB.Byte0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.EB
        assert v.db == 0
        assert v.type == DataType.BYTE
        assert v.offset == 0
        assert v.spec == "EB.Byte0"

    def test_parse_ab(self):
        v = S7Variable.parse("AB.Byte2")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.AB
        assert v.spec == "AB.Byte2"

    def test_parse_mb(self):
        v = S7Variable.parse("MB.Byte0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.MB

    def test_parse_ct(self):
        v = S7Variable.parse("CT.Word0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.CT
        assert v.type == DataType.WORD

    def test_parse_tm(self):
        v = S7Variable.parse("TM.Word0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.TM

    def test_parse_eb_bit(self):
        v = S7Variable.parse("EB.Bit0.3")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.EB
        assert v.type == DataType.BIT
        assert v.extra == 3
        assert v.spec == "EB.Bit0.3"

    def test_parse_eb_case_insensitive(self):
        v = S7Variable.parse("eb.byte0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.EB

    def test_db_default_area(self):
        v = S7Variable.parse("DB200.Byte0")
        assert isinstance(v, S7Variable)
        assert v.area == S7Area.DB

    def test_area_with_label(self):
        v = S7Variable.parse("EB.Byte0", label="input0")
        assert v.label == "input0"
        assert v.display_name == "input0"

    def test_mixed_areas_in_compute_read_range_raises(self):
        vars = [
            S7Variable.parse("DB100.Byte0"),
            S7Variable.parse("EB.Byte0"),
        ]
        with pytest.raises(ValueError, match="multiple sources"):
            compute_read_range(vars)

    def test_same_area_compute_read_range(self):
        vars = [
            S7Variable.parse("EB.Byte0"),
            S7Variable.parse("EB.Byte4"),
        ]
        start, size = compute_read_range(vars)
        assert start == 0
        assert size == 5

    def test_area_description(self):
        assert S7Area.EB.description == "Process Input"
        assert S7Area.AB.description == "Process Output"
        assert S7Area.MB.description == "Merker/Flag"
        assert S7Area.DB.description == "Data Block"


class TestEIPVariableParsing:
    def test_parse_eip_input_byte(self):
        v = S7Variable.parse("EIP.Input.Byte0")
        assert isinstance(v, EIPVariable)
        assert v.assembly == "Input"
        assert v.type == DataType.BYTE
        assert v.offset == 0
        assert v.extra is None
        assert v.spec == "EIP.Input.Byte0"

    def test_parse_eip_output_int(self):
        v = S7Variable.parse("EIP.Output.Int4")
        assert isinstance(v, EIPVariable)
        assert v.assembly == "Output"
        assert v.type == DataType.INT
        assert v.offset == 4

    def test_parse_eip_input_bit(self):
        v = S7Variable.parse("EIP.Input.Bit2.3")
        assert isinstance(v, EIPVariable)
        assert v.assembly == "Input"
        assert v.type == DataType.BIT
        assert v.offset == 2
        assert v.extra == 3

    def test_parse_eip_input_real(self):
        v = S7Variable.parse("EIP.Input.Real100")
        assert isinstance(v, EIPVariable)
        assert v.type == DataType.REAL
        assert v.offset == 100

    def test_parse_eip_case_insensitive(self):
        v = S7Variable.parse("eip.input.byte0")
        assert isinstance(v, EIPVariable)
        assert v.assembly == "input"
        assert v.type == DataType.BYTE

    def test_parse_eip_with_label(self):
        v = S7Variable.parse("EIP.Input.Byte0", label="heartbeat")
        assert isinstance(v, EIPVariable)
        assert v.display_name == "heartbeat"
        assert v.spec == "EIP.Input.Byte0"

    def test_eip_source_property(self):
        v = S7Variable.parse("EIP.Input.Int4")
        assert str(v.source) == "EIP.Input"

    def test_eip_output_source_property(self):
        v = S7Variable.parse("EIP.Output.Byte0")
        assert str(v.source) == "EIP.Output"

    def test_eip_decode_encode_byte(self):
        v = S7Variable.parse("EIP.Input.Byte0")
        assert v.decode(b"\x2A") == 42
        assert v.encode(42) == bytearray(b"\x2A")

    def test_eip_decode_encode_int(self):
        v = S7Variable.parse("EIP.Input.Int4")
        assert v.decode(b"\x2A\x00") == 42
        assert v.encode(42) == bytearray(b"\x2A\x00")

    def test_eip_bit_read_modify_write(self):
        v = S7Variable.parse("EIP.Input.Bit0.3")
        assert v.decode(b"\x08") is True
        assert v.decode(b"\x00") is False
        assert v.encode_bit(0x00, True) == bytearray(b"\x08")
        assert v.encode_bit(0x08, False) == bytearray(b"\x00")

    def test_eip_format_value(self):
        v = S7Variable.parse("EIP.Input.Real8")
        assert v.format_value(3.1415) == "3.1415"
        assert v.format_value(True) == "1.0000"  # coerced to float

    def test_eip_parse_input(self):
        v = S7Variable.parse("EIP.Input.Byte0")
        assert v.parse_input("0xFF") == 255
        assert v.parse_input("42") == 42

    def test_eip_invalid_spec_raises(self):
        with pytest.raises(ValueError, match="Invalid variable spec"):
            S7Variable.parse("EIP.Invalid.Byte0")

    def test_eip_missing_bit_number_raises(self):
        with pytest.raises(ValueError, match="requires bit number"):
            S7Variable.parse("EIP.Input.Bit0")

    def test_parse_chars(self):
        v = S7Variable.parse("DB200.Chars50.20")
        assert v.type == DataType.CHARS
        assert v.offset == 50
        assert v.extra == 20
        assert v.byte_size == 20  # no header bytes

    def test_parse_chars_requires_length(self):
        with pytest.raises(ValueError, match="requires max length"):
            S7Variable.parse("DB200.Chars50")

    def test_eip_parse_chars(self):
        v = S7Variable.parse("EIP.Input.Chars8.32")
        assert v.type == DataType.CHARS
        assert v.offset == 8
        assert v.extra == 32
        assert v.byte_size == 32
        assert v.spec == "EIP.Input.Chars8.32"

    def test_eip_chars_decode_trailing_nulls(self):
        v = S7Variable.parse("EIP.Input.Chars0.10")
        data = b"Hello\x00\x00\x00\x00\x00"
        assert v.decode(data) == "Hello"

    def test_eip_chars_decode_no_trailing_nulls(self):
        v = S7Variable.parse("EIP.Input.Chars0.5")
        assert v.decode(b"World") == "World"

    def test_eip_chars_decode_all_nulls(self):
        v = S7Variable.parse("EIP.Input.Chars0.10")
        assert v.decode(b"\x00" * 10) == ""

    def test_eip_chars_encode_pads_nulls(self):
        v = S7Variable.parse("EIP.Input.Chars0.8")
        assert v.encode("Hi") == bytearray(b"Hi\x00\x00\x00\x00\x00\x00")

    def test_eip_chars_encode_truncates(self):
        v = S7Variable.parse("EIP.Input.Chars0.4")
        assert v.encode("Hello") == bytearray(b"Hell")

    def test_eip_chars_decode_encode_roundtrip(self):
        v = S7Variable.parse("EIP.Input.Chars0.10")
        assert v.decode(v.encode("Test")) == "Test"

    def test_eip_chars_format_value(self):
        v = S7Variable.parse("EIP.Input.Chars0.10")
        assert v.format_value("hello") == "'hello'"

    def test_eip_parse_word_bit_hex(self):
        v = S7Variable.parse("EIP.Input.Word3.f")
        assert v.type == DataType.WORD
        assert v.offset == 3
        assert v.extra == 15
        assert v.byte_size == 2

    def test_eip_parse_word_bit_decimal(self):
        v = S7Variable.parse("EIP.Input.Word3.15")
        assert v.type == DataType.WORD
        assert v.extra == 15

    def test_eip_parse_dword_bit(self):
        v = S7Variable.parse("EIP.Input.DWord4.1f")
        assert v.type == DataType.DWORD
        assert v.offset == 4
        assert v.extra == 31
        assert v.byte_size == 4

    def test_eip_parse_word_bit_zero(self):
        v = S7Variable.parse("EIP.Input.Word0.0")
        assert v.type == DataType.WORD
        assert v.extra == 0

    def test_eip_word_bit_decode_set(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        assert v.decode(b"\xFF\xFF") is True
        assert v.decode(b"\x00\x80") is True  # bit 15 set (LE: mask=0x8000)

    def test_eip_word_bit_decode_clear(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        assert v.decode(b"\x00\x00") is False
        assert v.decode(b"\xFF\x7F") is False  # bit 15 clear (LE: mask=0x7FFF)

    def test_eip_dword_bit_decode(self):
        v = S7Variable.parse("EIP.Input.DWord0.1f")
        assert v.decode(b"\x00\x00\x00\x80") is True   # bit 31 set (LE: mask=0x80000000)
        assert v.decode(b"\x00\x00\x00\x00") is False   # all clear

    def test_eip_word_bit_out_of_range_raises(self):
        with pytest.raises(ValueError, match="must be 0-15"):
            S7Variable.parse("EIP.Input.Word0.1f")

    def test_eip_dword_bit_out_of_range_raises(self):
        with pytest.raises(ValueError, match="must be 0-31"):
            S7Variable.parse("EIP.Input.DWord0.2a")

    def test_eip_word_bit_format(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        assert v.format_value(True) == "1"
        assert v.format_value(False) == "0"

    def test_eip_word_bit_parse_input(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        assert v.parse_input("1") is True
        assert v.parse_input("true") is True
        assert v.parse_input("0") is False
        assert v.parse_input("false") is False

    def test_eip_word_bit_parse_input_invalid_raises(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        with pytest.raises(ValueError, match="Invalid bit value"):
            v.parse_input("xyz")

    def test_eip_word_bit_encode_raises(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        with pytest.raises(ValueError, match="Cannot encode whole register"):
            v.encode(42)

    def test_db_parse_word_bit(self):
        v = S7Variable.parse("DB1.Word2.1")
        assert v.type == DataType.WORD
        assert v.extra == 1

    def test_area_parse_word_bit(self):
        v = S7Variable.parse("AB.Word2.1")
        assert v.type == DataType.WORD
        assert v.extra == 1

    def test_eip_word_spec_shows_decimal(self):
        v = S7Variable.parse("EIP.Input.Word0.f")
        assert v.spec == "EIP.Input.Word0.15"

    def test_eip_word_le_suffix(self):
        v = S7Variable.parse("EIP.Input.Word0.le")
        assert v.byte_order == ByteOrder.LITTLE

    def test_eip_word_be_suffix_override(self):
        v = S7Variable.parse("EIP.Input.Word0.be")
        assert v.byte_order == ByteOrder.BIG

    def test_eip_word_little_suffix(self):
        v = S7Variable.parse("EIP.Input.Word0.little")
        assert v.byte_order == ByteOrder.LITTLE

    def test_eip_word_big_suffix(self):
        v = S7Variable.parse("EIP.Input.Word0.big")
        assert v.byte_order == ByteOrder.BIG

    def test_eip_word_bit_with_le_suffix(self):
        v = S7Variable.parse("EIP.Input.Word0.f.le")
        assert v.extra == 15
        assert v.byte_order == ByteOrder.LITTLE

    def test_db_word_be_suffix(self):
        v = S7Variable.parse("DB1.Word0.be")
        assert v.byte_order == ByteOrder.BIG

    def test_db_word_le_suffix_override(self):
        v = S7Variable.parse("DB1.Word0.le")
        assert v.byte_order == ByteOrder.LITTLE

    def test_area_word_be_suffix(self):
        v = S7Variable.parse("AB.Word0.be")
        assert v.byte_order == ByteOrder.BIG

    def test_eip_byte_ignores_suffix(self):
        v = S7Variable.parse("EIP.Input.Byte0.le")
        assert v.type == DataType.BYTE
        assert v.byte_order == ByteOrder.LITTLE

    def test_eip_word_encode_be_decodes_le(self):
        v_be = S7Variable.parse("EIP.Input.Word0.be")
        v_le = S7Variable.parse("EIP.Input.Word0.le")
        data = b"\x01\x02"
        assert v_be.decode(data) == 0x0102
        assert v_le.decode(data) == 0x0201

    def test_offset_display_bit_shows_extra(self):
        v = S7Variable.parse("EIP.Input.Bit0.3")
        assert v.offset_display == "0.3"

    def test_offset_display_word_bit_shows_extra(self):
        v = S7Variable.parse("EIP.Input.Word4.f")
        assert v.offset_display == "4.15"

    def test_offset_display_word_no_bit_no_extra(self):
        v = S7Variable.parse("EIP.Input.Word4")
        assert v.offset_display == "4"

    def test_offset_display_string_no_extra(self):
        v = S7Variable.parse("DB1.String0.10")
        assert v.offset_display == "0"

    def test_offset_display_chars_no_extra(self):
        v = S7Variable.parse("DB1.Chars0.10")
        assert v.offset_display == "0"

    def test_offset_display_dbit_shows_extra(self):
        v = S7Variable.parse("DB1.DWord8.1f")
        assert v.offset_display == "8.31"

    def test_offset_display_byte_no_extra(self):
        v = S7Variable.parse("EIP.Input.Byte0")
        assert v.offset_display == "0"
