from __future__ import annotations

import asyncio
import json
import os
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
from fastapi import Request
from fastapi.testclient import TestClient

from api_migration_proxy.app import control_authorizer, create_app, create_control_app
from api_migration_proxy.collection.collector import BatchResult
from api_migration_proxy.observability.metrics import Metrics


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
        for path in (
            "/docs",
            "/openapi.json",
            "/healthcheck",
            "/health/live",
            "/metrics",
            "/state",
            "/items/42/",
        ):
            response = client.get(path)
            assert response.status_code == 201
            assert response.content == b"backend"
        response = client.post("/items/a%2Fb?k=1&k=2", content=b'{"x":  1}\n')
        assert response.status_code == 201
        assert response.content == b'{"x":  1}\n'
        assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]
        assert runtime.requests[-1]["raw_path"].split(b"?")[0] == b"/items/a%2Fb"
        assert runtime.requests[-1]["query_string"] == b"k=1&k=2"
        response = client.get("/items/a%0Ab?k=1&k=2")
        assert response.status_code == 201
        assert response.content == b"backend"
        assert runtime.requests[-1]["path"] == "/items/a\nb"
        assert runtime.requests[-1]["raw_path"].split(b"?")[0] == b"/items/a%0Ab"
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


async def test_default_control_does_not_register_observation_routes_or_disable_collection(
    backend_factory, runtime_factory, asgi_request
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2)
    app = create_control_app(harness.runtime, authorize=lambda request: True)
    assert {route.path for route in app.routes} == {"/healthcheck"}
    exchange = await asgi_request(harness.runtime).wait()
    assert exchange.status == 200
    await harness.runtime.flush()
    assert harness.metrics.value("proxy_assignments_total", route="catalog", serving="v1") == 1
    assert harness.metrics.value("comparison_pipeline_total", route="catalog", step="stored") == 1
    assert len(await harness.records()) == 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control.test"
    ) as client:
        for path in ("/metrics", "/state", "/health/live", "/health/ready"):
            assert (await client.get(path)).status_code == 404
        assert (await client.get("/healthcheck")).json() == {"ready": True}


def test_healthcheck_requires_explicit_authorization():
    runtime = RuntimeDouble()
    with TestClient(
        create_control_app(runtime, authorize=lambda request: False), client=("127.0.0.1", 1234)
    ) as client:
        response = client.get("/healthcheck")
        assert response.status_code == 403
        assert response.json() == {"detail": "Access denied"}
    assert runtime.started == 0
    assert runtime.closed == 0


def test_control_readiness_and_async_authorization():
    runtime = RuntimeDouble()

    async def authorize(request):
        return request.headers.get("x-test-access") == "allow"

    with TestClient(
        create_control_app(runtime, authorize=authorize), client=("127.0.0.1", 1234)
    ) as client:
        assert client.get("/healthcheck").status_code == 403
        headers = {"x-test-access": "allow"}
        response = client.get("/healthcheck", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"ready": False}
        runtime.config.current = SimpleNamespace(revision="revision-2", routes=[])
        runtime.ready = True
        response = client.get("/healthcheck", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"ready": True}
        for path in ("/health/live", "/health/ready", "/metrics", "/state"):
            assert client.get(path, headers=headers).status_code == 404
        assert client.get("/docs", headers=headers).status_code == 404
        assert client.get("/openapi.json", headers=headers).status_code == 404


def test_control_access_errors_are_closed_and_sanitized():
    runtime = RuntimeDouble()

    def authorize(request):
        raise ValueError("sensitive authorization response")

    with TestClient(
        create_control_app(runtime, authorize=authorize), client=("127.0.0.1", 1234)
    ) as client:
        response = client.get("/healthcheck")
        assert response.status_code == 503
        assert "sensitive" not in response.text


@pytest.mark.parametrize(
    ("peer", "allowed"),
    [
        (("127.0.0.1", 1234), True),
        (("::1", 1234), True),
        (("192.0.2.1", 1234), False),
        (("localhost", 1234), False),
        (None, False),
    ],
)
@pytest.mark.parametrize("token", ["", "expected-token"])
def test_control_uses_actual_loopback_peer_even_with_valid_token(peer, allowed, token):
    request = Request(
        {
            "type": "http",
            "client": peer,
            "headers": [
                (b"authorization", b"Bearer expected-token"),
                (b"x-forwarded-for", b"127.0.0.1"),
                (b"forwarded", b"for=127.0.0.1"),
            ],
        }
    )
    assert control_authorizer(token)(request) is allowed


