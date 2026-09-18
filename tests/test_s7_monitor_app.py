"""Tests for the S7 Monitor TUI app components."""

import asyncio
import threading

import pytest
from rich.text import Text
from textual.widgets import DataTable

from busfactor.app import HexDumpDisplay, S7MonitorApp, format_hex_dump
from busfactor.engine import ReadGroup, WriteMode
from busfactor.protocols import ConnectionState, DataSource
from busfactor.variable import S7Area, DataType, S7Variable
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


class TestHexSelection:
    """HexDumpDisplay highlights selected variable's byte range."""

    @pytest.fixture
    def app(self):
        conn = BaseFakeConnection()
        variables = [
            S7Variable.parse("DB1.Byte0", label="b0"),
            S7Variable.parse("DB1.Byte2", label="b2"),
        ]
        groups = [ReadGroup(DataSource.s7_db(1), start=0, size=4)]
        return S7MonitorApp(
            connection=conn,
            variables=variables,
            read_groups=groups,
            poll_interval=3600,
            write_mode=WriteMode.ALLOWED,
        )

    def test_set_selected_offsets(self):
        """set_selected_offsets stores offsets per group."""
        hd = HexDumpDisplay()
        hd.set_selected_offsets("DB1", {1, 2, 3})
        assert hd._selected_abs_offsets == {"DB1": {1, 2, 3}}

    def test_selected_bytes_in_render(self):
        """Selected bytes appear in rendered output."""
        hd = HexDumpDisplay()
        data = bytearray([0x41, 0x42, 0x43, 0x44])
        hd.set_selected_offsets("DB1", {2, 3})
        hd.set_data([("DB1", data, 0)])
        rendered = hd.render()
        assert "43 44" in rendered.plain

    def test_row_highlight_sets_offsets(self, app):
        """on_data_table_row_highlighted computes byte range and sets it."""
        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump", HexDumpDisplay)
                table = app.query_one("#var-table-output")
                table.focus()
                await pilot.pause()
                # row 0 = DB1.Byte0 (offset 0, byte_size=1) — only sets when data exists
                app._current_data = {"DB1": (bytearray([0xAA, 0xBB, 0xCC, 0xDD]), 0)}
                row_key = app._row_keys.get(id(app._variables[0]))
                # Simulate row highlight
                from textual.widgets.data_table import RowKey
                app.on_data_table_row_highlighted(
                    DataTable.RowHighlighted(table, cursor_row=0, row_key=RowKey(row_key))
                )
                assert hex_dump._selected_abs_offsets == {"DB1": {0}}  # Byte0, 1 byte

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
        """Changed bytes have flash keys and render shows the byte."""
        hd = HexDumpDisplay()
        data = bytearray([0x41, 0x42, 0x43])
        hd.set_data([("DB1", data, 0)], changed_per_group={"DB1": {1}})
        assert ("DB1", 1) in hd._changed_flash_keys
        assert ("DB1", 0) not in hd._changed_flash_keys
        rendered = hd.render()
        assert "42" in rendered.plain  # changed byte value shown

    def test_data_and_flash_in_same_frame(self):
        """set_data updates data bytes and flash highlight atomically — the
        rendered line has both the new byte value and the flash style
        simultaneously."""
        hd = HexDumpDisplay()
        # Initial data: bytes [0x41, 0x42, 0x43] at offset 0
        hd.set_data([("DB1", bytearray([0x41, 0x42, 0x43]), 0)])
        # Changed data: byte at offset 1 changed from 0x42 → 0xEE, others unchanged
        hd.set_data([("DB1", bytearray([0x41, 0xEE, 0x43]), 0)], changed_per_group={"DB1": {1}})

        # Line 0 = separator, line 1 = hex data for 3 bytes at offset 0
        strip = hd.render_line(1)
        # Find the segment for the changed byte "EE"
        changed_segs = [s for s in strip._segments if "EE" in s.text]
        assert changed_segs, "Changed byte 'EE' should be present in rendered output"
        seg = changed_segs[0]
        assert seg.style is not None
        style_str = str(seg.style)
        assert "ff8800" in style_str, f"Expected flash style 'ff8800' in {style_str!r}"

        # Bytes that didn't change should NOT have flash style
        unchanged_segs = [s for s in strip._segments if s.text.strip() == "41"]
        for s in unchanged_segs:
            if s.style is not None:
                assert "ff8800" not in str(s.style)

    def test_two_groups_no_cross_group_flash(self):
        """Changing one group must not flash-style bytes in the other group."""
        hd = HexDumpDisplay()
        hd.set_data([
            ("Input", bytearray(range(16)), 0),
            ("Output", bytearray(range(16, 32)), 16),
        ])
        # Change only Output byte 1 (abs offset 17)
        hd.set_data([
            ("Input", bytearray(range(16)), 0),
            ("Output", bytearray([0x10, 0xFF, *range(18, 32)]), 16),
        ], changed_per_group={"Output": {17}})

        # Output data at render_line(3) (sep=0, Input=1, sep=2, Output=3)
        out = hd.render_line(3)
        ff = [s for s in out._segments if "FF" in s.text]
        assert ff, "Output byte FF should be in rendered output"
        assert ff[0].style is not None
        assert "ff8800" in str(ff[0].style)

        # Input line (render_line 1) must NOT have any flash style
        inp = hd.render_line(1)
        for seg in inp._segments:
            if seg.style is not None and "ff8800" in str(seg.style):
                assert False, f"Input line has flash on {seg.text!r}"

        # _lines_for_offsets must return only Output lines for Output byte 17
        line_indices = hd._lines_for_offsets({("Output", 17)})
        assert line_indices == {3}

    def test_flash_detected_in_on_data_received(self):
        """_on_data_received detects changed bytes across poll cycles."""
        grp = ReadGroup(DataSource.s7_db(1), start=0, size=4)
        variables = [S7Variable.parse("DB1.Byte0", label="b0")]
        app = S7MonitorApp(connection=BaseFakeConnection(), variables=variables, read_groups=[grp], poll_interval=3600)

        async def run():
            async with app.run_test() as pilot:
                hex_dump = app.query_one("#hex-dump", HexDumpDisplay)
                # First poll — no previous data, so no flash
                app._on_data_received({"DB1": (bytearray([0xAA, 0xBB, 0xCC, 0xDD]), 0)}, {"DB1": set()})
                assert len(hex_dump._changed_flash_keys) == 0
                # Second poll — byte 1 changed (0xBB → 0xEE)
                app._on_data_received({"DB1": (bytearray([0xAA, 0xEE, 0xCC, 0xDD]), 0)}, {"DB1": {1}})
                assert ("DB1", 1) in hex_dump._changed_flash_keys
                assert ("DB1", 0) not in hex_dump._changed_flash_keys
                assert ("DB1", 2) not in hex_dump._changed_flash_keys

        asyncio.run(run())


