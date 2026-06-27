"""Tests for the S7 Monitor TUI app components."""

import asyncio

import pytest

from s7pymon.app import HexDumpDisplay, S7MonitorApp, format_hex_dump
from s7pymon.engine import ReadGroup, WriteMode
from s7pymon.protocols import DataSource
from s7pymon.variable import S7Area, DataType, S7Variable
from tests.fakes import BaseFakeConnection


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


class TestHexCollapse:
    """HexDumpDisplay collapsible toggle."""

    @pytest.fixture
    def app(self):
        conn = BaseFakeConnection()
        variables = [S7Variable.parse("DB1.Byte0", label="b0")]
        groups = [ReadGroup(DataSource.s7_db(1), start=0, size=2)]
        return S7MonitorApp(connection=conn, variables=variables, read_groups=groups, poll_interval=3600)

    def test_starts_expanded(self, app):
        """Hex dump starts expanded (not collapsed)."""
        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump")
                assert not hex_dump.collapsed

        asyncio.run(run())

    def test_h_toggles_collapse(self, app):
        """Pressing h toggles hex dump collapse."""
        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump")
                assert not hex_dump.collapsed
                await pilot.press("h")
                await pilot.pause()
                assert hex_dump.collapsed
                await pilot.press("h")
                await pilot.pause()
                assert not hex_dump.collapsed

        asyncio.run(run())

    def test_collapsed_render_shows_hint(self, app):
        """Collapsed hex dump shows collapse hint text."""
        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump")
                hex_dump.collapsed = True
                rendered = hex_dump.render()
                assert "press h" in rendered.plain.lower()

        asyncio.run(run())


class TestHexFlash:
    """HexDumpDisplay byte-level flash on change."""

    def test_set_data_empty(self):
        """set_data with no groups renders 'No data yet'."""
        hd = HexDumpDisplay()
        hd.set_data([])
        rendered = hd.render()
        assert "No data yet" in rendered.plain

    def test_set_data_basic(self):
        """set_data with one group renders hex content."""
        hd = HexDumpDisplay()
        data = bytearray([0x41, 0x42, 0x43])
        hd.set_data([("DB1", data, 0)])
        rendered = hd.render()
        assert "41 42 43" in rendered.plain

    def test_changed_bytes_highlighted(self):
        """Changed bytes have _changed_abs_offsets and render shows the byte."""
        hd = HexDumpDisplay()
        data = bytearray([0x41, 0x42, 0x43])
        hd.set_data([("DB1", data, 0)], changed_abs_offsets={1})
        assert 1 in hd._changed_abs_offsets
        assert 0 not in hd._changed_abs_offsets
        rendered = hd.render()
        assert "42" in rendered.plain  # changed byte value shown

    def test_flash_detected_in_on_data_received(self):
        """_on_data_received detects changed bytes across poll cycles."""
        grp = ReadGroup(DataSource.s7_db(1), start=0, size=4)
        variables = [S7Variable.parse("DB1.Byte0", label="b0")]
        app = S7MonitorApp(connection=BaseFakeConnection(), variables=variables, read_groups=[grp], poll_interval=3600)

        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump", HexDumpDisplay)
                # First poll — no previous data, so no flash
                app._on_data_received({"DB1": (bytearray([0xAA, 0xBB, 0xCC, 0xDD]), 0)})
                assert len(hex_dump._changed_abs_offsets) == 0
                # Second poll — byte 1 changed (0xBB → 0xEE)
                app._on_data_received({"DB1": (bytearray([0xAA, 0xEE, 0xCC, 0xDD]), 0)})
                assert 1 in hex_dump._changed_abs_offsets
                assert 0 not in hex_dump._changed_abs_offsets
                assert 2 not in hex_dump._changed_abs_offsets

        asyncio.run(run())


class TestRowKeyLookup:
    """_row_key_to_var dict is populated and used by toggle/edit actions."""

    @pytest.fixture
    def app(self):
        conn = BaseFakeConnection()
        variables = [
            S7Variable.parse("DB1.Bit0.0", label="bit0"),
            S7Variable.parse("DB1.Byte1", label="byte1"),
            S7Variable.parse("EIP.Output.Bit0.0", label="eip_bit"),
        ]
        groups = [ReadGroup(DataSource.s7_db(1), start=0, size=2)]
        return S7MonitorApp(
            connection=conn,
            variables=variables,
            read_groups=groups,
            poll_interval=3600,
            write_mode=WriteMode.ALLOWED,
        )

    def test_row_key_to_var_populated(self, app):
        """_row_key_to_var maps every row key to its variable."""
        async def run():
            async with app.run_test() as pilot:
                assert len(app._row_key_to_var) == 3
                for var in app._variables:
                    row_key = app._row_keys.get(id(var))
                    assert row_key is not None
                    assert app._row_key_to_var[row_key] is var

        asyncio.run(run())

    def test_toggle_bit_finds_s7_bit(self, app):
        """action_toggle_bit finds an S7 Bit variable via _row_key_to_var."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table")
                table.move_cursor(row=0)
                await pilot.pause()
                app.action_toggle_bit()
                await pilot.pause()
                assert len(app._connection.writes) == 1

        asyncio.run(run())

    def test_toggle_bit_finds_eip_bit(self, app):
        """action_toggle_bit finds an EIP Bit variable via _row_key_to_var."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table")
                table.move_cursor(row=2)
                await pilot.pause()
                app.action_toggle_bit()
                await pilot.pause()
                assert len(app._connection.writes) == 1

        asyncio.run(run())

    def test_toggle_bit_skips_non_bit(self, app):
        """action_toggle_bit skips non-Bit variables."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table")
                table.move_cursor(row=1)
                await pilot.pause()
                app.action_toggle_bit()
                await pilot.pause()
                assert len(app._connection.writes) == 0

        asyncio.run(run())

    def test_edit_variable_finds_var(self, app):
        """action_edit_variable finds the variable via _row_key_to_var."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table")
                table.move_cursor(row=0)
                await pilot.pause()
                app.action_edit_variable()
                await pilot.pause()
                assert app.screen is not app  # edit screen pushed

        asyncio.run(run())
