import asyncio
import gc
import json
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from api_migration_proxy.collection.events import DetailPolicy, make_event
from api_migration_proxy.collection.retention import RetentionWorker
from api_migration_proxy.collection.sqlite import SQLiteEventStore


class RecordingStore:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.active = 0
        self.cancelled = False

    async def purge_expired(self, *, batch_size):
        self.calls.append(batch_size)
        self.active += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1
        return {"details_deleted": 0, "summaries_deleted": 0, "expired_pending": 0}


@pytest.mark.parametrize("interval", [True, False, 0, -1, float("nan"), float("inf"), "1", None])
def test_retention_rejects_invalid_intervals(interval):
    with pytest.raises(ValueError):
        RetentionWorker(RecordingStore(), interval_seconds=interval)


@pytest.mark.parametrize("batch_size", [True, False, 0, -1, 1.5, 2**63, "1", None])
def test_retention_rejects_invalid_batch_sizes(batch_size):
    with pytest.raises(ValueError):
        RetentionWorker(RecordingStore(), batch_size=batch_size)


@pytest.mark.parametrize("timeout", [True, False, -1, float("nan"), float("inf"), "1", None])
async def test_retention_rejects_invalid_close_timeouts(timeout):
    worker = RetentionWorker(RecordingStore())
    with pytest.raises(ValueError):
        await worker.close(timeout)


async def test_retention_start_is_nonblocking_and_does_not_duplicate_workers():
    store = RecordingStore()
    worker = RetentionWorker(store, interval_seconds=0.02, batch_size=7)
    try:
        worker.start()
        worker.start()
        await asyncio.sleep(0)
        assert store.calls == []
        await asyncio.wait_for(store.entered.wait(), 1)
        await worker.close(1)
        assert store.calls == [7]
    finally:
        await worker.close(0)


async def test_retention_does_not_overlap_slow_batches():
    store = RecordingStore()
    store.release.clear()
    worker = RetentionWorker(store, interval_seconds=0.01)
    try:
        worker.start()
        await asyncio.wait_for(store.entered.wait(), 1)
        await asyncio.sleep(0.04)
        assert store.calls == [1000]
        assert store.active == 1
        store.release.set()
        await worker.close(1)
        assert store.calls == [1000]
        assert store.active == 0
    finally:
        store.release.set()
        await worker.close(0)


async def test_retention_close_wakes_idle_worker_and_prevents_restart():
    store = RecordingStore()
    worker = RetentionWorker(store)
    worker.start()
    await asyncio.sleep(0)
    await asyncio.wait_for(worker.close(1), 0.2)
    await worker.close(0)
    assert store.calls == []
    with pytest.raises(RuntimeError, match="closed"):
        worker.start()


async def test_retention_close_before_start_prevents_work():
    store = RecordingStore()
    worker = RetentionWorker(store)
    await worker.close(0)
    with pytest.raises(RuntimeError, match="closed"):
        worker.start()
    assert store.calls == []


async def test_retention_close_waits_for_current_batch_within_grace():
    store = RecordingStore()
    store.release.clear()
    worker = RetentionWorker(store, interval_seconds=0.01)
    worker.start()
    closing_task = None
    try:
        await asyncio.wait_for(store.entered.wait(), 1)
        closing_task = asyncio.create_task(worker.close(1))
        await asyncio.sleep(0)
        assert not closing_task.done()
        store.release.set()
        await asyncio.wait_for(closing_task, 1)
        assert not store.cancelled
        assert store.calls == [1000]
    finally:
        store.release.set()
        if closing_task is not None:
            await closing_task
        await worker.close(0)


@pytest.mark.parametrize("timeout", [0, 0.01])
async def test_retention_close_cancels_current_wait_when_grace_ends(timeout):
    store = RecordingStore()
    store.release.clear()
    worker = RetentionWorker(store, interval_seconds=0.01)
    worker.start()
    try:
        await asyncio.wait_for(store.entered.wait(), 1)
        await asyncio.wait_for(worker.close(timeout), 0.2)
        assert store.cancelled
        assert store.active == 0
        assert store.calls == [1000]
    finally:
        store.release.set()
        await worker.close(0)


