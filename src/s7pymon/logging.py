"""Data change logging for s7pymon.

Records timestamped variable value changes to a log file for later
analysis. Supports CSV and JSONL output formats.

CSV format:
    # s7pymon session log
    # started: 2024-03-15T10:30:00
    # address: 192.168.1.100:102
    # variables: DB210.Byte0:heartbeat, DB210.Byte1:status
    timestamp,variable,type,area,offset,old_value,new_value,raw_hex
    2024-03-15T10:30:01.123,heartbeat,Byte,DB210,0,0,37,25

JSONL format: One JSON object per line, first line is session metadata.
"""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TextIO


class LogFormat(Enum):
    CSV = "csv"
    JSONL = "jsonl"


@dataclass
class LogEntry:
    """A single logged data change."""

    timestamp: str
    variable: str
    type: str
    area: str
    offset: int
    old_value: str
    new_value: str
    raw_hex: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "variable": self.variable,
            "type": self.type,
            "area": self.area,
            "offset": self.offset,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "raw_hex": self.raw_hex,
        }


@dataclass
class SessionMetadata:
    """Metadata about a logging session."""

    started: str
    address: str
    variables: list[str]
    poll_interval: float
    format: str

    def to_dict(self) -> dict:
        return {
            "session": True,
            "started": self.started,
            "address": self.address,
            "variables": self.variables,
            "poll_interval": self.poll_interval,
            "format": self.format,
        }


CSV_FIELDS = ["timestamp", "variable", "type", "area", "offset", "old_value", "new_value", "raw_hex"]


class DataLogger:
    """Logs variable value changes to a file."""

    def __init__(self, path: str | Path, fmt: LogFormat, metadata: SessionMetadata):
        self._path = Path(path)
        self._format = fmt
        self._metadata = metadata
        self._file: TextIO | None = None
        self._csv_writer: csv.DictWriter | None = None
        self._entry_count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entry_count(self) -> int:
        return self._entry_count

    def open(self) -> None:
        """Open the log file and write the header."""
        self._file = open(self._path, "w", newline="", encoding="utf-8")

        if self._format == LogFormat.CSV:
            # Write comment header lines
            self._file.write(f"# s7pymon session log\n")
            self._file.write(f"# started: {self._metadata.started}\n")
            self._file.write(f"# address: {self._metadata.address}\n")
            self._file.write(f"# poll_interval: {self._metadata.poll_interval}\n")
            self._file.write(f"# variables: {', '.join(self._metadata.variables)}\n")
            self._csv_writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
            self._csv_writer.writeheader()
            self._file.flush()
        elif self._format == LogFormat.JSONL:
            self._file.write(json.dumps(self._metadata.to_dict()) + "\n")
            self._file.flush()

    def log(self, entry: LogEntry) -> None:
        """Write a single change entry."""
        if self._file is None:
            return

        if self._format == LogFormat.CSV:
            assert self._csv_writer is not None
            self._csv_writer.writerow(entry.to_dict())
        elif self._format == LogFormat.JSONL:
            self._file.write(json.dumps(entry.to_dict()) + "\n")

        self._entry_count += 1
        self._file.flush()

    def close(self) -> None:
        """Close the log file."""
        if self._file is not None:
            self._file.close()
            self._file = None
            self._csv_writer = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


def load_log_file(path: str | Path) -> tuple[SessionMetadata | None, list[LogEntry]]:
    """Load a previously written log file.

    Returns (metadata, entries). Metadata may be None if not present.
    Auto-detects format from file extension or content.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Log file not found: {path}")

    content = path.read_text(encoding="utf-8")

    # Detect format
    if path.suffix == ".jsonl" or (content.startswith("{") and '"session"' in content.split("\n")[0]):
        return _load_jsonl(content)
    else:
        return _load_csv(content)


def _load_jsonl(content: str) -> tuple[SessionMetadata | None, list[LogEntry]]:
    metadata = None
    entries = []

    for line in content.strip().split("\n"):
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("session"):
            metadata = SessionMetadata(
                started=obj["started"],
                address=obj["address"],
                variables=obj["variables"],
                poll_interval=obj["poll_interval"],
                format=obj["format"],
            )
        else:
            entries.append(LogEntry(**obj))

    return metadata, entries


def _load_csv(content: str) -> tuple[SessionMetadata | None, list[LogEntry]]:
    metadata = None
    meta_lines: dict[str, str] = {}
    data_lines = []

    for line in content.split("\n"):
        if line.startswith("# "):
            # Parse comment metadata
            if ": " in line[2:]:
                key, val = line[2:].split(": ", 1)
                meta_lines[key.strip()] = val.strip()
        elif line.strip() and not line.startswith("#"):
            data_lines.append(line)

    if meta_lines.get("started"):
        metadata = SessionMetadata(
            started=meta_lines.get("started", ""),
            address=meta_lines.get("address", ""),
            variables=[v.strip() for v in meta_lines.get("variables", "").split(",") if v.strip()],
            poll_interval=float(meta_lines.get("poll_interval", "1.0")),
            format="csv",
        )

    entries = []
    if data_lines:
        reader = csv.DictReader(data_lines)
        for row in reader:
            entries.append(LogEntry(
                timestamp=row["timestamp"],
                variable=row["variable"],
                type=row["type"],
                area=row["area"],
                offset=int(row["offset"]),
                old_value=row["old_value"],
                new_value=row["new_value"],
                raw_hex=row["raw_hex"],
            ))

    return metadata, entries
