from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from api_migration_proxy.app import create_app, create_control_app, prometheus_text
from api_migration_proxy.collection import BatchResult
from api_migration_proxy.observability import Metrics


class RuntimeDouble:
    def __init__(self):
        self.ready = False
        self.config = SimpleNamespace(current=None)
        self.metrics = Metrics({"registered"})
        self.started = 0
        self.closed = 0
        self.requests = []

    async def start(self):
        self.started += 1
        self.ready = True

    async def close(self):
        self.closed += 1
        self.ready = False

    def observation_status(self):
        return {"worker_id": "local", "collection": {"completeness_known": False}}

    def metric_snapshot(self):
        return self.metrics.snapshot()

    async def __call__(self, scope, receive, send):
        self.requests.append(scope)
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks) or b"backend"
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"set-cookie", b"first=1"), (b"set-cookie", b"second=2")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def test_data_plane_lifecycle_and_unmodified_public_paths():
    runtime = RuntimeDouble()
    with TestClient(create_app(runtime)) as client:
        assert runtime.started == 1
        for path in ("/docs", "/openapi.json", "/health/live", "/metrics", "/state", "/items/42/"):
            response = client.get(path)
            assert response.status_code == 201
            assert response.content == b"backend"
        response = client.post("/items/a%2Fb?k=1&k=2", content=b'{"x":  1}\n')
        assert response.status_code == 201
        assert response.content == b'{"x":  1}\n'
        assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]
        assert runtime.requests[-1]["raw_path"].split(b"?")[0] == b"/items/a%2Fb"
        assert runtime.requests[-1]["query_string"] == b"k=1&k=2"
    assert runtime.closed == 1
    assert runtime.ready is False


def test_lifespan_closes_resources_if_start_fails():
    runtime = RuntimeDouble()

    async def fail_start():
        raise RuntimeError("startup failed")

    runtime.start = fail_start
    with pytest.raises(RuntimeError, match="startup failed"):
        with TestClient(create_app(runtime)):
            pass
    assert runtime.closed == 1


@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/metrics", "/state"])
def test_control_endpoints_require_explicit_authorization(path):
    runtime = RuntimeDouble()
    with TestClient(create_control_app(runtime, authorize=lambda request: False)) as client:
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "Access denied"}
    assert runtime.started == 0
    assert runtime.closed == 0


def test_control_readiness_and_async_authorization():
    runtime = RuntimeDouble()

    async def authorize(request):
        return request.headers.get("x-test-access") == "allow"

    with TestClient(create_control_app(runtime, authorize=authorize)) as client:
        assert client.get("/health/live").status_code == 403
        headers = {"x-test-access": "allow"}
        assert client.get("/health/live", headers=headers).json() == {"status": "alive"}
        response = client.get("/health/ready", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"ready": False, "revision": None}
        runtime.config.current = SimpleNamespace(revision="revision-2")
        runtime.ready = True
        response = client.get("/health/ready", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"ready": True, "revision": "revision-2"}
        assert client.get("/state", headers=headers).json() == {
            "worker_id": "local",
            "collection": {"completeness_known": False},
        }
        assert client.get("/docs", headers=headers).status_code == 404
        assert client.get("/openapi.json", headers=headers).status_code == 404


def test_control_access_errors_are_closed_and_sanitized():
    runtime = RuntimeDouble()

    def authorize(request):
        raise ValueError("sensitive authorization response")

    with TestClient(create_control_app(runtime, authorize=authorize)) as client:
        response = client.get("/metrics")
        assert response.status_code == 503
        assert "sensitive" not in response.text


