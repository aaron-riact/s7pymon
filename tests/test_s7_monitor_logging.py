import json
import pytest
from pathlib import Path

from s7pymon.logging import (
    CSV_FIELDS,
    DataLogger,
    LogEntry,
    LogFormat,
    SessionMetadata,
    load_log_file,
)


@pytest.fixture
def sample_metadata():
    return SessionMetadata(
        started="2024-03-15T10:30:00",
        address="192.168.1.100:102",
        variables=["DB210.Byte0:heartbeat", "DB210.Byte1:status"],
        poll_interval=1.0,
        format="csv",
    )


@pytest.fixture
def sample_entry():
    return LogEntry(
        timestamp="2024-03-15T10:30:01.123",
        variable="heartbeat",
        type="Byte",
        area="DB210",
        offset=0,
        old_value="0",
        new_value="37",
        raw_hex="25",
    )


class TestLogEntry:
    def test_to_dict(self, sample_entry):
        d = sample_entry.to_dict()
        assert d["variable"] == "heartbeat"
        assert d["old_value"] == "0"
        assert d["new_value"] == "37"
        assert d["raw_hex"] == "25"


class TestSessionMetadata:
    def test_to_dict(self, sample_metadata):
        d = sample_metadata.to_dict()
        assert d["session"] is True
        assert d["address"] == "192.168.1.100:102"
        assert len(d["variables"]) == 2


class TestDataLoggerCSV:
    def test_csv_output(self, tmp_path, sample_metadata, sample_entry):
        log_path = tmp_path / "test.csv"
        with DataLogger(log_path, LogFormat.CSV, sample_metadata) as logger:
            logger.log(sample_entry)
            assert logger.entry_count == 1

        content = log_path.read_text()
        assert "# s7pymon session log" in content
        assert "# started: 2024-03-15T10:30:00" in content
        assert "timestamp,variable,type,area,offset,old_value,new_value,raw_hex" in content
        assert "heartbeat" in content
        assert "37" in content

    def test_csv_multiple_entries(self, tmp_path, sample_metadata):
        log_path = tmp_path / "test.csv"
        with DataLogger(log_path, LogFormat.CSV, sample_metadata) as logger:
            for i in range(5):
                logger.log(LogEntry(
                    timestamp=f"2024-03-15T10:30:0{i}",
                    variable="heartbeat",
                    type="Byte",
                    area="DB210",
                    offset=0,
                    old_value=str(i),
                    new_value=str(i + 1),
                    raw_hex=f"{i+1:02X}",
                ))
            assert logger.entry_count == 5


class TestDataLoggerJSONL:
    def test_jsonl_output(self, tmp_path, sample_metadata, sample_entry):
        meta = SessionMetadata(**{**sample_metadata.__dict__, "format": "jsonl"})
        log_path = tmp_path / "test.jsonl"
        with DataLogger(log_path, LogFormat.JSONL, meta) as logger:
            logger.log(sample_entry)

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2  # metadata + 1 entry

        session = json.loads(lines[0])
        assert session["session"] is True

        entry = json.loads(lines[1])
        assert entry["variable"] == "heartbeat"
        assert entry["new_value"] == "37"


class TestLoadLogFile:
    def test_load_csv(self, tmp_path, sample_metadata, sample_entry):
        log_path = tmp_path / "test.csv"
        with DataLogger(log_path, LogFormat.CSV, sample_metadata) as logger:
            logger.log(sample_entry)

        metadata, entries = load_log_file(log_path)
        assert metadata is not None
        assert metadata.started == "2024-03-15T10:30:00"
        assert metadata.address == "192.168.1.100:102"
        assert len(entries) == 1
        assert entries[0].variable == "heartbeat"
        assert entries[0].new_value == "37"

    def test_load_jsonl(self, tmp_path, sample_metadata, sample_entry):
        meta = SessionMetadata(**{**sample_metadata.__dict__, "format": "jsonl"})
        log_path = tmp_path / "test.jsonl"
        with DataLogger(log_path, LogFormat.JSONL, meta) as logger:
            logger.log(sample_entry)

        metadata, entries = load_log_file(log_path)
        assert metadata is not None
        assert metadata.started == "2024-03-15T10:30:00"
        assert len(entries) == 1
        assert entries[0].variable == "heartbeat"

    def test_load_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_log_file("/nonexistent/file.csv")

    def test_roundtrip_csv(self, tmp_path, sample_metadata):
        log_path = tmp_path / "test.csv"
        original_entries = []
        with DataLogger(log_path, LogFormat.CSV, sample_metadata) as logger:
            for i in range(3):
                entry = LogEntry(
                    timestamp=f"2024-03-15T10:30:0{i}",
                    variable=f"var_{i}",
                    type="Byte",
                    area="DB210",
                    offset=i,
                    old_value=str(i),
                    new_value=str(i + 10),
                    raw_hex=f"{i+10:02X}",
                )
                logger.log(entry)
                original_entries.append(entry)

        metadata, entries = load_log_file(log_path)
        assert len(entries) == 3
        for orig, loaded in zip(original_entries, entries):
            assert orig.variable == loaded.variable
            assert orig.new_value == loaded.new_value
            assert orig.offset == loaded.offset

    def test_roundtrip_jsonl(self, tmp_path, sample_metadata):
        meta = SessionMetadata(**{**sample_metadata.__dict__, "format": "jsonl"})
        log_path = tmp_path / "test.jsonl"
        with DataLogger(log_path, LogFormat.JSONL, meta) as logger:
            for i in range(3):
                logger.log(LogEntry(
                    timestamp=f"2024-03-15T10:30:0{i}",
                    variable=f"var_{i}",
                    type="Byte",
                    area="DB210",
                    offset=i,
                    old_value=str(i),
                    new_value=str(i + 10),
                    raw_hex=f"{i+10:02X}",
                ))

        metadata, entries = load_log_file(log_path)
        assert len(entries) == 3
        assert metadata is not None
        assert metadata.format == "jsonl"
