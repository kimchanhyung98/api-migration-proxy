import json
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from api_migration_proxy.app import create_app
from api_migration_proxy.collection.collector import BoundedCollector
from api_migration_proxy.collection.query import EventQuery, QueryAccess
from api_migration_proxy.collection.sqlite import SQLiteEventStore
from api_migration_proxy.comparison.engine import (
    BackendResponse,
    ComparisonContext,
    FieldMapping,
    compare,
)
from api_migration_proxy.proxy.runtime import ProxyRuntime
from api_migration_proxy.routing.configuration import ConfigManager
from api_migration_proxy.settings import load_settings, metrics_from_settings

from tests.smoke import check

ROOT = Path(__file__).parents[1]


@pytest.fixture
def local_process():
    processes = []

    def start(arguments):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, *arguments, "--port", str(port)],
            cwd=ROOT,
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
            ["-m", "uvicorn", f"{version}.app:app", "--no-access-log", "--no-proxy-headers"],
        )
        for version in ("v1", "v2")
    )


@pytest.mark.parametrize("serving", ["v1", "v2"])
def test_local_cli_smoke_with_real_backends_and_persistent_events(
    local_process, demo_backends, tmp_path, capsys, serving
):
    v1, v2 = demo_backends
    data = json.loads((ROOT / "config/docker.json").read_text())
    snapshot = data["snapshot"]
    snapshot.update(default_v1=v1.url, allowed_backends=[v1.url, v2.url])
    snapshot["routes"][0].update(v1=v1.url, v2=v2.url, v2_serve_ratio=int(serving == "v2"))
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
    check(proxy.url, str(database), serving)
    assert "PASS:" in capsys.readouterr().out
    proxy.process.send_signal(signal.SIGTERM)
    assert proxy.process.wait(timeout=10) in (0, -signal.SIGTERM)
    assert b"Application shutdown complete." in proxy.process.stderr.read()
    with closing(sqlite3.connect(database)) as db:
        before = dict(db.execute("SELECT event_id, summary FROM comparison_event"))
    assert len(before) == 3
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
    check(restarted.url, str(database), serving)
    with closing(sqlite3.connect(database)) as db:
        after = dict(db.execute("SELECT event_id, summary FROM comparison_event"))
    assert before.items() <= after.items()
    assert len(after) == len(before) + 3


@pytest.mark.parametrize("serving", ["v1", "v2"])
async def test_demo_with_explicit_synthetic_context_compares_both_roles(
    demo_backends, tmp_path, serving
):
    v1, v2 = demo_backends
    data = json.loads((ROOT / "config/docker.json").read_text())
    snapshot = data["snapshot"]
    snapshot.update(default_v1=v1.url, allowed_backends=[v1.url, v2.url])
    snapshot["routes"][0].update(v1=v1.url, v2=v2.url, v2_serve_ratio=int(serving == "v2"))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data))
    settings = load_settings(config)
    store = SQLiteEventStore(":memory:")
    metrics = metrics_from_settings(settings)
    runtime = ProxyRuntime(
        ConfigManager(settings.snapshot),
        comparison_policies=settings.comparison_policies,
        work_limits=settings.work_limits,
        collector=BoundedCollector(store, settings.collection_limits),
        metrics=metrics,
        context_provider=lambda *_: ComparisonContext(True, True, True),
    )
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic-proxy.test"
        ) as client:
            for item, expected in (
                ("same", "different"),
                ("different", "different"),
                ("sample", "different"),
                ("missing", "different"),
                ("error", "execution_error"),
            ):
                response = await client.get(f"/items/{item}")
                assert response.status_code == {"missing": 404, "error": 500}.get(item, 200)
                assert response.headers["x-backend-version"] == serving
                await runtime.flush()
                now = time.time()
                rows = await store.query(
                    EventQuery(now - 100, now + 1, frozenset({"synthetic_item"}), 100),
                    QueryAccess(frozenset({"synthetic_item"}), 100, 200),
                )
                record = rows[-1]["summary"]
                assert record["comparison"]["result"] == expected
                assert record["serving_backend"] == serving
                if expected == "different":
                    assert record["comparison"]["reason"] == "json_field_mismatch"
                    assert record["comparison"]["difference_count"] > 0
        assert (
            metrics.value("comparison_pipeline_total", route="synthetic_item", step="stored") == 5
        )


async def test_explicit_mapping_compares_business_data_and_preserves_original_responses(
    demo_backends,
):
    v1, v2 = demo_backends
    settings = load_settings(ROOT / "config/docker.json")
    policy = settings.comparison_policies["synthetic-json-v1"]
    normalized = replace(
        policy,
        revision="synthetic-envelope-mapping-test",
        mappings=(FieldMapping("/id", "/result/id"), FieldMapping("/name", "/result/name")),
        ignore_paths=("/code", "/message", "/result"),
    )
    context = ComparisonContext(True, True, True)
    async with httpx.AsyncClient(trust_env=False) as client:
        for item, expected in (("same", "matched"), ("different", "different")):
            left = await client.get(f"{v1.url}/items/{item}")
            right = await client.get(f"{v2.url}/items/{item}")
            assert left.status_code == right.status_code == 200
            original = (left.content, right.content)
            responses = (
                BackendResponse(
                    "v1", "serving", "http_response", "success", 200, body=left.content
                ),
                BackendResponse(
                    "v2", "shadow", "http_response", "success", 200, body=right.content
                ),
            )
            assert compare(*responses, policy, context).result == "different"
            result = compare(*responses, normalized, context)
            assert result.result == expected
            assert result.raw_difference_count > 0
            assert "mapping:0" in result.applied_rules and "mapping:1" in result.applied_rules
            if expected == "matched":
                assert result.difference_count == 0
                assert left.json() == right.json()["result"]
            else:
                assert result.difference_paths == ("/name",)
                assert result.reason == "json_value_mismatch"
            assert tuple(response.body for response in responses) == original
            assert (left.content, right.content) == original
