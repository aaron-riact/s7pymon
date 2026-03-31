"""Tests for the S7 Monitor TUI app components."""

import pytest

from s7pymon.app import format_hex_dump


class TestFormatHexDump:
    def test_empty_data(self):
        result = format_hex_dump(bytearray())
        assert result == ""

    def test_single_byte(self):
        result = format_hex_dump(bytearray([0x42]))
        assert "0000" in result
        assert "42" in result

    def test_full_line(self):
        data = bytearray(range(16))
        result = format_hex_dump(data)
        assert "0000" in result
        assert "00 01 02 03 04 05 06 07" in result
        assert "08 09 0A 0B 0C 0D 0E 0F" in result

    def test_multiple_lines(self):
        data = bytearray(range(32))
        result = format_hex_dump(data)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "0000" in lines[0]
        assert "0010" in lines[1]

    def test_with_start_offset(self):
        data = bytearray([0xFF])
        result = format_hex_dump(data, start_offset=0x100)
        assert "0100" in result

    def test_ascii_printable(self):
        data = bytearray(b"Hello World!!!!!")
        result = format_hex_dump(data)
        assert "Hello World!!!!!" in result

    def test_ascii_non_printable(self):
        data = bytearray([0x00, 0x01, 0x02])
        result = format_hex_dump(data)
        assert "···" in result

    def test_18_bytes_like_jakob_db(self):
        """Test with the same size as the Jakob S7 DB (18 bytes)."""
        data = bytearray(
            [0x01, 0x09, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00,
             0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00,
             0x00, 0x00]
        )
        result = format_hex_dump(data)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "01 09 00 04 00 00 00 00" in lines[0]
        assert "00 00" in lines[1]
