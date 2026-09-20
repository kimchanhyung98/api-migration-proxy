import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import datetime

import httpx
import pytest

from api_migration_proxy.collection import BatchResult, DetailPolicy, EventQuery, QueryAccess
from api_migration_proxy.comparison import compare
from api_migration_proxy.config import ConfigurationError, ShadowPolicy
from api_migration_proxy.runtime import IncompleteResponse


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def pipeline(harness, step):
    return harness.metrics.value("comparison_pipeline_total", route="catalog", step=step)


@pytest.mark.parametrize("status,body", [(200, b'{"ok":true}'), (404, b'{"missing":true}')])
async def test_t02_disabled_rollout_calls_only_v1_and_preserves_contract(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
    status,
    body,
):
    async def backend(_, writer):
        await respond(writer, body, status=status, headers=((b"etag", b'"original"'),))

    v1, v2 = await backend_factory(backend), await backend_factory()
    harness = await runtime_factory(v1, v2, route_values={"rollout_enabled": False})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == status
    assert exchange.body == body
    assert (b"etag", b'"original"') in exchange.messages[0]["headers"]
    assert len(v1.requests) == 1
    assert not v2.requests
    assert await harness.records() == []
    assert pipeline(harness, "eligible") == 0


async def test_t04_fast_shadow_never_replaces_selected_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def serving(_, writer):
        await release.wait()
        await respond(writer, b'{"source":"v1"}')

    async def shadow(_, writer):
        await respond(writer, b'{"source":"v2"}')

    v1, v2 = await backend_factory(serving), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2)
    exchange = asgi_request(harness.runtime)
    await v2.received.wait()
    await eventually(
        lambda: (
            harness.metrics.value(
                "backend_completed_total",
                route="catalog",
                backend="v2",
                role="shadow",
                outcome="http_response",
                contract_class="success",
            )
            == 1
        )
    )
    assert not exchange.messages
    release.set()
    await exchange.wait()
    await harness.runtime.flush()
    assert exchange.body == b'{"source":"v1"}'
    assert len(v1.requests) == len(v2.requests) == 1
    assert (await harness.records())[0]["comparison"]["result"] == "different"


async def test_t09_post_body_raw_path_query_and_headers_duplicate_without_mutation(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, route_values={"method": "POST"})
    headers = (
        (b"authorization", b"Bearer request-local"),
        (b"cookie", b"session=current"),
        (b"content-type", b"application/json"),
        (b"x-tenant", b"tenant-a"),
        (b"connection", b"x-remove"),
        (b"x-remove", b"private-hop"),
    )
    chunks = (b'{"value":', b' "unchanged", ', b'"n":1.00}')
    exchange = await asgi_request(
        harness.runtime,
        method="POST",
        path=b"/catalog/a%20b",
        query=b"k=1&k=2&x=%2B",
        headers=headers,
        chunks=chunks,
    ).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    for backend in (v1, v2):
        assert len(backend.requests) == 1
        request = backend.requests[0]
        assert request.body == b"".join(chunks)
        assert request.target == b"/catalog/a%20b?k=1&k=2&x=%2B"
        assert request.method == b"POST"
        fields = dict(request.headers)
        assert fields[b"authorization"] == b"Bearer request-local"
        assert fields[b"cookie"] == b"session=current"
        assert fields[b"x-tenant"] == b"tenant-a"
        assert b"x-remove" not in fields
        assert fields[b"host"] == backend.url.removeprefix("http://").encode()


async def test_t13_slow_shadow_and_database_collection_finish_after_user_response(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def shadow(_, writer):
        await release.wait()
        await respond(writer)

    v1, v2 = await backend_factory(), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2)
    exchange = await asgi_request(harness.runtime).wait()
    assert exchange.status == 200
    assert not release.is_set()
    assert await harness.records() == []
    assert pipeline(harness, "selected") == pipeline(harness, "dispatched") == 1
    assert pipeline(harness, "terminal") == 0
    release.set()
    await harness.runtime.flush()
    rows = await harness.records()
    assert len(rows) == 1
    assert rows[0]["comparison"]["result"] == "matched"
    assert pipeline(harness, "terminal") == pipeline(harness, "stored") == 1