@pytest.mark.parametrize("peer,status", [("127.0.0.1", 200), ("192.0.2.1", 403)])
async def test_healthcheck_rejects_remote_peer_with_valid_token(peer, status):
    runtime = RuntimeDouble()
    runtime.ready = True
    app = create_control_app(runtime, authorize=control_authorizer("expected-token"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
        base_url="http://control.test",
    ) as client:
        response = await client.get(
            "/healthcheck",
            headers={
                "Authorization": "Bearer expected-token",
                "X-Forwarded-For": "127.0.0.1",
                "Forwarded": "for=127.0.0.1",
            },
        )
    assert response.status_code == status
    assert response.json() == ({"ready": True} if status == 200 else {"detail": "Access denied"})


@pytest.mark.parametrize(
    "peer,status",
    [("127.0.0.1", 200), ("::1", 200), ("192.0.2.1", 403), ("2001:db8::1", 403)],
)
@pytest.mark.parametrize("async_authorizer", [False, True])
async def test_custom_authorizer_cannot_bypass_healthcheck_loopback(peer, status, async_authorizer):
    runtime = RuntimeDouble()
    runtime.ready = True
    calls = []

    def authorize(request):
        calls.append(request)
        return True

    async def authorize_async(request):
        return authorize(request)

    app = create_control_app(runtime, authorize=authorize_async if async_authorizer else authorize)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
        base_url="http://control.test",
    ) as client:
        response = await client.get(
            "/healthcheck",
            headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
        )
    assert response.status_code == status
    assert len(calls) == (1 if status == 200 else 0)


def test_control_token_rejects_ambiguous_duplicate_authorization():
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"authorization", b"Bearer expected-token"),
                (b"authorization", b"Bearer other-token"),
            ],
        }
    )
    assert control_authorizer("expected-token")(request) is False


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_control_bearer_scheme_is_case_insensitive(scheme):
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 1234),
            "headers": [(b"authorization", f"{scheme} expected-token".encode())],
        }
    )
    assert control_authorizer("expected-token")(request) is True


def test_internal_metrics_preserve_counters_and_histogram_buckets():
    runtime = RuntimeDouble()
    runtime.metrics.increment("proxy_assignments_total", route="registered", serving="v1")
    runtime.metrics.observe(
        "proxy_request_duration_seconds", 0.006, route="registered", serving="v1"
    )
    runtime.metrics.set_gauge("backend_inflight", 2, backend="v1", role="serving")
    snapshot = runtime.metric_snapshot()
    route_labels = (("route", "registered"), ("serving", "v1"))
    assert snapshot.counters[("proxy_assignments_total", route_labels)] == 1
    assert snapshot.gauges[("backend_inflight", (("backend", "v1"), ("role", "serving")))] == 2
    histogram = snapshot.histograms[("proxy_request_duration_seconds", route_labels)]
    assert histogram.buckets[histogram.bounds.index(0.005)] == 0
    assert histogram.buckets[histogram.bounds.index(0.01)] == 1
    assert histogram.count == 1
    assert histogram.total == 0.006
    empty = Metrics({"registered"}).snapshot()
    assert empty.counters == empty.gauges == empty.histograms == {}


