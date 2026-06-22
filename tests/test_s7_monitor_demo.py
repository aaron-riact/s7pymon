import json
import threading
import time
import urllib.request

from click.testing import CliRunner

from s7pymon.demo import DEMO_DB, build_demo_engine, demo_web_cli
from s7pymon.protocols import DataSource
from s7pymon.web import S7WebServer


class TestBuildDemoEngine:
    def test_describe_exposes_demo_layout(self):
        engine, connection = build_demo_engine(seed=7)
        try:
            meta = engine.describe()
            assert meta["address"].startswith("192.168.0.50:102")
            assert meta["write_mode"] == "allowed"
            assert [v["label"] for v in meta["variables"]] == [
                "heartbeat",
                "temperature",
                "pressure",
                "e_stop",
                "running",
                "cycles",
            ]
            assert meta["groups"] == [{"key": f"DB{DEMO_DB}", "label": f"DB{DEMO_DB}", "start": 0, "size": 16}]
        finally:
            connection.close()

    def test_demo_values_change_over_time(self):
        engine, connection = build_demo_engine(poll_interval=0.01, seed=3)
        try:
            engine.connect()
            first = connection.read_source(DataSource.s7_db(DEMO_DB), 0, 16).data
            time.sleep(0.04)
            second = connection.read_source(DataSource.s7_db(DEMO_DB), 0, 16).data
            assert first != second
        finally:
            connection.close()


class TestDemoHttp:
    def test_demo_server_exposes_synthetic_state(self):
        engine, connection = build_demo_engine(poll_interval=0.01, seed=5)
        engine.connect()
        server = S7WebServer(engine, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.start()
        try:
            with urllib.request.urlopen(server.url + "api/state", timeout=2) as resp:
                payload = json.loads(resp.read())
            assert payload["address"].startswith("192.168.0.50:102")
            assert payload["variables"][0]["label"] == "heartbeat"
        finally:
            server.shutdown()
            connection.close()
            thread.join(timeout=2)


class TestDemoCli:
    def test_help_shows_easy_start_options(self):
        runner = CliRunner()
        result = runner.invoke(demo_web_cli, ["--help"])
        assert result.exit_code == 0
        assert "s7pymon-demo" in result.output
        assert "--open" in result.output
        assert "--seed" in result.output
