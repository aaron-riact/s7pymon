#!/usr/bin/env python3
"""CLI entry point for s7pymon — S7 PLC Monitor TUI.

Usage:
    s7pymon <ip> [variables...] [OPTIONS]

Examples:
    # Monitor specific DB variables
    s7pymon 192.168.1.100 DB210.Byte0 DB210.Byte1 DB210.Int4

    # Monitor a raw DB range
    s7pymon 192.168.1.100 --db 210 --start 0 --size 18

    # With named variables
    s7pymon 192.168.1.100 DB210.Byte0:heartbeat DB210.Byte1:status DB210.Bit1.0:e_stop

    # Monitor DB and process inputs simultaneously
    s7pymon 192.168.1.100 DB210.Byte0 EB.Byte0 EB.Byte1

    # Monitor process outputs and merkers
    s7pymon 192.168.1.100 AB.Byte0:output0 MB.Byte0:flag0

    # Custom connection settings
    s7pymon 192.168.1.100 --rack 0 --slot 2 --port 1102 DB210.Byte0

    # Fast polling
    s7pymon 192.168.1.100 --interval 0.25 DB210.Byte0 DB210.Byte1
"""

import sys
from collections import defaultdict

import click

from .connection import ConnectionConfig, S7Connection
from .variable import S7Area, S7Type, S7Variable, compute_read_range


def parse_variable_arg(arg: str) -> S7Variable:
    """Parse a CLI variable argument, supporting optional label syntax.

    Formats:
        DB200.Byte0           -> DB variable with no label
        DB200.Byte0:heartbeat -> DB variable with label "heartbeat"
        EB.Byte0:input0       -> process input variable with label
    """
    if ":" in arg:
        spec, label = arg.split(":", 1)
        return S7Variable.parse(spec, label=label)
    return S7Variable.parse(arg)


def build_default_variables(db: int, start: int, size: int) -> list[S7Variable]:
    """Build a default set of Byte variables covering the entire DB range."""
    return [
        S7Variable(db=db, type=S7Type.BYTE, offset=start + i, label=f"byte_{i}")
        for i in range(size)
    ]


def build_read_groups(variables: list[S7Variable]):
    """Group variables by area+db and compute read ranges for each group.

    Returns list of ReadGroup (imported lazily from app).
    """
    from .app import ReadGroup

    # Group by (area, db)
    groups: dict[tuple[S7Area, int], list[S7Variable]] = defaultdict(list)
    for var in variables:
        groups[(var.area, var.db)].append(var)

    read_groups = []
    for (area, db), group_vars in groups.items():
        start, size = compute_read_range(group_vars)
        read_groups.append(ReadGroup(area=area, db=db, start=start, size=size))

    return read_groups


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("address")
@click.argument("variables", nargs=-1)
@click.option("-r", "--rack", default=0, help="Rack number of S7 instance.")
@click.option("-s", "--slot", default=2, help="Slot number of S7 instance.")
@click.option("-p", "--port", default=102, help="TCP port for S7 communication.")
@click.option("-t", "--timeout", default=3000, help="Connection timeout in ms.")
@click.option("-i", "--interval", default=1.0, help="Poll interval in seconds.")
@click.option("--db", "db_number", default=None, type=int, help="DB number for raw range mode.")
@click.option("--start", "db_start", default=0, type=int, help="Start offset for raw range mode.")
@click.option("--size", "db_size", default=None, type=int, help="Number of bytes for raw range mode.")
@click.option(
    "-w",
    "--write-mode",
    "write_mode",
    type=click.Choice(["disabled", "confirm", "allowed"], case_sensitive=False),
    default="disabled",
    help="Write permission mode (default: disabled).",
)
def main(
    address: str,
    variables: tuple[str, ...],
    rack: int,
    slot: int,
    port: int,
    timeout: int,
    interval: float,
    db_number: int | None,
    db_start: int,
    db_size: int | None,
    write_mode: str,
) -> None:
    """s7pymon — Live S7 PLC data monitor.

    ADDRESS is the IP address of the S7 PLC.

    VARIABLES are variable specs. Append :label to name them.

    \b
    Supported areas and types:
      DB<n>.Type<offset>  — Data Block (DB210.Byte0, DB210.Int4)
      EB.Type<offset>     — Process Image Input  (EB.Byte0, EB.Bit0.3)
      AB.Type<offset>     — Process Image Output (AB.Byte0)
      MB.Type<offset>     — Merkers / Flags      (MB.Byte100)
      CT.Type<offset>     — Counters             (CT.Word0)
      TM.Type<offset>     — Timers               (TM.Word0)

    \b
    Types: Byte, Int, DInt, Word, DWord, Real, Bit, String

    \b
    Keyboard shortcuts in the TUI:
      e       Edit selected variable (with confirmation)
      Space   Toggle bit variable (with confirmation)
      :       Command bar (write/set/read, with confirmation)
      r       Force refresh
      p       Pause/resume polling
      c       Reconnect
      q       Quit
    """
    from .app import S7MonitorApp, WriteMode

    wm = WriteMode(write_mode.lower())

    config = ConnectionConfig(
        address=address,
        rack=rack,
        slot=slot,
        tcp_port=port,
        timeout_ms=timeout,
    )
    connection = S7Connection(config)

    if variables:
        parsed_vars = []
        for v in variables:
            try:
                parsed_vars.append(parse_variable_arg(v))
            except ValueError as e:
                click.echo(f"Error parsing variable '{v}': {e}", err=True)
                sys.exit(1)

        # If explicit --db/--size given, extend the DB read range
        if db_number is not None:
            db_vars = [v for v in parsed_vars if v.area == S7Area.DB]
            db_dbs = {v.db for v in db_vars}
            if db_dbs and db_number not in db_dbs:
                click.echo(f"Error: --db {db_number} conflicts with variable DBs {db_dbs}", err=True)
                sys.exit(1)

        read_groups = build_read_groups(parsed_vars)

        # Extend DB read range if --size specified
        if db_size is not None:
            for group in read_groups:
                if group.area == S7Area.DB and (db_number is None or group.db == db_number):
                    group.size = max(group.size, db_size)
                    group.start = min(group.start, db_start)

    elif db_number is not None and db_size is not None:
        parsed_vars = build_default_variables(db_number, db_start, db_size)
        read_groups = build_read_groups(parsed_vars)
    else:
        click.echo("Error: Provide variable specs or --db and --size for raw range mode.", err=True)
        click.echo("Try: s7pymon --help", err=True)
        sys.exit(1)

    app = S7MonitorApp(
        connection=connection,
        variables=parsed_vars,
        read_groups=read_groups,
        poll_interval=interval,
        write_mode=wm,
    )
    app.run()


def cli():
    """Entry point wrapper for setuptools console_scripts."""
    main()


if __name__ == "__main__":
    cli()