def test_metrics_exposition_preserves_counters_and_histogram_buckets():
    runtime = RuntimeDouble()
    runtime.metrics.increment("proxy_assignments_total", route="registered", serving="v1")
    runtime.metrics.observe(
        "proxy_request_duration_seconds", 0.006, route="registered", serving="v1"
    )
    runtime.metrics.set_gauge("backend_inflight", 2, backend="v1", role="serving")
    with TestClient(create_control_app(runtime, authorize=lambda request: True)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain; version=0.0.4" in response.headers["content-type"]
    assert "# TYPE proxy_assignments_total counter\n" in response.text
    assert 'proxy_assignments_total{route="registered",serving="v1"} 1\n' in response.text
    assert 'backend_inflight{backend="v1",role="serving"} 2\n' in response.text
    assert (
        'proxy_request_duration_seconds_bucket{route="registered",serving="v1",le="0.005"} 0'
        in response.text
    )
    assert (
        'proxy_request_duration_seconds_bucket{route="registered",serving="v1",le="0.01"} 1'
        in response.text
    )
    assert (
        'proxy_request_duration_seconds_bucket{route="registered",serving="v1",le="+Inf"} 1'
        in response.text
    )
    assert (
        'proxy_request_duration_seconds_count{route="registered",serving="v1"} 1' in response.text
    )
    assert prometheus_text(Metrics({"registered"}).snapshot()) == ""


async def test_control_exports_live_collection_pending_and_acknowledgement(
    backend_factory, runtime_factory, asgi_request
):
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedStore:
        def __init__(self, actual):
            self.actual = actual

        async def write_batch(self, events):
            entered.set()
            await release.wait()
            return await self.actual.write_batch(events)

        async def close(self):
            await self.actual.close()

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, store_wrapper=DelayedStore)
    exchange = await asgi_request(harness.runtime).wait()
    assert exchange.status == 200
    await asyncio.wait_for(entered.wait(), 2)
    app = create_control_app(harness.runtime, authorize=lambda request: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control.test"
    ) as client:
        try:
            pending = await client.get("/metrics")
            assert pending.status_code == 200
            assert "collection_queue_depth 1.0\n" in pending.text
            assert "collection_oldest_age_seconds " in pending.text
            assert "collection_write_failures_total 0\n" in pending.text
            state = (await client.get("/state")).json()
            assert state["scope"] == "current_process"
            assert state["collection"]["queue_depth"] == 1
            assert state["collection"]["queue_bytes"] > 0
            assert state["collection"]["oldest_age_seconds"] >= 0
            assert state["collection"].get("stored", 0) == 0
        finally:
            release.set()
        await harness.runtime.flush()
        completed = await client.get("/metrics")
        assert "collection_queue_depth 0.0\n" in completed.text
        assert "collection_oldest_age_seconds 0.0\n" in completed.text
        assert 'comparison_pipeline_total{route="catalog",step="stored"} 1\n' in completed.text
        state = (await client.get("/state")).json()
        assert state["collection"]["stored"] == 1
        assert state["collection"]["queue_bytes"] == 0


async def test_control_exports_storage_failure_without_counting_scrapes_as_writes(
    backend_factory, runtime_factory, asgi_request
):
    class FailedStore:
        def __init__(self, actual):
            self.actual = actual

        async def write_batch(self, events):
            return BatchResult(failed=frozenset(event.event_id for event in events))

        async def close(self):
            await self.actual.close()

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1, v2, store_wrapper=FailedStore, collection_values={"max_attempts": 2}
    )
    exchange = await asgi_request(harness.runtime).wait()
    assert exchange.status == 200
    await harness.runtime.flush()
    app = create_control_app(harness.runtime, authorize=lambda request: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control.test"
    ) as client:
        first = await client.get("/metrics")
        second = await client.get("/metrics")
        assert first.status_code == second.status_code == 200
        assert first.text == second.text
        assert "collection_write_failures_total 2\n" in first.text
        assert "collection_queue_depth 0.0\n" in first.text
        assert 'collection_dropped_total{reason="retry_exhausted"} 1\n' in first.text
        assert 'comparison_pipeline_total{route="catalog",step="stored"}' not in first.text
        state = (await client.get("/state")).json()
        assert state["scope"] == "current_process"
        assert state["collection"]["write_failures"] == 2
        assert state["collection"]["dropped_retry_exhausted"] == 1
        assert state["collection"]["completeness_known"] is True


def example_settings():
    return json.loads((Path(__file__).parents[1] / "examples/local.json").read_text())


