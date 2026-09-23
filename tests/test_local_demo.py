import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.smoke import check

ROOT = Path(__file__).parents[1]


@pytest.fixture
def local_process():
    processes = []

    def start(arguments, *, environment=None):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, *arguments, "--port", str(port)],
            cwd=ROOT,
            env={**os.environ, **(environment or {})},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, process.communicate()[1].decode()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return SimpleNamespace(url=f"http://127.0.0.1:{port}", process=process)
            except OSError:
                assert time.monotonic() < deadline, "local process startup timed out"
                time.sleep(0.02)

    yield start
    for process in reversed(processes):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            _, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail("local process did not shut down within its deadline")
        assert process.returncode in (0, -signal.SIGTERM), errors.decode()


@pytest.fixture
def demo_backends(local_process):
    return tuple(
        local_process(
            ["-m", "uvicorn", "examples.backend:app", "--no-access-log", "--no-proxy-headers"],
            environment={"BACKEND_VERSION": version},
        )
        for version in ("v1", "v2")
    )


def test_local_cli_smoke_with_real_backends_and_persistent_events(
    local_process, demo_backends, tmp_path, capsys
):
    v1, v2 = demo_backends
    data = json.loads((ROOT / "examples/docker.json").read_text())
    snapshot = data["snapshot"]
    snapshot.update(default_v1=v1.url, allowed_backends=[v1.url, v2.url])
    snapshot["routes"][0].update(v1=v1.url, v2=v2.url)
    config, database = tmp_path / "config.json", tmp_path / "events.sqlite"
    config.write_text(json.dumps(data))
    proxy = local_process(
        [
            "-m",
            "api_migration_proxy.cli",
            "serve",
            "--config",
            str(config),
            "--event-store",
            str(database),
        ]
    )
    check(proxy.url, str(database))
    assert "PASS:" in capsys.readouterr().out
    proxy.process.send_signal(signal.SIGTERM)
    assert proxy.process.wait(timeout=10) in (0, -signal.SIGTERM)
    assert b"Application shutdown complete." in proxy.process.stderr.read()
    with closing(sqlite3.connect(database)) as db:
        before = dict(db.execute("SELECT event_id, summary FROM comparison_event"))
    assert len(before) >= 2
    restarted = local_process(
        [
            "-m",
            "api_migration_proxy.cli",
            "serve",
            "--config",
            str(config),
            "--event-store",
            str(database),
        ]
    )
    check(restarted.url, str(database))
    with closing(sqlite3.connect(database)) as db:
        after = dict(db.execute("SELECT event_id, summary FROM comparison_event"))
    assert before.items() <= after.items()
    assert len(after) >= len(before) + 2


@pytest.mark.parametrize("serving", ["v1", "v2"])
async def test_demo_with_explicit_synthetic_context_compares_both_roles(
    demo_backends, runtime_factory, asgi_request, serving
):
    v1, v2 = demo_backends
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"path_template": "/items/{id}", "v2_serve_ratio": int(serving == "v2")},
    )
    for item, expected in (
        ("same", "matched"),
        ("different", "different"),
        ("missing", "matched"),
        ("error", "execution_error"),
    ):
        response = await asgi_request(harness.runtime, path=f"/items/{item}".encode()).wait()
        assert response.status == {"missing": 404, "error": 500}.get(item, 200)
        headers = next(message["headers"] for message in response.messages if "status" in message)
        assert dict(headers)[b"x-backend-version"] == serving.encode()
        await harness.runtime.flush()
        record = (await harness.records())[-1]
        assert record["comparison"]["result"] == expected
        assert record["serving_backend"] == serving
    assert harness.metrics.value("comparison_pipeline_total", route="catalog", step="stored") == 4