async def test_t14_shadow_timeout_cannot_finalize_before_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def serving(_, writer):
        await release.wait()
        await respond(writer)

    async def shadow(_, writer):
        await asyncio.Event().wait()

    v1, v2 = await backend_factory(serving), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2, budget_values={"shadow_timeout_seconds": 0.05})
    exchange = asgi_request(harness.runtime)
    await eventually(
        lambda: (
            harness.metrics.value(
                "backend_completed_total",
                route="catalog",
                backend="v2",
                role="shadow",
                outcome="timeout",
                contract_class="unknown",
            )
            == 1
        )
    )
    assert not exchange.task.done()
    assert pipeline(harness, "terminal") == 0
    assert await harness.records() == []
    release.set()
    await exchange.wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "execution_error"
    assert record["backends"]["v2"]["execution_outcome"] == "timeout"
    assert record["backends"]["v1"]["execution_outcome"] == "http_response"


async def test_t14_disconnect_after_normal_completion_keeps_existing_shadow(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def shadow(_, writer):
        await release.wait()
        await respond(writer)

    v1, v2 = await backend_factory(), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2)
    exchange = await asgi_request(harness.runtime).wait()
    exchange.disconnect()
    release.set()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert record["request_outcome"] == "completed"
    assert record["backends"]["v2"]["execution_outcome"] == "http_response"
    assert record["comparison"]["result"] == "matched"


async def test_t14_disconnect_emitted_by_final_send_does_not_cancel_shadow(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def shadow(_, writer):
        await release.wait()
        await respond(writer)

    async def final_send(message):
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            exchange.disconnect()

    v1, v2 = await backend_factory(), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2)
    exchange = asgi_request(harness.runtime, on_send=final_send)
    await exchange.wait()
    release.set()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert record["request_outcome"] == "completed"
    assert record["backends"]["v2"]["execution_outcome"] == "http_response"


async def test_t14_disconnect_during_response_cancels_both_attempts(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    async def serving(_, writer):
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\npartial")
        await writer.drain()
        await asyncio.Event().wait()

    async def shadow(_, writer):
        await asyncio.Event().wait()

    v1, v2 = await backend_factory(serving), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2)
    exchange = asgi_request(harness.runtime)
    await eventually(lambda: exchange.body == b"partial")
    exchange.disconnect()
    await exchange.wait()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert record["request_outcome"] == "cancelled"
    assert all(item["execution_outcome"] == "cancelled" for item in record["backends"].values())
    assert not any(message.get("more_body") is False for message in exchange.messages)
    assert all(
        harness.metrics.value("backend_inflight", backend=backend, role=role) == 0
        for backend, role in (("v1", "serving"), ("v2", "shadow"))
    )


async def test_t16_shadow_slot_limit_preserves_selected_denominator_and_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def shadow(_, writer):
        await release.wait()
        await respond(writer)

    v1, v2 = await backend_factory(), await backend_factory(shadow)
    harness = await runtime_factory(v1, v2, budget_values={"shadow_max_inflight": 1})
    first = await asgi_request(harness.runtime).wait()
    second = await asgi_request(harness.runtime, path=b"/catalog/2").wait()
    await eventually(lambda: pipeline(harness, "stored") == 1)
    assert first.status == second.status == 200
    assert len(v1.requests) == 2
    assert len(v2.requests) == 1
    assert pipeline(harness, "selected") == 2
    assert pipeline(harness, "dispatched") == 1
    assert pipeline(harness, "terminal") == 0
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "not_executed"
    assert record["backends"]["v2"]["reason"] == "slot_exhausted"
    release.set()
    await harness.runtime.flush()
    assert pipeline(harness, "terminal") == 1
    assert pipeline(harness, "stored") == 2