class TestVarSide:
    """_var_side classifies variables by source."""

    def test_input_eb(self):
        var = S7Variable.parse("EB.Byte0")
        assert S7MonitorApp._var_side(var) == "input"

    def test_input_eip(self):
        var = S7Variable.parse("EIP.Input.Byte0")
        assert S7MonitorApp._var_side(var) == "input"

    def test_output_ab(self):
        var = S7Variable.parse("AB.Byte0")
        assert S7MonitorApp._var_side(var) == "output"

    def test_output_eip(self):
        var = S7Variable.parse("EIP.Output.Byte0")
        assert S7MonitorApp._var_side(var) == "output"

    def test_output_db(self):
        """DB variables go to output (writable) side."""
        var = S7Variable.parse("DB1.Byte0")
        assert S7MonitorApp._var_side(var) == "output"

    def test_output_mb(self):
        var = S7Variable.parse("MB.Byte0")
        assert S7MonitorApp._var_side(var) == "output"


class TestTwoTableRouting:
    """Variables are split across input and output tables."""

    @pytest.fixture
    def app(self):
        conn = BaseFakeConnection()
        variables = [
            S7Variable.parse("EIP.Input.Byte0", label="in0"),
            S7Variable.parse("EIP.Input.Byte1", label="in1"),
            S7Variable.parse("EIP.Output.Bit0.0", label="out_bit"),
            S7Variable.parse("DB1.Byte0", label="db0"),
        ]
        groups = [
            ReadGroup(DataSource.s7_db(1), start=0, size=2),
        ]
        return S7MonitorApp(
            connection=conn,
            variables=variables,
            read_groups=groups,
            poll_interval=3600,
        )

    def test_input_table_populated(self, app):
        """Input variables go to var-table-input."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table-input", DataTable)
                assert table.row_count == 2

        asyncio.run(run())

    def test_output_table_populated(self, app):
        """Output variables go to var-table-output."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table-output", DataTable)
                assert table.row_count == 2

        asyncio.run(run())

    def test_focused_table(self, app):
        """_focused_table returns the currently focused table."""
        async def run():
            async with app.run_test() as pilot:
                # Default focus is on var-table-input (first DataTable)
                output_table = app.query_one("#var-table-output", DataTable)
                output_table.focus()
                await pilot.pause()
                assert app._focused_table() is output_table

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
                table = app.query_one("#var-table-output")
                table.focus()
                await pilot.pause()
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
                table = app.query_one("#var-table-output")
                table.focus()
                await pilot.pause()
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
                table = app.query_one("#var-table-output")
                table.focus()
                await pilot.pause()
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
                table = app.query_one("#var-table-output")
                table.focus()
                await pilot.pause()
                table.move_cursor(row=0)
                await pilot.pause()
                app.action_edit_variable()
                await pilot.pause()
                assert app.screen is not app  # edit screen pushed

        asyncio.run(run())

    def test_first_poll_populates_value(self, app):
        """First _on_data_received populates cells (not '—')."""
        async def run():
            async with app.run_test() as pilot:
                table = app.query_one("#var-table-output", DataTable)
                app._on_data_received({"DB1": (bytearray([0x01, 0x00]), 0)}, {"DB1": set()})
                await pilot.pause()
                row_key = app._row_keys.get(id(app._variables[0]))
                cell = table.get_cell(row_key, app.COL_VALUE)
                assert cell != "—"

        asyncio.run(run())

    def test_flash_clears_on_stable_value(self, app):
        """Flash fades over FLASH_DURATION cycles like the hex dump."""
        async def run():
            async with app.run_test() as pilot:
                # Call 1: populate initial value
                app._on_data_received({"DB1": (bytearray([0x00, 0x01]), 0)}, {"DB1": set()})
                await pilot.pause()

                table = app.query_one("#var-table-output", DataTable)
                row_key = app._row_keys.get(id(app._variables[1]))  # DB1.Byte1

                # Call 2: change value → flash applied
                app._on_data_received({"DB1": (bytearray([0x00, 0x02]), 0)}, {"DB1": {1}})
                await pilot.pause()
                cell = table.get_cell(row_key, app.COL_VALUE)
                assert isinstance(cell, Text)

                # Calls 3..N: unchanged value — flash persists for FLASH_DURATION cycles
                from busfactor.app import HexDumpDisplay
                for _ in range(HexDumpDisplay.FLASH_DURATION):
                    app._on_data_received({"DB1": (bytearray([0x00, 0x02]), 0)}, {"DB1": set()})
                    await pilot.pause()

                # After FLASH_DURATION unchanged cycles, flash must be cleared
                cell2 = table.get_cell(row_key, app.COL_VALUE)
                assert not isinstance(cell2, Text)

        asyncio.run(run())