def test_cli_config_check_validates_without_starting_server(monkeypatch, capsys):
    from api_migration_proxy.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("configuration checking must not start a server")

    monkeypatch.setattr("api_migration_proxy.cli.uvicorn.run", forbidden)
    path = Path(__file__).parents[1] / "examples/local.json"
    assert main(["check-config", "--config", str(path)]) == 0
    assert capsys.readouterr().out == "Configuration valid.\n"


@pytest.mark.parametrize(
    "case",
    ["duplicate", "nonfinite", "missing", "unknown", "policy", "overflow", "boolean", "rule_field"],
)
def test_cli_rejects_invalid_configuration_without_values_in_output(tmp_path, capsys, case):
    from api_migration_proxy.cli import main

    data = example_settings()
    if case == "missing":
        del data["snapshot"]["budgets"]["serving_timeout_seconds"]
    elif case == "unknown":
        data["unexpected"] = "sensitive value"
    elif case == "policy":
        data["comparison_policies"] = []
    elif case == "overflow":
        data["work_limits"]["compare_max_age_seconds"] = 10**1000
    elif case == "boolean":
        data["comparison_policies"][0]["allow_empty_body"] = "false"
    elif case == "rule_field":
        data["comparison_policies"][0]["tolerances"] = [
            {"path": "/id", "absolute": "1", "unknown": "sensitive value"}
        ]
    content = json.dumps(data)
    if case == "duplicate":
        content = content.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    elif case == "nonfinite":
        content = content.replace('"serving_timeout_seconds": 5', '"serving_timeout_seconds": NaN')
    path = tmp_path / "sensitive-filename.json"
    path.write_text(content)
    assert main(["check-config", "--config", str(path)]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err == "Configuration could not be loaded or validated.\n"


def test_cli_requires_explicit_event_store_and_valid_port(capsys):
    from api_migration_proxy.cli import main

    with pytest.raises(SystemExit) as error:
        main(["serve", "--config", "example.json", "--port", "8080"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        main(["serve", "--config", "example.json", "--port", "65536", "--event-store", ":memory:"])
    assert error.value.code == 2


def test_cli_local_serving_does_not_enable_access_logs_or_forwarded_headers(monkeypatch, tmp_path):
    from api_migration_proxy.cli import main

    calls = []
    monkeypatch.setattr(
        "api_migration_proxy.cli.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    path = Path(__file__).parents[1] / "examples/local.json"
    assert (
        main(
            [
                "serve",
                "--config",
                str(path),
                "--port",
                "8080",
                "--event-store",
                str(tmp_path / "events.sqlite"),
            ]
        )
        == 0
    )
    assert len(calls) == 1
    _, options = calls[0]
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8080
    assert options["workers"] == 1
    assert options["access_log"] is False
    assert options["proxy_headers"] is False
    assert list(tmp_path.iterdir()) == []


def test_cli_process_forwards_real_loopback_http_and_shuts_down(tmp_path):
    requests = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'{"source":"synthetic-v1"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "first=1")
            self.send_header("Set-Cookie", "second=2")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            proxy_port = reserved.getsockname()[1]
        data = example_settings()
        origin = f"http://127.0.0.1:{backend.server_port}"
        data["snapshot"]["default_v1"] = origin
        data["snapshot"]["allowed_backends"] = [origin]
        data["snapshot"]["routes"][0]["v1"] = origin
        data["snapshot"]["routes"][0]["v2"] = origin
        config = tmp_path / "synthetic.json"
        config.write_text(json.dumps(data))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "api_migration_proxy.cli",
                "serve",
                "--config",
                str(config),
                "--port",
                str(proxy_port),
                "--event-store",
                str(tmp_path / "events.sqlite"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                pytest.fail("proxy process exited before accepting requests")
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("proxy did not accept connections before startup deadline")
                time.sleep(0.02)
        with httpx.Client(base_url=f"http://127.0.0.1:{proxy_port}", trust_env=False) as client:
            response = client.get("/items/a%2Fb?key=1&key=2")
            assert response.status_code == 200
            assert response.content == b'{"source":"synthetic-v1"}'
            assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]
            assert client.get("/docs").json() == {"source": "synthetic-v1"}
        assert requests == ["/items/a%2Fb?key=1&key=2", "/docs"]
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=10)
        assert process.returncode in (0, -signal.SIGINT)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
