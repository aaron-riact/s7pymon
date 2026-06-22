"""YAML config file support for s7pymon.

Config files allow storing connection settings and variable definitions
so long command lines don't need to be repeated.

Example S7 config file (monitor.yaml):

    address: 192.168.1.100
    rack: 0
    slot: 2
    port: 102
    interval: 0.5
    write_mode: confirm
    variables:
      - DB210.Byte0:heartbeat
      - DB210.Byte1:status
      - DB210.Bit1.0:e_stop
      - EB.Byte0:input0

Example EIP config file (eip-monitor.yaml):

    protocol: eip
    address: 192.168.1.200
    interval: 0.5
    output_assembly: 100
    input_assembly: 101
    input_size: 32
    output_size: 32
    rpi_ms: 50
    variables:
      - EIP.Input.Byte0:heartbeat
      - EIP.Input.Byte1:status
      - EIP.Output.Byte0:output0
    rules:
      EIP.Output.Byte0:
        follow: EIP.Input.Byte0
      EIP.Output.Bit0.0:
        toggle: 2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class S7MonitorConfig:
    """Parsed configuration for s7pymon."""

    address: str | None = None
    protocol: str | None = None
    rack: int | None = None
    slot: int | None = None
    port: int | None = None
    timeout: int | None = None
    interval: float | None = None
    write_mode: str | None = None
    db: int | None = None
    start: int | None = None
    size: int | None = None
    variables: list[str] = field(default_factory=list)
    log_file: str | None = None
    log_format: str | None = None
    # EIP-specific
    eip_port: int | None = None
    input_assembly: int | None = None
    output_assembly: int | None = None
    config_assembly: int | None = None
    input_size: int | None = None
    output_size: int | None = None
    rpi_ms: int | None = None
    # Output rules (dict of target -> rule config)
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> S7MonitorConfig:
        """Load config from a YAML file.

        Raises FileNotFoundError if the file doesn't exist.
        Raises ValueError on invalid config content.
        """
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            raw = yaml.safe_load(f)

        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError(f"Config file must be a YAML mapping, got {type(raw).__name__}")

        return cls(
            address=raw.get("address"),
            protocol=raw.get("protocol"),
            rack=raw.get("rack"),
            slot=raw.get("slot"),
            port=raw.get("port"),
            timeout=raw.get("timeout"),
            interval=raw.get("interval"),
            write_mode=raw.get("write_mode"),
            db=raw.get("db"),
            start=raw.get("start"),
            size=raw.get("size"),
            variables=[str(v) for v in raw["variables"]] if "variables" in raw else [],
            log_file=raw.get("log_file"),
            log_format=raw.get("log_format"),
            eip_port=raw.get("eip_port"),
            input_assembly=raw.get("input_assembly"),
            output_assembly=raw.get("output_assembly"),
            config_assembly=raw.get("config_assembly"),
            input_size=raw.get("input_size"),
            output_size=raw.get("output_size"),
            rpi_ms=raw.get("rpi_ms"),
            rules=raw.get("rules", {}),
        )

    def merge_cli(
        self,
        address: str | None = None,
        protocol: str | None = None,
        rack: int | None = None,
        slot: int | None = None,
        port: int | None = None,
        timeout: int | None = None,
        interval: float | None = None,
        write_mode: str | None = None,
        db_number: int | None = None,
        db_start: int | None = None,
        db_size: int | None = None,
        variables: tuple[str, ...] = (),
        log_file: str | None = None,
        log_format: str | None = None,
        eip_port: int | None = None,
        input_assembly: int | None = None,
        output_assembly: int | None = None,
        config_assembly: int | None = None,
        input_size: int | None = None,
        output_size: int | None = None,
        rpi_ms: int | None = None,
    ) -> S7MonitorConfig:
        """Return a new config with CLI args overriding file values.

        CLI values override config file values when explicitly provided.
        For click options, we pass None to indicate "not specified".
        """
        return S7MonitorConfig(
            address=address or self.address,
            protocol=protocol or self.protocol,
            rack=rack if rack is not None else self.rack,
            slot=slot if slot is not None else self.slot,
            port=port if port is not None else self.port,
            timeout=timeout if timeout is not None else self.timeout,
            interval=interval if interval is not None else self.interval,
            write_mode=write_mode or self.write_mode,
            db=db_number if db_number is not None else self.db,
            start=db_start if db_start is not None else self.start,
            size=db_size if db_size is not None else self.size,
            variables=list(variables) if variables else self.variables,
            log_file=log_file or self.log_file,
            log_format=log_format or self.log_format,
            eip_port=eip_port if eip_port is not None else self.eip_port,
            input_assembly=input_assembly if input_assembly is not None else self.input_assembly,
            output_assembly=output_assembly if output_assembly is not None else self.output_assembly,
            config_assembly=config_assembly if config_assembly is not None else self.config_assembly,
            input_size=input_size if input_size is not None else self.input_size,
            output_size=output_size if output_size is not None else self.output_size,
            rpi_ms=rpi_ms if rpi_ms is not None else self.rpi_ms,
        )