async def test_t17_unknown_length_over_capture_cap_preserves_prefix_and_remaining_input(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"method": "POST"},
        budget_values={"request_capture_limit_bytes": 4},
    )
    chunks = (b"abc", b"def", b"ghi", b"jkl")
    exchange = await asgi_request(harness.runtime, method="POST", chunks=chunks).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert v1.requests[0].body == b"".join(chunks)
    assert len(v1.requests) == 1
    assert not v2.requests
    assert pipeline(harness, "selected") == 1
    assert pipeline(harness, "dispatched") == pipeline(harness, "terminal") == 0
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "not_executed"
    assert record["backends"]["v2"]["reason"] == "request_oversized"


async def test_t17_oversized_response_still_delivered_and_never_parsed_as_complete(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    body = b'{"long":"' + b"x" * 64 + b'"}'

    async def backend(_, writer):
        await respond(writer, body)

    v1, v2 = await backend_factory(backend), await backend_factory(backend)
    harness = await runtime_factory(v1, v2, budget_values={"response_capture_limit_bytes": 8})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.body == body
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "not_comparable"
    assert record["comparison"]["reason"] == "capture_oversized"
    assert record["backends"]["v1"]["capture_state"] == "oversized"


async def test_t29_backend_completion_precedes_slow_client_and_final_summary_waits(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    release = asyncio.Event()

    async def slow_send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await release.wait()

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2)
    exchange = asgi_request(harness.runtime, on_send=slow_send)
    await eventually(
        lambda: (
            harness.metrics.value(
                "backend_completed_total",
                route="catalog",
                backend="v1",
                role="serving",
                outcome="http_response",
                contract_class="success",
            )
            == 1
        )
    )
    assert not exchange.task.done()
    assert await harness.records() == []
    assert pipeline(harness, "terminal") == 0
    await asyncio.sleep(0.02)
    release.set()
    await exchange.wait()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert record["request_outcome"] == "completed"
    assert datetime.fromisoformat(record["backend_completed_at"]) < datetime.fromisoformat(
        record["request_ended_at"]
    )
    backend_time = harness.metrics.value(
        "backend_duration_seconds", route="catalog", backend="v1", role="serving"
    ).total
    user_time = harness.metrics.value(
        "proxy_request_duration_seconds", route="catalog", serving="v1"
    ).total
    assert user_time > backend_time


async def test_t32_revision_snapshot_preserves_inflight_roles_and_changes_new_requests(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def v1_handler(request, writer):
        if request.target == b"/catalog/1":
            await release.wait()
        await respond(writer, b'{"version":1}')

    async def v2_handler(_, writer):
        await respond(writer, b'{"version":2}')

    v1, v2 = await backend_factory(v1_handler), await backend_factory(v2_handler)
    harness = await runtime_factory(v1, v2)
    first = asgi_request(harness.runtime)
    await v1.received.wait()
    old = harness.config.current
    new = replace(
        old,
        revision="config-2",
        previous_revision=old.revision,
        routes=(replace(old.routes[0], v2_serve_ratio=1),),
    )
    harness.config.apply(new, actor="test-operator", expected_revision=old.revision)
    second = await asgi_request(harness.runtime, path=b"/catalog/2").wait()
    release.set()
    await first.wait()
    await harness.runtime.flush()
    assert first.body == b'{"version":1}'
    assert second.body == b'{"version":2}'
    records = {record["configuration_revision"]: record for record in await harness.records()}
    assert records["config-1"]["backends"]["v1"]["role"] == "serving"
    assert records["config-2"]["backends"]["v1"]["role"] == "shadow"
    assert records["config-1"]["epoch_id"] != records["config-2"]["epoch_id"]
    assert len(v1.requests) == len(v2.requests) == 2


async def test_t32_stopping_shadow_does_not_change_v2_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    async def v2_handler(_, writer):
        await respond(writer, b'{"version":2}')

    v1, v2 = await backend_factory(), await backend_factory(v2_handler)
    harness = await runtime_factory(
        v1,
        v2,
        route_values={
            "v2_serve_ratio": 1,
            "shadow": ShadowPolicy(True, 1, "synthetic-safe-fixture", True),
        },
    )
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.body == b'{"version":2}'
    assert not v1.requests
    assert len(v2.requests) == 1
    assert pipeline(harness, "eligible") == 1
    assert pipeline(harness, "selected") == 0


async def test_t42_drip_response_is_bounded_by_total_deadline_without_replacement_body(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    async def drip(_, writer):
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n")
        for _ in range(100):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.01)

    v1, v2 = await backend_factory(drip), await backend_factory()
    harness = await runtime_factory(v1, v2, budget_values={"serving_timeout_seconds": 0.08})
    exchange = asgi_request(harness.runtime)
    with pytest.raises(IncompleteResponse):
        await exchange.wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert 0 < len(exchange.body) < 100
    assert not any(message.get("more_body") is False for message in exchange.messages)
    record = (await harness.records())[0]
    assert record["request_outcome"] == "incomplete"
    assert record["backends"]["v1"]["execution_outcome"] == "timeout"
    assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    assert len(v1.requests) == len(v2.requests) == 1


@pytest.mark.parametrize(
    "failure,expected_status", [("disconnect", 502), ("timeout", 504), ("server_error", 503)]
)
async def test_t12_serving_failure_never_retries_or_falls_back_to_healthy_shadow(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
    failure,
    expected_status,
):
    async def failing(_, writer):
        if failure == "timeout":
            await asyncio.Event().wait()
        elif failure == "server_error":
            await respond(writer, b'{"backend":"unavailable"}', status=503)

    v1, v2 = await backend_factory(failing), await backend_factory()
    harness = await runtime_factory(v1, v2, budget_values={"serving_timeout_seconds": 0.05})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == expected_status
    assert len(v1.requests) == len(v2.requests) == 1
    assert exchange.body != b'{"ok":true}'
    if failure == "server_error":
        assert exchange.body == b'{"backend":"unavailable"}'
    record = (await harness.records())[0]
    assert record["serving_backend"] == "v1"
    assert record["response_source"] == ("backend" if failure == "server_error" else "proxy")
    assert record["comparison"]["result"] == "execution_error"


async def test_t43_shared_pool_does_not_replay_cookie_or_authorization_state(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    async def v1_handler(_, writer):
        await respond(
            writer, headers=((b"set-cookie", b"serving=one"), (b"set-cookie", b"other=two"))
        )

    async def v2_handler(_, writer):
        await respond(writer, headers=((b"set-cookie", b"shadow=never-replay"),))

    v1, v2 = await backend_factory(v1_handler), await backend_factory(v2_handler)
    harness = await runtime_factory(v1, v2)
    first = await asgi_request(
        harness.runtime, headers=((b"cookie", b"caller=a"), (b"authorization", b"Bearer A"))
    ).wait()
    await harness.runtime.flush()
    second = asgi_request(
        harness.runtime,
        path=b"/catalog/2",
        headers=((b"cookie", b"serving=one"), (b"authorization", b"Bearer B")),
    )
    third = asgi_request(harness.runtime, path=b"/catalog/3")
    await asyncio.gather(second.wait(), third.wait())
    await harness.runtime.flush()
    assert [value for key, value in first.messages[0]["headers"] if key == b"set-cookie"] == [
        b"serving=one",
        b"other=two",
    ]
    for backend in (v1, v2):
        requests = {request.target: dict(request.headers) for request in backend.requests}
        assert requests[b"/catalog/1"][b"cookie"] == b"caller=a"
        assert requests[b"/catalog/1"][b"authorization"] == b"Bearer A"
        assert requests[b"/catalog/2"][b"cookie"] == b"serving=one"
        assert requests[b"/catalog/2"][b"authorization"] == b"Bearer B"
        assert b"cookie" not in requests[b"/catalog/3"]
        assert b"authorization" not in requests[b"/catalog/3"]
        assert all(
            b"shadow=never-replay" not in value
            for fields in requests.values()
            for value in fields.values()
        )


async def test_t14_upload_disconnect_finishes_without_waiting_for_serving_deadline(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={
            "method": "POST",
            "shadow": ShadowPolicy(False, 0),
        },
    )
    exchange = asgi_request(harness.runtime, method="POST", chunks=())
    exchange.incoming.put_nowait({"type": "http.request", "body": b"prefix", "more_body": True})
    await eventually(
        lambda: (
            harness.metrics.value(
                "backend_started_total",
                route="catalog",
                backend="v1",
                role="serving",
            )
            == 1
        )
    )
    exchange.disconnect()
    async with asyncio.timeout(0.5):
        await exchange.wait()
    assert not exchange.messages
    assert (
        harness.metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="v1",
            outcome="cancelled",
            source="none",
        )
        == 1
    )
    assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    assert not v2.requests


async def test_t36_shutdown_inflight_request_drains_new_pair_within_grace_and_no_orphans(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    async def hung(_, writer):
        await asyncio.Event().wait()

    v1, v2 = await backend_factory(hung), await backend_factory(hung)
    harness = await runtime_factory(v1, v2, budget_values={"shutdown_grace_seconds": 0.01})
    exchange = asgi_request(harness.runtime)
    await asyncio.gather(v1.received.wait(), v2.received.wait())
    started = time.monotonic()
    async with asyncio.timeout(0.5):
        await harness.close()
    assert time.monotonic() - started < 0.5
    assert exchange.task.done()
    status = harness.runtime.observation_status()
    assert status["ready"] is False
    assert status["active_requests"] == status["pending_pairs"] == status["comparison_jobs"] == 0
    assert status["comparison_bytes"] == 0
    assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    assert harness.metrics.value("backend_inflight", backend="v2", role="shadow") == 0
    assert pipeline(harness, "selected") == pipeline(harness, "dispatched") == 1


async def test_t42_http_client_read_timeout_before_headers_maps_to_504_without_fallback(
    backend_factory,
    runtime_factory,
    asgi_request,
    monkeypatch,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2)

    async def read_timeout(*_):
        raise httpx.ReadTimeout("upstream read timeout")

    monkeypatch.setattr(harness.runtime._transports["serving"], "open", read_timeout)
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 504
    record = (await harness.records())[0]
    assert record["backends"]["v1"]["execution_outcome"] == "timeout"
    assert record["backends"]["v2"]["execution_outcome"] == "http_response"
    assert record["serving_backend"] == "v1"
    assert record["response_source"] == "proxy"


async def test_t23_comparison_byte_budget_drop_does_not_delay_or_change_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, work_values={"compare_max_bytes": 1})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert exchange.body == b'{"ok":true}'
    assert pipeline(harness, "selected") == pipeline(harness, "terminal") == 1
    assert pipeline(harness, "stored") == pipeline(harness, "comparable") == 0
    assert harness.metrics.value("collection_dropped_total", reason="queue_full") == 1
    assert await harness.records() == []
    assert harness.runtime.observation_status()["comparison_bytes"] == 0


async def test_t44_running_comparison_timeout_keeps_slot_until_thread_really_finishes(
    backend_factory,
    runtime_factory,
    asgi_request,
    monkeypatch,
):
    entered, release = threading.Event(), threading.Event()

    def blocked_compare(*args):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test did not release comparison")
        return compare(*args)

    monkeypatch.setattr("api_migration_proxy.runtime.compare", blocked_compare)
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        work_values={
            "compare_max_jobs": 1,
            "compare_timeout_seconds": 0.03,
        },
    )
    try:
        first = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await eventually(
            lambda: harness.metrics.value("collection_dropped_total", reason="timeout") == 1
        )
        second = await asgi_request(harness.runtime, path=b"/catalog/2").wait()
        await eventually(
            lambda: harness.metrics.value("collection_dropped_total", reason="queue_full") == 1
        )
        assert first.status == second.status == 200
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert harness.runtime.observation_status()["comparison_bytes"] > 0
        assert await harness.records() == []
    finally:
        release.set()
    await harness.runtime.flush()
    assert harness.runtime.observation_status()["comparison_jobs"] == 0
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
    assert pipeline(harness, "stored") == 0


async def test_t23_storage_failure_is_independent_of_completed_serving_and_observable(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    entered, release = asyncio.Event(), asyncio.Event()

    class FailedStorage:
        def __init__(self, actual):
            self.actual = actual

        async def write_batch(self, events):
            entered.set()
            await release.wait()
            return BatchResult(failed=frozenset(event.event_id for event in events))

        async def close(self):
            await self.actual.close()

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, store_wrapper=FailedStorage)
    exchange = await asgi_request(harness.runtime).wait()
    await entered.wait()
    assert exchange.status == 200
    assert pipeline(harness, "comparable") == 1
    assert pipeline(harness, "stored") == 0
    release.set()
    await harness.runtime.flush()
    assert harness.metrics.value("collection_dropped_total", reason="retry_exhausted") == 1
    assert harness.runtime.metric_snapshot().counters[("collection_write_failures_total", ())] == 2
    assert pipeline(harness, "selected") == 1
    assert await harness.records() == []


async def test_t25_disabled_detail_sampling_never_calls_provider_or_masker(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    called = []

    def provider(*_):
        called.append("provider")
        return {"value": "raw"}

    def masker(*_):
        called.append("masker")
        return "masked"

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        runtime_values={
            "detail_policy": DetailPolicy(0, 100, 10, ("/value",), masker),
            "detail_provider": provider,
        },
    )
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert exchange.status == 200
    assert not called
    assert record["detail_state"] == "disabled"
    assert record["comparison"]["result"] == "matched"
    assert pipeline(harness, "stored") == 1


async def test_t25_selected_detail_masked_allowlist_written_and_query_permission_enforced(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    calls = []

    def provider(v1, v2, result):
        calls.append((v1.backend, v2.backend, result.result))
        return {"value": "original-sensitive-value", "authorization": "private-secret"}

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        runtime_values={
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), lambda path, value: "redacted"),
            "detail_provider": provider,
        },
    )
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert calls == [("v1", "v2", "matched")]
    now = time.time()
    query = EventQuery(now - 100, now + 1, frozenset({"catalog"}), 100)
    denied = await harness.store.query(query, QueryAccess(frozenset({"catalog"}), 100, 200))
    allowed = await harness.store.query(query, QueryAccess(frozenset({"catalog"}), 100, 200, True))
    assert denied[0]["detail"] is None
    assert allowed[0]["detail"] == {"/value": "redacted"}
    assert allowed[0]["summary"]["comparison"]["result"] == "matched"
    encoded = json.dumps(allowed)
    assert "original-sensitive-value" not in encoded
    assert "private-secret" not in encoded