async def test_retention_sqlite_purges_one_bounded_batch_and_preserves_live_data(
    tmp_path, monkeypatch
):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    now = time.time()
    summary = {"route_id": "catalog", "comparison": {"result": "matched", "reason": "matched"}}
    detail = DetailPolicy(1, 100, 10, ("/value",), lambda path, value: value)
    expired = [make_event(summary, retention_seconds=60, now=now - 61) for _ in range(2)]
    old_detail = make_event(
        summary, retention_seconds=60, now=now - 20, detail_policy=detail, details={"value": 1}
    )
    live = make_event(
        summary, retention_seconds=60, now=now, detail_policy=detail, details={"value": 2}
    )
    completed = asyncio.Event()
    outcomes = []
    original_purge = store.purge_expired

    async def observe_purge(*, batch_size):
        outcome = await original_purge(batch_size=batch_size)
        outcomes.append(outcome)
        completed.set()
        return outcome

    monkeypatch.setattr(store, "purge_expired", observe_purge)
    worker = RetentionWorker(store, interval_seconds=0.02, batch_size=1)
    try:
        await store.write_batch([*expired, old_detail, live])
        worker.start()
        await asyncio.wait_for(completed.wait(), 1)
        await worker.close(1)
        assert outcomes == [{"details_deleted": 1, "summaries_deleted": 1, "expired_pending": 1}]
        assert store.counters["expired_pending"] == 1
        with closing(sqlite3.connect(path)) as db:
            rows = {
                event_id: (json.loads(raw), raw_detail)
                for event_id, raw, raw_detail in db.execute(
                    "SELECT event_id, summary, detail FROM comparison_event"
                )
            }
        assert len(rows) == 3
        assert len({item.event_id for item in expired} & rows.keys()) == 1
        assert rows[old_detail.event_id][0]["detail_state"] == "expired"
        assert rows[old_detail.event_id][1] is None
        assert json.loads(rows[live.event_id][1]) == {"/value": 2}
        assert rows[live.event_id][0]["detail_state"] == "stored"
    finally:
        await worker.close(0)
        await store.close()


async def test_retention_sqlite_failure_keeps_counter_and_retries_next_interval(
    tmp_path, monkeypatch, caplog
):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    original_db = store._db
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        db = original_db()
        if calls == 1:
            db.execute("SELECT * FROM synthetic_private_retention_failure")
        return db

    monkeypatch.setattr(store, "_db", fail_once)
    store.counters["expired_pending"] = 7
    worker = RetentionWorker(store, interval_seconds=0.01)
    try:
        worker.start()
        async with asyncio.timeout(1):
            while store.counters["expired_pending"] != 0:
                await asyncio.sleep(0.005)
        await worker.close(1)
        assert calls >= 2
        assert store.counters["purge_failures"] == 1
        assert "synthetic_private_retention_failure" not in caplog.text
    finally:
        await worker.close(0)
        await store.close()


@pytest.mark.parametrize("fails", [False, True])
async def test_retention_close_leaves_sqlite_work_owned_until_actual_completion(
    tmp_path, monkeypatch, caplog, fails
):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    entered, release = threading.Event(), threading.Event()
    original_db = store._db
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def delayed_db():
        db = original_db()
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release SQLite retention operation")
        if fails:
            db.execute("SELECT * FROM synthetic_private_late_retention_failure")
        return db

    monkeypatch.setattr(store, "_db", delayed_db)
    store.counters["expired_pending"] = 7
    worker = RetentionWorker(store, interval_seconds=0.01)
    closing_task = None
    try:
        worker.start()
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(worker.close(0), 0.2)
        assert store._slot.locked()
        assert store.counters["expired_pending"] == 7
        closing_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        assert not closing_task.done()
        release.set()
        await asyncio.wait_for(closing_task, 1)
        gc.collect()
        await asyncio.sleep(0)
        assert not store._slot.locked()
        assert store._connection is None
        assert store.counters["purge_failures"] == int(fails)
        assert store.counters["expired_pending"] == (7 if fails else 0)
        assert errors == []
        assert "synthetic_private_late_retention_failure" not in caplog.text
    finally:
        release.set()
        await worker.close(0)
        if closing_task is not None:
            await closing_task
        await store.close()
        loop.set_exception_handler(previous_handler)
