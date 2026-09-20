import asyncio
import threading

import pytest

from api_migration_proxy import runtime as runtime_module


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def blocked_comparator(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = runtime_module.compare

    def compare(*args):
        calls.append(1)
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release comparator")
        return original(*args)

    monkeypatch.setattr(runtime_module, "compare", compare)
    yield entered, release, calls
    release.set()


def dropped(harness, reason):
    return harness.metrics.value("collection_dropped_total", reason=reason)


async def test_t44_timeout_keeps_real_thread_slot_and_bytes_without_delaying_serving(
    backend_factory, runtime_factory, asgi_request, blocked_comparator
):
    entered, release, calls = blocked_comparator
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1, v2, work_values={"compare_max_jobs": 1, "compare_timeout_seconds": 0.03}
    )
    try:
        first = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await eventually(lambda: dropped(harness, "timeout") == 1)
        retained = harness.runtime.observation_status()
        assert first.status == 200
        assert first.body == b'{"ok":true}'
        assert retained["comparison_jobs"] == 1
        assert retained["comparison_bytes"] > 0
        second = await asgi_request(harness.runtime).wait()
        await eventually(lambda: dropped(harness, "queue_full") == 1)
        assert second.status == 200
        assert second.body == b'{"ok":true}'
        assert not release.is_set()
        assert len(calls) == 1
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert (
            harness.runtime.observation_status()["comparison_bytes"] == retained["comparison_bytes"]
        )
        assert await harness.records() == []
    finally:
        release.set()
        await harness.runtime.flush()
    assert harness.runtime.observation_status()["comparison_jobs"] == 0
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
    assert await harness.records() == []
    assert dropped(harness, "timeout") == 1
    assert len(v1.requests) == len(v2.requests) == 2


async def test_t23_job_budget_counts_running_and_queued_work(
    backend_factory, runtime_factory, asgi_request, blocked_comparator
):
    entered, release, calls = blocked_comparator
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, work_values={"compare_max_jobs": 2})
    try:
        first = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        second = await asgi_request(harness.runtime).wait()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 2)
        third = await asgi_request(harness.runtime).wait()
        await eventually(lambda: dropped(harness, "queue_full") == 1)
        assert all(exchange.status == 200 for exchange in (first, second, third))
        assert all(exchange.body == b'{"ok":true}' for exchange in (first, second, third))
        assert harness.runtime.observation_status()["comparison_jobs"] == 2
        assert len(calls) == 1
        assert await harness.records() == []
    finally:
        release.set()
        await harness.runtime.flush()
    records = await harness.records()
    assert len(records) == 2
    assert all(record["comparison"]["result"] == "matched" for record in records)
    assert harness.runtime.observation_status()["comparison_jobs"] == 0
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
    assert dropped(harness, "queue_full") == 1


async def test_t23_byte_budget_includes_active_capture_and_rejects_another_pair(
    backend_factory, runtime_factory, asgi_request, blocked_comparator, respond
):
    entered, release, calls = blocked_comparator
    body = b'{"data":"' + b"x" * 3900 + b'"}'

    async def backend(_, writer):
        await respond(writer, body)

    v1, v2 = await backend_factory(backend), await backend_factory(backend)
    harness = await runtime_factory(v1, v2, work_values={"compare_max_bytes": 20_000})
    try:
        first = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        retained = harness.runtime.observation_status()["comparison_bytes"]
        assert retained <= 20_000 < 2 * retained
        second = await asgi_request(harness.runtime).wait()
        await eventually(lambda: dropped(harness, "queue_full") == 1)
        assert first.body == second.body == body
        assert first.status == second.status == 200
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert harness.runtime.observation_status()["comparison_bytes"] == retained
        assert len(calls) == 1
    finally:
        release.set()
        await harness.runtime.flush()
    assert len(await harness.records()) == 1
    assert harness.runtime.observation_status()["comparison_bytes"] == 0


async def test_t23_aged_queue_entry_is_dropped_before_starting_computation(
    backend_factory, runtime_factory, asgi_request, blocked_comparator
):
    entered, release, calls = blocked_comparator
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, work_values={"compare_max_age_seconds": 0.05})
    try:
        await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await asgi_request(harness.runtime).wait()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 2)
        await eventually(lambda: dropped(harness, "timeout") == 1)
        await asyncio.sleep(0.06)
    finally:
        release.set()
        await harness.runtime.flush()
    assert len(calls) == 1
    assert dropped(harness, "timeout") == 1
    assert dropped(harness, "queue_expired") == 1
    assert await harness.records() == []
    assert harness.runtime.observation_status()["comparison_jobs"] == 0
    assert harness.runtime.observation_status()["comparison_bytes"] == 0


async def test_t44_shutdown_grace_returns_with_real_thread_resources_still_observable(
    backend_factory, runtime_factory, asgi_request, blocked_comparator
):
    entered, release, _ = blocked_comparator
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, budget_values={"shutdown_grace_seconds": 0.03})
    try:
        exchange = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        retained = harness.runtime.observation_status()["comparison_bytes"]
        async with asyncio.timeout(1):
            await harness.close()
        status = harness.runtime.observation_status()
        assert exchange.status == 200
        assert not release.is_set()
        assert status["ready"] is False
        assert status["comparison_jobs"] == 1
        assert status["comparison_bytes"] == retained > 0
        assert dropped(harness, "shutdown") >= 1
    finally:
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