async def test_t25_detail_provider_exception_keeps_safe_comparison_summary(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    def provider(*_):
        raise ValueError("Bearer private-provider-error")

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        runtime_values={
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), lambda path, value: value),
            "detail_provider": provider,
        },
    )
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    record = (await harness.records())[0]
    assert exchange.status == 200
    assert record["comparison"]["result"] == "matched"
    assert record["detail_state"] == "masking_failed"
    assert "private-provider-error" not in json.dumps(record)
    assert pipeline(harness, "stored") == 1


async def test_t21_http_200_business_error_classification_does_not_change_public_response(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    body = b'{"business_error":"fixture_failure"}'

    async def serving(_, writer):
        await respond(writer, body)

    def classifier(route, response):
        assert route.route_id == "catalog"
        if b"business_error" in response.body:
            return "unexpected_error"
        return "success"

    v1, v2 = await backend_factory(serving), await backend_factory()
    harness = await runtime_factory(v1, v2, runtime_values={"response_classifier": classifier})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert exchange.body == body
    assert len(v1.requests) == len(v2.requests) == 1
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "execution_error"
    assert record["backends"]["v1"]["execution_outcome"] == "http_response"
    assert record["backends"]["v1"]["contract_class"] == "unexpected_error"
    assert (
        harness.metrics.value(
            "backend_completed_total",
            route="catalog",
            backend="v1",
            role="serving",
            outcome="http_response",
            contract_class="unexpected_error",
        )
        == 1
    )


async def test_t21_classifier_exception_leaves_received_http_response_intact_and_unknown(
    backend_factory,
    runtime_factory,
    asgi_request,
):
    def classifier(*_):
        raise ValueError("private-classification-error")

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, runtime_values={"response_classifier": classifier})
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert exchange.body == b'{"ok":true}'
    record = (await harness.records())[0]
    assert record["comparison"]["result"] == "not_comparable"
    assert record["comparison"]["reason"] == "contract_class_unknown"
    assert all(backend["contract_class"] == "unknown" for backend in record["backends"].values())
    assert all(
        backend["execution_outcome"] == "http_response" for backend in record["backends"].values()
    )
    assert "private-classification-error" not in json.dumps(record)
    assert (
        harness.metrics.value(
            "backend_completed_total",
            route="catalog",
            backend="v1",
            role="serving",
            outcome="http_response",
            contract_class="unknown",
        )
        == 1
    )
    assert len(v1.requests) == len(v2.requests) == 1