class TestPollOverrun:
    """Poll ticks that arrive during a read are dropped, not queued."""

    @staticmethod
    def _blocking_app(release: threading.Event):
        class BlockingConnection(BaseFakeConnection):
            reads = 0

            def read_source(self, source: DataSource, offset: int, size: int):
                BlockingConnection.reads += 1
                release.wait(5)
                return super().read_source(source, offset, size)

        conn = BlockingConnection()
        app = S7MonitorApp(
            connection=conn,
            variables=[S7Variable.parse("DB1.Byte0", label="b0")],
            read_groups=[ReadGroup(DataSource.s7_db(1), start=0, size=2)],
            poll_interval=3600,
        )
        return app, conn

    @staticmethod
    async def _wait_for_read(conn, count: int) -> None:
        for _ in range(500):
            if type(conn).reads >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"read {count} never started")

    def test_tick_during_read_starts_no_second_read(self):
        release = threading.Event()
        app, conn = self._blocking_app(release)

        async def run():
            async with app.run_test() as pilot:
                app._poll_tick()
                await self._wait_for_read(conn, 1)
                app._poll_tick()
                app._poll_tick()
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert type(conn).reads == 1
                assert not app._read_in_flight

        asyncio.run(run())

    def test_next_tick_reads_once_the_last_one_finished(self):
        release = threading.Event()
        release.set()
        app, conn = self._blocking_app(release)

        async def run():
            async with app.run_test() as pilot:
                app._poll_tick()
                await app.workers.wait_for_complete()
                await pilot.pause()
                app._poll_tick()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert type(conn).reads == 2

        asyncio.run(run())

    def test_disconnected_tick_leaves_the_flag_clear(self):
        """The early return in _do_read must not wedge polling off."""
        release = threading.Event()
        release.set()
        app, conn = self._blocking_app(release)

        async def run():
            async with app.run_test() as pilot:
                conn.state = ConnectionState.DISCONNECTED
                app._poll_tick()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert type(conn).reads == 0
                assert not app._read_in_flight

        asyncio.run(run())
