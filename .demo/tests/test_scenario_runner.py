import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scenarios import runner


@pytest.mark.parametrize("existing", [False, True])
def test_event_query_reads_storage_without_creating_a_missing_database(tmp_path, existing):
    database = tmp_path / "events.sqlite"
    expected = [{"event_id": "saved-event", "summary": {"configuration_revision": "old"}}]
    if existing:
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE comparison_event (event_id TEXT, summary TEXT)")
            db.execute(
                "INSERT INTO comparison_event VALUES (?, ?)",
                ("saved-event", '{"configuration_revision":"old"}'),
            )

    def execute(arguments, **kwargs):
        assert arguments[:5] == ["run", "--rm", "--no-deps", "test", "python"]
        query = arguments[-1].replace("/data/events.sqlite", str(database))
        assert "mode=ro" in query
        return subprocess.run(
            [sys.executable, "-c", query], capture_output=True, text=True, check=True
        )

    assert runner._stored_events(execute) == (expected if existing else [])
    assert database.exists() is existing


def test_event_query_does_not_hide_an_existing_corrupt_database(tmp_path):
    database = tmp_path / "events.sqlite"
    database.write_text("not a database")

    def execute(arguments, **kwargs):
        query = arguments[-1].replace("/data/events.sqlite", str(database))
        return subprocess.run(
            [sys.executable, "-c", query], capture_output=True, text=True, check=True
        )

    with pytest.raises(subprocess.CalledProcessError):
        runner._stored_events(execute)
    assert database.read_text() == "not a database"


@pytest.mark.parametrize(
    "exit_code, shutdown_complete, passed",
    [(0, True, True), (143, True, True), (137, True, False), (143, False, False)],
)
def test_stage_closes_proxy_before_final_observations_and_cleans_up(
    tmp_path, monkeypatch, exit_code, shutdown_complete, passed
):
    config = json.loads((Path(runner.__file__).parents[1] / "config/docker.json").read_text())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/docker.json").write_text(json.dumps(config))
    monkeypatch.setattr(runner, "__file__", str(tmp_path / "scenarios/runner.py"))
    running = False
    current_config = None
    counts = {"v1": 0, "v2": 0}
    actions = []
    cleaned = False

    def execute(command, *, env, stdout, **kwargs):
        nonlocal running, current_config, cleaned
        output = ""
        if command[1] == "inspect":
            assert not running
            output = json.dumps({"Status": "exited", "ExitCode": exit_code, "OOMKilled": False})
        else:
            assert command[:2] == ["docker", "compose"]
            args = command[6:]
            action = args[0]
            if action == "up":
                assert not running
                running = True
                current_config = json.loads(Path(env["PROXY_CONFIG"]).read_text())
            elif action == "run" and args[-1] == "user":
                assert running
                actions.append("user")
                serving = (
                    "v2" if current_config["snapshot"]["routes"][0]["v2_serve_ratio"] else "v1"
                )
                requests = int(env["DEMO_REQUESTS"])
                counts[serving] += requests
                output = json.dumps(
                    {
                        "attempts": requests,
                        "responses": requests,
                        "transport_errors": 0,
                        "status_counts": {"200": requests},
                        "backend_versions": {
                            "denominator": "responses",
                            "counts": {
                                "v1": requests if serving == "v1" else 0,
                                "v2": requests if serving == "v2" else 0,
                                "unknown": 0,
                            },
                        },
                    }
                )
            elif action == "stop":
                assert running
                running = False
                actions.append("stop")
            elif action == "ps":
                output = "owned-proxy-container\n"
            elif action == "logs":
                output = "Application shutdown complete.\n" if shutdown_complete else ""
            elif action == "down":
                cleaned = True
        if stdout != subprocess.PIPE:
            stdout.write(output)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def health(execute):
        assert running
        actions.append("health")
        return {
            "health": {"ready": True},
            "configuration_revision": current_config["snapshot"]["revision"],
        }

    def backends(execute):
        actions.append("before" if running else "after")
        return {version: {"version": version, "items": count} for version, count in counts.items()}

    def stored(execute):
        assert not running
        actions.append("events")
        return []

    monkeypatch.setattr(runner.subprocess, "run", execute)
    monkeypatch.setattr(runner, "_health", health)
    monkeypatch.setattr(runner, "_backends", backends)
    monkeypatch.setattr(runner, "_stored_events", stored)
    status = runner.run_stages("serving", (runner.Stage("v1", 2, 0), runner.Stage("v2", 2, 1)))
    assert status == (0 if passed else 1)
    assert cleaned
    assert actions == ["health", "before", "user", "stop", "after", "events"] * (2 if passed else 1)
    output = next((tmp_path / "results/scenarios").glob("run-*"))
    result = json.loads((output / "validation.json").read_text())
    assert result["passed"] is passed
    assert (output / "v1/observation.json").exists()
    assert (output / "v1/stored-events.json").exists()
    assert (output / "v1/proxy.log").exists()
    assert not (output / "v1/metrics.prom").exists()
    assert not (output / "v1/state.json").exists()