async def test_internal_metrics_report_live_collection_pending_and_acknowledgement(
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
    try:
        pending = harness.runtime.metric_snapshot()
        assert pending.gauges[("collection_queue_depth", ())] == 1
        assert pending.gauges[("collection_oldest_age_seconds", ())] >= 0
        assert pending.counters[("collection_write_failures_total", ())] == 0
        state = harness.runtime.observation_status()
        assert state["scope"] == "current_process"
        assert state["collection"]["queue_depth"] == 1
        assert state["collection"]["queue_bytes"] > 0
        assert state["collection"]["oldest_age_seconds"] >= 0
        assert state["collection"].get("stored", 0) == 0
    finally:
        release.set()
    await harness.runtime.flush()
    completed = harness.runtime.metric_snapshot()
    assert completed.gauges[("collection_queue_depth", ())] == 0
    assert completed.gauges[("collection_oldest_age_seconds", ())] == 0
    assert (
        completed.counters[
            ("comparison_pipeline_total", (("route", "catalog"), ("step", "stored")))
        ]
        == 1
    )
    state = harness.runtime.observation_status()
    assert state["collection"]["stored"] == 1
    assert state["collection"]["queue_bytes"] == 0


async def test_internal_metrics_report_storage_failure_without_counting_reads_as_writes(
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
    first = harness.runtime.metric_snapshot()
    second = harness.runtime.metric_snapshot()
    assert first == second
    assert first.counters[("collection_write_failures_total", ())] == 2
    assert first.gauges[("collection_queue_depth", ())] == 0
    assert first.counters[("collection_dropped_total", (("reason", "retry_exhausted"),))] == 1
    assert (
        "comparison_pipeline_total",
        (("route", "catalog"), ("step", "stored")),
    ) not in first.counters
    state = harness.runtime.observation_status()
    assert state["scope"] == "current_process"
    assert state["collection"]["write_failures"] == 2
    assert state["collection"]["dropped_retry_exhausted"] == 1
    assert state["collection"]["completeness_known"] is True


def example_settings():
    return json.loads((Path(__file__).parents[1] / "fixtures/proxy.json").read_text())


@pytest.fixture
def cli_environment(monkeypatch, tmp_path):
    for name in os.environ:
        if name.startswith("API_PROXY_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_cli_config_check_validates_without_starting_server(monkeypatch, capsys, cli_environment):
    from api_migration_proxy.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("configuration checking must not start a server")

    monkeypatch.setattr("api_migration_proxy.cli.run_server", forbidden)
    monkeypatch.setattr("api_migration_proxy.cli.SQLiteEventStore", forbidden)
    path = Path(__file__).parents[1] / "fixtures/proxy.json"
    assert main(["check-config", "--config", str(path)]) == 0
    assert capsys.readouterr().out == "Configuration valid.\n"


def test_postgresql_config_check_is_offline_and_explains_only_safe_run_options(
    monkeypatch, tmp_path, capsys, cli_environment
):
    from api_migration_proxy.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("configuration checking must not construct storage or start background work")

    monkeypatch.setattr("api_migration_proxy.cli.PostgreSQLEventStore", forbidden)
    monkeypatch.setattr("api_migration_proxy.cli.RetentionWorker", forbidden)
    monkeypatch.setattr("api_migration_proxy.cli.run_server", forbidden)
    env = tmp_path / "postgres.env"
    env.write_text(
        "API_PROXY_EVENT_STORE_BACKEND=postgresql\n"
        "API_PROXY_POSTGRES_DSN=postgresql://operator:sensitive-password@private-db/events\n"
        "API_PROXY_EVENT_STORE=private-file.sqlite\n"
        "API_PROXY_EXPOSE_OBSERVABILITY=true\n"
        "API_PROXY_RETENTION_INTERVAL_SECONDS=1.5\n"
        "API_PROXY_RETENTION_BATCH_SIZE=23\n"
    )
    assert main(["check-config", "--env-file", str(env), "--explain"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output.splitlines()[1])
    assert report["runtime"] == {
        "event_store_backend": "postgresql",
        "retention_interval_seconds": 1.5,
        "retention_batch_size": 23,
    }
    assert "sensitive-password" not in output
    assert "private-db" not in output
    assert "private-file" not in output


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_cli_selects_store_and_attaches_internal_retention(
    monkeypatch, tmp_path, cli_environment, backend
):
    from api_migration_proxy.cli import main

    stores, workers, runtimes = [], [], []

    def store_factory(value, *, create=True):
        store = SimpleNamespace(value=value, create=create)
        stores.append(store)
        return store

    def worker_factory(store, **kwargs):
        worker = SimpleNamespace(store=store, **kwargs)
        workers.append(worker)
        return worker

    def runtime_factory(*args, **kwargs):
        runtime = SimpleNamespace(**kwargs)
        runtimes.append(runtime)
        return runtime

    selected = "PostgreSQLEventStore" if backend == "postgresql" else "SQLiteEventStore"
    other = "SQLiteEventStore" if backend == "postgresql" else "PostgreSQLEventStore"
    monkeypatch.setattr(f"api_migration_proxy.cli.{selected}", store_factory)
    monkeypatch.setattr(
        f"api_migration_proxy.cli.{other}", lambda *_a, **_kw: pytest.fail("wrong adapter")
    )
    monkeypatch.setattr("api_migration_proxy.cli.RetentionWorker", worker_factory)
    monkeypatch.setattr("api_migration_proxy.cli.ProxyRuntime", runtime_factory)
    monkeypatch.setattr("api_migration_proxy.cli.run_server", lambda *_: 0)
    monkeypatch.setenv("API_PROXY_EVENT_STORE_BACKEND", backend)
    monkeypatch.setenv("API_PROXY_POSTGRES_DSN", "postgresql://operator:sensitive@localhost/events")
    monkeypatch.setenv("API_PROXY_RETENTION_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("API_PROXY_RETENTION_BATCH_SIZE", "17")
    assert main(["serve"]) == 0
    assert len(stores) == len(workers) == len(runtimes) == 1
    assert stores[0].value == (
        "postgresql://operator:sensitive@localhost/events"
        if backend == "postgresql"
        else "events.sqlite"
    )
    assert stores[0].create is True
    assert workers[0].store is stores[0]
    assert workers[0].interval_seconds == 0.5
    assert workers[0].batch_size == 17
    assert runtimes[0].maintenance is workers[0]


def test_explicit_sqlite_path_overrides_postgresql_environment_for_serving_and_cleanup(
    monkeypatch, tmp_path, capsys, cli_environment
):
    from api_migration_proxy.cli import main

    calls = []

    class Store:
        def __init__(self, path, *, create=True):
            calls.append((path, create))

        async def purge_expired(self, *, batch_size):
            return {"details_deleted": 0, "summaries_deleted": 0, "expired_pending": 0}

        async def close(self):
            pass

    monkeypatch.setenv("API_PROXY_EVENT_STORE_BACKEND", "postgresql")
    monkeypatch.setenv("API_PROXY_POSTGRES_DSN", "")
    monkeypatch.setattr("api_migration_proxy.cli.SQLiteEventStore", Store)
    monkeypatch.setattr(
        "api_migration_proxy.cli.PostgreSQLEventStore",
        lambda *_a, **_kw: pytest.fail("wrong adapter"),
    )
    monkeypatch.setattr("api_migration_proxy.cli.run_server", lambda *_: 0)
    assert main(["serve", "--event-store", "explicit.sqlite"]) == 0
    assert main(["purge-events", "--event-store", "explicit.sqlite", "--batch-size", "1"]) == 0
    assert calls == [("explicit.sqlite", True), ("explicit.sqlite", False)]
    assert json.loads(capsys.readouterr().out)["expired_pending"] == 0


def test_cli_does_not_print_storage_constructor_errors(monkeypatch, capsys, cli_environment):
    from api_migration_proxy.cli import main

    def fail(*args, **kwargs):
        raise TimeoutError("postgresql://operator:sensitive-password@private-db/events")

    monkeypatch.setenv("API_PROXY_EVENT_STORE_BACKEND", "postgresql")
    monkeypatch.setenv(
        "API_PROXY_POSTGRES_DSN", "postgresql://operator:sensitive-password@private-db/events"
    )
    monkeypatch.setattr("api_migration_proxy.cli.PostgreSQLEventStore", fail)
    assert main(["serve"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Proxy execution failed; verify configuration and retry.\n"


@pytest.mark.parametrize("fails", [False, True])
def test_cli_postgresql_purge_uses_env_dsn_without_initializing_and_redacts_errors(
    monkeypatch, tmp_path, capsys, cli_environment, fails
):
    from api_migration_proxy.cli import main

    calls = []

    class Store:
        def __init__(self, dsn, *, create):
            calls.append((dsn, create))

        async def purge_expired(self, *, batch_size):
            calls.append(batch_size)
            if fails:
                raise TimeoutError("sensitive-password DB operation timed out")
            return {"details_deleted": 1, "summaries_deleted": 2, "expired_pending": 3}

        async def close(self):
            calls.append("closed")

    monkeypatch.setattr("api_migration_proxy.cli.PostgreSQLEventStore", Store)
    env = tmp_path / "postgres.env"
    env.write_text(
        "API_PROXY_EVENT_STORE_BACKEND=postgresql\n"
        "API_PROXY_POSTGRES_DSN=postgresql://operator:sensitive-password@private-db/events\n"
    )
    assert main(["purge-events", "--env-file", str(env), "--batch-size", "7"]) == (
        2 if fails else 0
    )
    output = capsys.readouterr()
    assert calls == [
        ("postgresql://operator:sensitive-password@private-db/events", False),
        7,
        "closed",
    ]
    assert "sensitive-password" not in output.out + output.err
    if fails:
        assert output.out == ""
        assert output.err == "Event cleanup failed; verify the store and retry.\n"
    else:
        assert json.loads(output.out) == {
            "details_deleted": 1,
            "summaries_deleted": 2,
            "expired_pending": 3,
        }


@pytest.mark.parametrize(
    "absolute,body_value,expected",
    [
        ("0.099999999999999999999", "0.1", "different"),
        ('"0.099999999999999999999"', "0.1", "different"),
        ("0.100000000000000000001", "0.100000000000000000001", "matched"),
        ('"0.100000000000000000001"', "0.100000000000000000001", "matched"),
        ("0.1", "0.1", "matched"),
        ("1", "1", "matched"),
    ],
)
def test_json_tolerance_preserves_exact_comparison_boundary(
    tmp_path, absolute, body_value, expected
):
    from api_migration_proxy.comparison.engine import BackendResponse, ComparisonContext, compare
    from api_migration_proxy.settings import load_settings

    data = example_settings()
    data["comparison_policies"][0]["tolerances"] = [
        {"path": "/id", "absolute": "TOLERANCE_LITERAL"}
    ]
    data["snapshot"]["budgets"]["serving_timeout_seconds"] = 0.25
    data["snapshot"]["routes"][0]["v2_serve_ratio"] = 0.5
    data["work_limits"]["compare_timeout_seconds"] = 0.25
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data).replace('"TOLERANCE_LITERAL"', absolute))

    settings = load_settings(path)
    policy = next(iter(settings.comparison_policies.values()))
    result = compare(
        BackendResponse("v1", "serving", "http_response", "success", 200, body=b'{"id":0}'),
        BackendResponse(
            "v2",
            "shadow",
            "http_response",
            "success",
            200,
            body=f'{{"id":{body_value}}}'.encode(),
        ),
        policy,
        ComparisonContext(True, True, True),
    )

    assert result.result == expected
    assert type(settings.snapshot.budgets.serving_timeout_seconds) is float
    assert type(settings.snapshot.routes[0].v2_serve_ratio) is float
    assert type(settings.work_limits.compare_timeout_seconds) is float
    assert type(settings.collection_limits.retry_delay_seconds) is float


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "nonfinite",
        "missing",
        "unknown",
        "policy",
        "active_without_policy",
        "stopped_without_policy",
        "overflow",
        "boolean",
        "rule_field",
    ],
)
def test_cli_rejects_invalid_configuration_without_values_in_output(
    tmp_path, capsys, case, cli_environment
):
    from api_migration_proxy.cli import main

    data = example_settings()
    if case == "missing":
        del data["snapshot"]["budgets"]["serving_timeout_seconds"]
    elif case == "unknown":
        data["unexpected"] = "sensitive value"
    elif case == "policy":
        data["comparison_policies"] = []
    elif case in {"active_without_policy", "stopped_without_policy"}:
        route = data["snapshot"]["routes"][0]
        route["rollout_enabled"] = True
        route["shadow"].update(
            eligible=True,
            sample_ratio=1,
            stopped=case == "stopped_without_policy",
            review_ref="synthetic-read-only-fixture",
        )
        route["comparison_policy_revision"] = None
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


def test_cli_defaults_event_store_and_requires_valid_port(monkeypatch, tmp_path, cli_environment):
    from api_migration_proxy.cli import main

    calls = []
    monkeypatch.setattr(
        "api_migration_proxy.cli.run_server", lambda runtime, options: calls.append(options) or 0
    )
    path = Path(__file__).parents[1] / "fixtures/proxy.json"
    assert main(["serve", "--config", str(path), "--port", "8080"]) == 0
    assert calls[0].event_store == "events.sqlite"
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(SystemExit) as error:
        main(["serve", "--config", "example.json", "--port", "65536", "--event-store", ":memory:"])
    assert error.value.code == 2


def test_cli_local_serving_does_not_enable_access_logs_or_forwarded_headers(
    monkeypatch, tmp_path, cli_environment
):
    from api_migration_proxy.cli import main

    calls = []
    monkeypatch.setattr(
        "api_migration_proxy.server.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    monkeypatch.setenv("API_PROXY_CONTROL_ENABLED", "false")
    path = Path(__file__).parents[1] / "fixtures/proxy.json"
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
    assert options["timeout_graceful_shutdown"] == 5
    assert list(tmp_path.iterdir()) == []


def test_cli_explicit_options_override_environment_and_selected_env_file(
    monkeypatch, tmp_path, cli_environment
):
    from api_migration_proxy.cli import main

    env_file = tmp_path / "chosen.env"
    env_file.write_text("API_PROXY_CONTROL_ENABLED=false\nAPI_PROXY_PORT=8082\n")
    monkeypatch.setenv("API_PROXY_CONFIG", "missing-sensitive-file.json")
    monkeypatch.setenv("API_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("API_PROXY_PORT", "invalid")
    monkeypatch.setenv("API_PROXY_EVENT_STORE", "environment.sqlite")
    calls = []
    monkeypatch.setattr(
        "api_migration_proxy.cli.run_server",
        lambda runtime, options: calls.append((runtime, options)) or 0,
    )
    config = Path(__file__).parents[1] / "fixtures/proxy.json"
    assert (
        main(
            [
                "serve",
                "--env-file",
                str(env_file),
                "--config",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                "8083",
                "--event-store",
                "explicit.sqlite",
            ]
        )
        == 0
    )
    runtime, options = calls[0]
    assert runtime.config.current.revision == "configuration-fixture-1"
    assert options.host == "127.0.0.1"
    assert options.port == 8083
    assert options.event_store == "explicit.sqlite"
    assert options.control_enabled is False


@pytest.mark.parametrize("mode", ["request", "user", "session", "tenant"])
def test_cli_explain_reports_effective_capabilities_without_secrets(
    monkeypatch, tmp_path, capsys, cli_environment, mode
):
    from api_migration_proxy.cli import main

    data = example_settings()
    route = data["snapshot"]["routes"][0]
    route.update(rollout_enabled=True, v2_serve_ratio=1)
    route["cohort"].update(mode=mode, key_source="request" if mode == "request" else "identity")
    route["shadow"].update(eligible=True, sample_ratio=1, review_ref="approved-review")
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(data))
    monkeypatch.setenv("API_PROXY_CONTROL_TOKEN", "private-test-token")
    assert main(["check-config", "--config", str(config), "--explain"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output.splitlines()[1])
    assert report["scope"] == "configuration_only"
    assert report["mode"] == "json"
    assert report["routes"][0]["cohort_mode"] == mode
    assert report["routes"][0]["effective_v2_serve_ratio"] == (1 if mode == "request" else 0)
    assert report["routes"][0]["shadow_enabled"] is (mode == "request")
    assert bool(report["routes"][0]["warnings"]) is (mode != "request")
    assert not any(report["capabilities"].values())
    for value in ("v1.example.invalid", "v2.example.invalid", "fixture-salt", "private-test-token"):
        assert value not in output


async def test_internal_status_excludes_backend_addresses_and_credentials(
    runtime_factory, backend_factory
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2)
    snapshot = harness.runtime.config.current
    route = snapshot.routes[0]
    state = harness.runtime.observation_status()
    assert state["scope"] == "current_process"
    assert set(state["worker_revisions"].values()) == {snapshot.revision}
    serialized = json.dumps(state)
    for value in (route.v1, route.v2, route.cohort.salt):
        assert value not in serialized


@pytest.mark.parametrize("policy_case", ["configured", "zero_without_policy", "stopped_zero"])
def test_cli_process_forwards_real_loopback_http_and_shuts_down(
    monkeypatch, tmp_path, policy_case, cli_environment
):
    monkeypatch.setenv("API_PROXY_CONTROL_ENABLED", "false")
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
        if policy_case != "configured":
            route = data["snapshot"]["routes"][0]
            route["rollout_enabled"] = True
            route["shadow"].update(
                eligible=True,
                sample_ratio=0,
                stopped=policy_case == "stopped_zero",
                review_ref="synthetic-read-only-fixture",
            )
            route["comparison_policy_revision"] = None
            data["comparison_policies"] = []
        config = tmp_path / "synthetic.json"
        config.write_text(json.dumps(data))
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_migration_proxy.cli",
                "check-config",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert checked.returncode == 0
        assert checked.stdout == "Configuration valid.\n"
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
            assert response.headers.get_list("server") == [
                f"{Backend.server_version} {Backend.sys_version}"
            ]
            assert len(response.headers.get_list("date")) == 1
            assert client.get("/docs").json() == {"source": "synthetic-v1"}
            assert client.get("/items/registered").json() == {"source": "synthetic-v1"}
            for path in ("/items/a%0Ab?key=1&key=2", "/unregistered/a%0Ab"):
                response = client.get(path)
                assert response.status_code == 200
                assert response.content == b'{"source":"synthetic-v1"}'
        assert requests == [
            "/items/a%2Fb?key=1&key=2",
            "/docs",
            "/items/registered",
            "/items/a%0Ab?key=1&key=2",
            "/unregistered/a%0Ab",
        ]
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