@pytest.mark.parametrize(
    "invalid_change", ["missing_policy", "resource_budget", "unregistered_route"]
)
async def test_t01_invalid_hot_configuration_keeps_last_snapshot_and_working_serving(
    backend_factory,
    runtime_factory,
    asgi_request,
    invalid_change,
):
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2)
    before = harness.config.current
    updated = replace(before, revision="config-rejected", previous_revision=before.revision)
    if invalid_change == "missing_policy":
        updated = replace(
            updated, routes=(replace(before.routes[0], comparison_policy_revision="missing"),)
        )
    elif invalid_change == "resource_budget":
        updated = replace(updated, budgets=replace(before.budgets, serving_max_inflight=100))
    else:
        updated = replace(
            updated, routes=(replace(before.routes[0], route_id="unknown-new-route"),)
        )
    history = harness.config.history
    with pytest.raises(ConfigurationError):
        harness.config.apply(updated, actor="test-operator", expected_revision=before.revision)
    assert harness.config.current is before
    assert harness.config.history == history
    assert harness.runtime.ready
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    assert exchange.status == 200
    assert (await harness.records())[0]["configuration_revision"] == before.revision


async def test_t16_overloaded_registered_route_reports_rejection_without_fake_assignment(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
):
    release = asyncio.Event()

    async def slow(_, writer):
        await release.wait()
        await respond(writer)

    v1, v2 = await backend_factory(slow), await backend_factory()
    harness = await runtime_factory(v1, v2, budget_values={"serving_max_inflight": 1})
    first = asgi_request(harness.runtime)
    await v1.received.wait()
    second = await asgi_request(harness.runtime, path=b"/catalog/2").wait()
    assert second.status == 503
    assert (
        harness.metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="unknown",
            outcome="rejected",
            source="proxy",
        )
        == 1
    )
    assert (
        harness.metrics.value(
            "proxy_request_duration_seconds", route="catalog", serving="unknown"
        ).count
        == 1
    )
    assert harness.metrics.value("proxy_assignments_total", route="catalog", serving="v1") == 1
    assert (
        harness.metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="v1",
            outcome="completed",
            source="backend",
        )
        == 0
    )
    assert len(v1.requests) == 1
    release.set()
    await first.wait()
    await harness.runtime.flush()
    assert (
        harness.metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="v1",
            outcome="completed",
            source="backend",
        )
        == 1
    )
