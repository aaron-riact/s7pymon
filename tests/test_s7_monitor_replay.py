import pytest
from click.testing import CliRunner

from busfactor.logging import (
    DataLogger,
    LogEntry,
    LogFormat,
    SessionMetadata,
)
from busfactor.replay import replay_main


@pytest.fixture
def sample_log_csv(tmp_path):
    log_path = tmp_path / "session.csv"
    meta = SessionMetadata(
        started="2024-03-15T10:30:00",
        address="192.168.1.100:102",
        variables=["DB210.Byte0:heartbeat"],
        poll_interval=1.0,
        format="csv",
    )
    with DataLogger(log_path, LogFormat.CSV, meta) as logger:
        logger.log(LogEntry(
            timestamp="2024-03-15T10:30:01",
            variable="heartbeat",
            type="Byte",
            area="DB210",
            offset=0,
            old_value="0",
            new_value="37",
            raw_hex="25",
        ))
    return str(log_path)


class TestReplayCLI:
    def test_nonexistent_file(self):
        runner = CliRunner()
        result = runner.invoke(replay_main, ["/nonexistent/file.csv"])
        assert result.exit_code != 0

    def test_empty_log_file(self, tmp_path):
        log_path = tmp_path / "empty.csv"
        meta = SessionMetadata(
            started="2024-03-15T10:30:00",
            address="192.168.1.100:102",
            variables=[],
            poll_interval=1.0,
            format="csv",
        )
        with DataLogger(log_path, LogFormat.CSV, meta) as logger:
            pass  # No entries
        runner = CliRunner()
        result = runner.invoke(replay_main, [str(log_path)])
        assert result.exit_code != 0
        assert "no data" in result.output.lower()
