import asyncio
import json
import threading
import time

import pytest

from api_migration_proxy.collection import DetailPolicy, EventQuery, QueryAccess


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def pipeline(harness, step):
    return harness.metrics.value("comparison_pipeline_total", route="catalog", step=step)


async def test_t25_sampled_out_detail_never_invokes_provider_or_masker(
    backend_factory, runtime_factory, asgi_request, monkeypatch
):
    calls = []

    def provider(*_):
        calls.append("provider")
        return {"value": "private-detail-value"}

    def masker(*_):
        calls.append("masker")
        return "redacted"

    monkeypatch.setattr("api_migration_proxy.runtime.secrets.randbelow", lambda n: n * 3 // 4)
    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        runtime_values={
            "detail_policy": DetailPolicy(0.5, 100, 10, ("/value",), masker),
            "detail_provider": provider,
        },
    )
    exchange = await asgi_request(harness.runtime).wait()
    await harness.runtime.flush()
    records = await harness.records()
    assert exchange.status == 200
    assert not calls
    assert len(records) == 1
    assert records[0]["comparison"]["result"] == "matched"
    assert records[0]["detail_sampled"] is False
    assert records[0]["detail_state"] == "not_selected"
    assert pipeline(harness, "comparable") == pipeline(harness, "stored") == 1


@pytest.mark.parametrize("blocked_component", ["provider", "masker"])
async def test_t25_t44_detail_timeout_preserves_summary_and_tracks_actual_cpu_work(
    backend_factory, runtime_factory, asgi_request, blocked_component
):
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test did not release detail processing")

    def provider(*_):
        if blocked_component == "provider":
            block()
        return {"value": "private-detail-value"}

    def masker(*_):
        if blocked_component == "masker":
            block()
        return "redacted"

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        work_values={"compare_max_jobs": 1, "compare_timeout_seconds": 0.03},
        runtime_values={
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), masker),
            "detail_provider": provider,
        },
    )
    first_event_id = None
    try:
        exchange = await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await eventually(lambda: pipeline(harness, "stored") == 1)
        assert exchange.status == 200
        assert pipeline(harness, "comparable") == 1
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert harness.runtime.observation_status()["comparison_bytes"] > 0
        now = time.time()
        records = await harness.store.query(
            EventQuery(now - 100, now + 1, frozenset({"catalog"}), 100),
            QueryAccess(frozenset({"catalog"}), 100, 200, True),
        )
        assert len(records) == 1
        first_event_id = records[0]["event_id"]
        assert records[0]["summary"]["comparison"]["result"] == "matched"
        assert records[0]["summary"]["detail_sampled"] is True
        assert records[0]["summary"]["detail_state"] == "masking_failed"
        assert records[0]["detail"] is None
        assert "private-detail-value" not in json.dumps(records)
    finally:
        release.set()
    await harness.runtime.flush()
    assert harness.runtime.observation_status()["comparison_jobs"] == 0
    assert harness.runtime.observation_status()["comparison_bytes"] == 0
    assert pipeline(harness, "comparable") == pipeline(harness, "stored") == 1
    now = time.time()
    records = await harness.store.query(
        EventQuery(now - 100, now + 1, frozenset({"catalog"}), 100),
        QueryAccess(frozenset({"catalog"}), 100, 200, True),
    )
    assert len(records) == 1
    assert records[0]["event_id"] == first_event_id
    assert records[0]["summary"]["detail_state"] == "masking_failed"
    assert records[0]["detail"] is None


async def test_shutdown_after_safe_summary_storage_does_not_report_summary_loss(
    backend_factory, runtime_factory, asgi_request
):
    entered, release = threading.Event(), threading.Event()

    def provider(*_):
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release detail provider")
        return {"value": "private-detail-value"}

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        budget_values={"shutdown_grace_seconds": 0.03},
        work_values={"compare_timeout_seconds": 0.03},
        runtime_values={
            "detail_policy": DetailPolicy(1, 100, 10, ("/value",), lambda *_: "redacted"),
            "detail_provider": provider,
        },
    )
    try:
        await asgi_request(harness.runtime).wait()
        await eventually(entered.is_set)
        await eventually(lambda: pipeline(harness, "stored") == 1)
        assert len(await harness.records()) == 1
        await harness.close()
        assert harness.runtime.observation_status()["comparison_jobs"] == 1
        assert harness.metrics.value("collection_dropped_total", reason="shutdown") == 0
    finally:
        release.set()
        await eventually(lambda: harness.runtime.observation_status()["comparison_jobs"] == 0)
    assert pipeline(harness, "stored") == 1
    assert harness.metrics.value("collection_dropped_total", reason="shutdown") == 0
