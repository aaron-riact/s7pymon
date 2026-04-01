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

from .config import S7MonitorConfig
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
@click.argument("address", required=False, default=None)
@click.argument("variables", nargs=-1)
@click.option("-c", "--config", "config_file", default=None, type=click.Path(), help="YAML config file.")
@click.option("-r", "--rack", default=None, type=int, help="Rack number (default: 0).")
@click.option("-s", "--slot", default=None, type=int, help="Slot number (default: 2).")
@click.option("-p", "--port", default=None, type=int, help="TCP port (default: 102).")
@click.option("-t", "--timeout", default=None, type=int, help="Connection timeout in ms (default: 3000).")
@click.option("-i", "--interval", default=None, type=float, help="Poll interval in seconds (default: 1.0).")
@click.option("--db", "db_number", default=None, type=int, help="DB number for raw range mode.")
@click.option("--start", "db_start", default=None, type=int, help="Start offset for raw range mode.")
@click.option("--size", "db_size", default=None, type=int, help="Number of bytes for raw range mode.")
@click.option(
    "-w",
    "--write-mode",
    "write_mode",
    type=click.Choice(["disabled", "confirm", "allowed"], case_sensitive=False),
    default=None,
    help="Write permission mode (default: disabled).",
)
@click.option(
    "-l",
    "--log-file",
    "log_file",
    default=None,
    type=click.Path(),
    help="Log data changes to file.",
)
@click.option(
    "--log-format",
    "log_format",
    type=click.Choice(["csv", "jsonl"], case_sensitive=False),
    default=None,
    help="Log file format (default: csv).",
)
def main(
    address: str | None,
    variables: tuple[str, ...],
    config_file: str | None,
    rack: int | None,
    slot: int | None,
    port: int | None,
    timeout: int | None,
    interval: float | None,
    db_number: int | None,
    db_start: int | None,
    db_size: int | None,
    write_mode: str | None,
    log_file: str | None,
    log_format: str | None,
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

    # Load config file if specified, then merge CLI overrides
    if config_file:
        try:
            cfg = S7MonitorConfig.from_yaml(config_file)
        except (FileNotFoundError, ValueError) as e:
            click.echo(f"Error loading config: {e}", err=True)
            sys.exit(1)
    else:
        cfg = S7MonitorConfig()

    cfg = cfg.merge_cli(
        address=address,
        rack=rack,
        slot=slot,
        port=port,
        timeout=timeout,
        interval=interval,
        write_mode=write_mode,
        db_number=db_number,
        db_start=db_start,
        db_size=db_size,
        variables=variables,
        log_file=log_file,
        log_format=log_format,
    )

    # Resolve defaults for values that weren't set anywhere
    final_address = cfg.address
    if not final_address:
        click.echo("Error: ADDRESS is required (as argument or in config file).", err=True)
        sys.exit(1)

    final_rack = cfg.rack if cfg.rack is not None else 0
    final_slot = cfg.slot if cfg.slot is not None else 2
    final_port = cfg.port if cfg.port is not None else 102
    final_timeout = cfg.timeout if cfg.timeout is not None else 3000
    final_interval = cfg.interval if cfg.interval is not None else 1.0
    final_write_mode = WriteMode(cfg.write_mode.lower()) if cfg.write_mode else WriteMode.DISABLED

    conn_config = ConnectionConfig(
        address=final_address,
        rack=final_rack,
        slot=final_slot,
        tcp_port=final_port,
        timeout_ms=final_timeout,
    )
    connection = S7Connection(conn_config)

    all_variables = cfg.variables

    if all_variables:
        parsed_vars = []
        for v in all_variables:
            try:
                parsed_vars.append(parse_variable_arg(v))
            except ValueError as e:
                click.echo(f"Error parsing variable '{v}': {e}", err=True)
                sys.exit(1)

        # If explicit --db/--size given, extend the DB read range
        if cfg.db is not None:
            db_vars = [v for v in parsed_vars if v.area == S7Area.DB]
            db_dbs = {v.db for v in db_vars}
            if db_dbs and cfg.db not in db_dbs:
                click.echo(f"Error: --db {cfg.db} conflicts with variable DBs {db_dbs}", err=True)
                sys.exit(1)

        read_groups = build_read_groups(parsed_vars)

        # Extend DB read range if --size specified
        if cfg.size is not None:
            db_start_val = cfg.start if cfg.start is not None else 0
            for group in read_groups:
                if group.area == S7Area.DB and (cfg.db is None or group.db == cfg.db):
                    group.size = max(group.size, cfg.size)
                    group.start = min(group.start, db_start_val)

    elif cfg.db is not None and cfg.size is not None:
        db_start_val = cfg.start if cfg.start is not None else 0
        parsed_vars = build_default_variables(cfg.db, db_start_val, cfg.size)
        read_groups = build_read_groups(parsed_vars)
    else:
        click.echo("Error: Provide variable specs or --db and --size for raw range mode.", err=True)
        click.echo("Try: s7pymon --help", err=True)
        sys.exit(1)

    from .logging import LogFormat

    # Resolve log format
    final_log_format = LogFormat.CSV
    if cfg.log_format:
        final_log_format = LogFormat(cfg.log_format.lower())

    app = S7MonitorApp(
        connection=connection,
        variables=parsed_vars,
        read_groups=read_groups,
        poll_interval=final_interval,
        write_mode=final_write_mode,
        log_file=cfg.log_file,
        log_format=final_log_format,
    )
    app.run()


def cli():
    """Entry point wrapper for setuptools console_scripts."""
    main()


if __name__ == "__main__":
    cli()
