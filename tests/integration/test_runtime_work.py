import asyncio
import gc
import threading
import time
import weakref

import pytest

from api_migration_proxy.collection.events import DetailPolicy, make_event
from api_migration_proxy.collection.retention import RetentionWorker
from api_migration_proxy.proxy import pipeline as processing_module


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def blocked_comparator(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = processing_module.compare

    def compare(*args):
        calls.append(1)
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release comparator")
        return original(*args)

    monkeypatch.setattr(processing_module, "compare", compare)
    yield entered, release, calls
    release.set()


def dropped(harness, reason):
    return harness.metrics.value("collection_dropped_total", reason=reason)


async def test_retention_maintenance_follows_runtime_lifecycle(
    backend_factory, runtime_factory, monkeypatch
):
    calls = []

    class Maintenance:
        def start(self):
            calls.append("start")

        async def close(self, timeout):
            assert not harness.runtime.ready
            assert 0 <= timeout <= 0.1
            calls.append("maintenance_close")

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        budget_values={"shutdown_grace_seconds": 0.1},
        runtime_values={"maintenance": Maintenance()},
    )
    original_close = harness.store.close

    async def close_store():
        calls.append("store_close")
        await original_close()

    monkeypatch.setattr(harness.store, "close", close_store)
    harness.store.counters["expired_pending"] = 7
    await harness.runtime.start()
    assert calls == ["start"]
    assert harness.runtime.observation_status()["storage_maintenance"]["expired_pending"] == 7
    await harness.close()
    await harness.runtime.close()
    assert calls == ["start", "maintenance_close", "store_close"]


async def test_runtime_retention_deletes_expired_rows_without_request_traffic(
    backend_factory, runtime_factory
):
    runtime_values = {}

    def attach_maintenance(store):
        runtime_values["maintenance"] = RetentionWorker(store, interval_seconds=0.01, batch_size=1)
        return store

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        store_wrapper=attach_maintenance,
        runtime_values=runtime_values,
    )
    expired = make_event({"route_id": "catalog"}, now=1, retention_seconds=1)
    retained = make_event({"route_id": "catalog"}, now=time.time(), retention_seconds=60)
    await harness.store.write_batch([expired, retained])
    async with asyncio.timeout(2):
        while True:
            rows = await harness.store._run(
                lambda: (
                    harness.store._db().execute("SELECT event_id FROM comparison_event").fetchall()
                )
            )
            if rows == [(retained.event_id,)]:
                break
            await asyncio.sleep(0.005)
    assert not v1.requests and not v2.requests
    assert harness.runtime.observation_status()["storage_maintenance"]["expired_pending"] == 0


async def test_pending_sqlite_retention_does_not_block_serving_or_extend_shutdown(
    backend_factory, runtime_factory, asgi_request, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    runtime_values = {}

    def attach_maintenance(store):
        original_db = store._db

        def delayed_db():
            db = original_db()
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release retention operation")
            return db

        monkeypatch.setattr(store, "_db", delayed_db)
        runtime_values["maintenance"] = RetentionWorker(store, interval_seconds=0.01, batch_size=1)
        return store

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"rollout_enabled": False},
        budget_values={"shutdown_grace_seconds": 0.03},
        store_wrapper=attach_maintenance,
        runtime_values=runtime_values,
    )
    try:
        await eventually(entered.is_set)
        async with asyncio.timeout(0.5):
            assert (await asgi_request(harness.runtime).wait()).status == 200
            await harness.close()
            await harness.runtime.close()
        assert not release.is_set()
        assert not harness.runtime.ready
        assert harness.collector.metrics()["completeness_known"] is False
        assert "expired_pending" not in harness.runtime.observation_status()["storage_maintenance"]
    finally:
        release.set()
        await harness.store.close()
    assert harness.runtime.observation_status()["storage_maintenance"]["expired_pending"] == 0


@pytest.mark.parametrize(
    "completion", ["normal", "compare_error", "detail_error", "timeout", "detail_timeout"]
)
async def test_completed_comparison_releases_responses_before_reusing_byte_budget(
    backend_factory, runtime_factory, asgi_request, respond, monkeypatch, completion
):
    references = []
    entered, release = threading.Event(), threading.Event()
    original = processing_module.compare

    def compare(v1, v2, *args):
        references.extend((weakref.ref(v1), weakref.ref(v2)))
        if completion == "compare_error":
            raise RuntimeError("synthetic comparison failure")
        if completion == "timeout":
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release comparator")
        return original(v1, v2, *args)

    def detail_provider(*responses):
        if completion == "detail_error":
            raise RuntimeError("synthetic detail failure")
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release detail provider")
        return {"value": "private-detail"}

    monkeypatch.setattr(processing_module, "compare", compare)
    body = b'{"data":"' + b"x" * 3900 + b'"}'

    async def backend(_, writer):
        await respond(writer, body)

    v1, v2 = await backend_factory(backend), await backend_factory(backend)
    runtime_values = (
        {
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), lambda *_: "redacted"),
            "detail_provider": detail_provider,
        }
        if completion in {"detail_error", "detail_timeout"}
        else {}
    )
    harness = await runtime_factory(
        v1,
        v2,
        work_values={
            "compare_workers": 2,
            "compare_max_jobs": 1,
            "compare_max_bytes": 20_000,
            "compare_timeout_seconds": 0.03 if "timeout" in completion else 1,
        },
        runtime_values=runtime_values,
    )
    for index in range(2):
        entered.clear()
        release.clear()
        try:
            exchange = await asgi_request(harness.runtime).wait()
            assert exchange.status == 200
            assert exchange.body == body
            if "timeout" in completion:
                await eventually(entered.is_set)
                if completion == "timeout":
                    await eventually(lambda: dropped(harness, "timeout") == index + 1)
                else:
                    await eventually(
                        lambda: (
                            harness.metrics.value(
                                "comparison_pipeline_total", route="catalog", step="stored"
                            )
                            == index + 1
                        )
                    )
                status = harness.runtime.observation_status()
                assert status["comparison_jobs"] == 1
                assert 0 < status["comparison_bytes"] <= 20_000
                assert all(reference() is not None for reference in references[-2:])
        finally:
            release.set()
            await harness.runtime.flush()
        gc.collect()
        assert len(references) == 2 * (index + 1)
        assert all(reference() is None for reference in references)
        assert harness.runtime.observation_status()["comparison_jobs"] == 0
        assert harness.runtime.observation_status()["comparison_bytes"] == 0
        assert dropped(harness, "queue_full") == 0


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
    backend_factory, runtime_factory, asgi_request, blocked_comparator, monkeypatch
):
    entered, release, _ = blocked_comparator
    references = []
    original = processing_module.compare

    def compare(v1, v2, *args):
        references.extend((weakref.ref(v1), weakref.ref(v2)))
        return original(v1, v2, *args)

    monkeypatch.setattr(processing_module, "compare", compare)
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
        assert len(references) == 2
        assert all(reference() is not None for reference in references)
        assert dropped(harness, "shutdown") >= 1
    finally:
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
    gc.collect()
    assert all(reference() is None for reference in references)


async def test_shutdown_budget_does_not_wait_for_unacknowledged_sqlite_thread(
    backend_factory, runtime_factory, asgi_request, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, budget_values={"shutdown_grace_seconds": 0.03})
    original_db = harness.store._db

    def delayed_db():
        db = original_db()
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release SQLite operation")
        return db

    monkeypatch.setattr(harness.store, "_db", delayed_db)
    try:
        assert (await asgi_request(harness.runtime).wait()).status == 200
        await eventually(entered.is_set)
        async with asyncio.timeout(0.5):
            await harness.close()
            await harness.runtime.close()
        assert not release.is_set()
        assert not harness.runtime.ready
        assert harness.collector.counters["ack_unknown"] == 1
        assert harness.collector.metrics()["completeness_known"] is False
    finally:
        release.set()
        await harness.store.close()


async def test_shutdown_does_not_count_already_timed_out_job_twice(
    backend_factory, runtime_factory, asgi_request, blocked_comparator
):
    entered, release, _ = blocked_comparator
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        budget_values={"shutdown_grace_seconds": 0.03},
        work_values={"compare_timeout_seconds": 0.03},
    )
    try:
        await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await eventually(lambda: dropped(harness, "timeout") == 1)
        await harness.close()
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert dropped(harness, "shutdown") == 0
    finally:
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
    assert dropped(harness, "timeout") == 1
    assert dropped(harness, "shutdown") == 0
    assert dropped(harness, "comparison_dropped") == 0


@pytest.mark.parametrize("component", ["comparison", "detail"])
@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_shutdown_late_failure_releases_real_capture_and_keeps_exception_private(
    backend_factory,
    runtime_factory,
    asgi_request,
    respond,
    monkeypatch,
    caplog,
    component,
    timed_out,
    repeat_cancel,
):
    entered, release = threading.Event(), threading.Event()
    references: list[weakref.ReferenceType] = []
    original = processing_module.compare
    marker = "PRIVATE_LATE_COMPARISON_FAILURE_123"

    def fail_later():
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release failed work")
        raise ValueError(marker)

    def compare(v1, v2, *args):
        references.extend((weakref.ref(v1), weakref.ref(v2)))
        if component == "comparison":
            fail_later()
        return original(v1, v2, *args)

    def detail_provider(*_):
        fail_later()

    async def backend(_, writer):
        await respond(writer, b'{"data":"' + b"x" * 2_000_000 + b'"}')

    monkeypatch.setattr(processing_module, "compare", compare)
    v1, v2 = await backend_factory(backend), await backend_factory(backend)
    harness = await runtime_factory(
        v1,
        v2,
        budget_values={
            "response_capture_limit_bytes": 2_100_000,
            "shutdown_grace_seconds": 0.03,
        },
        work_values={
            "compare_max_jobs": 1,
            "compare_max_bytes": 4_200_000,
            "compare_timeout_seconds": 0.03 if timed_out else 1,
        },
        runtime_values={
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), lambda *_: "redacted"),
            "detail_provider": detail_provider,
        }
        if component == "detail"
        else {},
    )
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        assert (await asgi_request(harness.runtime).wait()).status == 200
        await eventually(entered.is_set)
        if timed_out:
            await eventually(
                lambda: (
                    dropped(harness, "timeout") == 1
                    if component == "comparison"
                    else harness.collector.counters["stored"] == 1
                )
            )
        async with asyncio.timeout(0.5):
            await harness.close()
        if repeat_cancel:
            for worker in harness.runtime._comparison._workers:
                worker.cancel()
            await asyncio.sleep(0)
        status = harness.runtime.observation_status()
        assert status["comparison_jobs"] == 1
        assert 4_000_000 < status["comparison_bytes"] <= 4_200_000
        assert len(references) == 2
        assert all(reference() is not None for reference in references)
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
        assert harness.runtime.observation_status()["comparison_bytes"] == 0
        assert all(reference() is None for reference in references)
        assert marker not in caplog.text
        assert dropped(harness, "comparison_dropped") == 0
        assert dropped(harness, "shutdown") == (0 if timed_out else 1)
        assert dropped(harness, "timeout") == int(timed_out and component == "comparison")
        assert harness.collector.counters["stored"] == int(timed_out and component == "detail")
    finally:
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
        if gc_was_enabled:
            gc.enable()
