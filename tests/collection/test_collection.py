import asyncio
import copy
import gc
import json
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import weakref
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from api_migration_proxy.cli import main
from api_migration_proxy.collection.collector import BatchResult, BoundedCollector, CollectionLimits
from api_migration_proxy.collection.events import DetailPolicy, make_event
from api_migration_proxy.collection.query import EventQuery, QueryAccess
from api_migration_proxy.collection.sqlite import SQLiteEventStore


def event(**kwargs):
    summary = {
        "route_id": "catalog",
        "configuration_revision": "config-1",
        "comparison_policy_revision": "compare-1",
        "epoch_id": "epoch-1",
        "request_started_at": "2026-09-19T00:00:00+00:00",
        "request_ended_at": "2026-09-19T00:00:01+00:00",
        "shadow_selected": True,
        "shadow_dispatched": True,
        "serving_backend": "v1",
        "response_source": "backend",
        "request_outcome": "completed",
        "backends": {
            "v1": {"role": "serving", "deployment_revision": "v1-build"},
            "v2": {"role": "shadow", "deployment_revision": "v2-build"},
        },
        "comparison": {
            "result": "different",
            "reason": "json_value_mismatch",
            "comparison_class": "success",
            "difference_paths": ["/price"],
        },
    }
    summary.update(kwargs.pop("summary", {}))
    return make_event(summary, retention_seconds=100, **kwargs)


def limits(**kwargs):
    values = {
        "max_events": 3,
        "max_bytes": 20_000,
        "max_age_seconds": 1,
        "batch_size": 3,
        "write_timeout_seconds": 0.1,
        "max_attempts": 3,
        "retry_delay_seconds": 0,
    }
    values.update(kwargs)
    return CollectionLimits(**values)


@pytest.mark.parametrize(
    "field",
    [
        "max_events",
        "max_bytes",
        "batch_size",
        "max_attempts",
        "max_age_seconds",
        "write_timeout_seconds",
        "retry_delay_seconds",
    ],
)
def test_boolean_is_not_a_collection_budget(field):
    with pytest.raises(ValueError):
        limits(**{field: True})


def test_detail_and_query_numeric_limits_reject_booleans():
    with pytest.raises(ValueError):
        DetailPolicy(True, 100, 10)
    with pytest.raises(ValueError):
        QueryAccess(frozenset({"catalog"}), True, 10)
    with pytest.raises(ValueError):
        QueryAccess(frozenset({"catalog"}), 10, True)


class AcknowledgingStore:
    def __init__(self):
        self.events = []

    async def write_batch(self, events):
        self.events.extend(events)
        return BatchResult(acknowledged=frozenset(item.event_id for item in events))


def test_t25_detail_disabled_preserves_safe_comparison_summary():
    item = event(
        details={"authorization": "do-not-store", "price": 42},
        allowed_difference_paths=frozenset({"/price"}),
    )
    assert item.detail is None
    assert item.summary["detail_state"] == "disabled"
    assert item.summary["comparison"]["result"] == "different"
    assert item.summary["comparison"]["difference_paths"] == ["/price"]
    assert "do-not-store" not in json.dumps(item.summary)


def test_t26_raw_values_exception_headers_and_dynamic_paths_not_stored():
    item = event(
        summary={
            "url": "https://user:password@host/?token=secret",
            "headers": {"cookie": "private"},
            "trace_id": "invalid trace",
            "body": {"email": "person@example.invalid"},
            "comparison": {
                "result": "different",
                "reason": "request failed with Bearer secret",
                "difference_paths": ["/users/person@example.invalid", "/authorization"],
                "left_value": "private",
            },
        }
    )
    encoded = json.dumps(item.summary)
    assert all(
        value not in encoded
        for value in ("secret", "private", "person@example.invalid", "password")
    )
    assert item.summary["comparison"]["reason"] == "unknown"
    assert item.summary["comparison"]["difference_paths"] == []
    assert item.summary["comparison"]["paths_truncated"]
    assert "trace_id" not in item.summary


def test_t25_detail_allows_only_reviewed_scalar_fields_and_caps_bytes():
    policy = DetailPolicy(1, 100, 20, ("/price",), lambda path, value: value)
    item = event(detail_policy=policy, details={"price": 12, "token": "hidden"}, now=100)
    assert item.detail == {"/price": 12}
    assert item.detail_expires_at == 120
    oversized = event(detail_policy=policy, details={"price": "x" * 1000})
    assert oversized.detail is None
    assert oversized.summary["detail_state"] == "oversized"
    assert oversized.summary["comparison"]["result"] == "different"


def test_t26_masking_errors_never_capture_exception_or_nested_secrets():
    def fail(path, value):
        raise ValueError("token=private")

    policy = DetailPolicy(1, 100, 20, ("/price",), fail)
    item = event(detail_policy=policy, details={"price": 1})
    assert item.summary["detail_state"] == "masking_failed"
    assert item.detail is None
    nested = event(
        detail_policy=DetailPolicy(1, 100, 20, ("/price",), lambda path, value: value),
        details={"price": {"secret": "nested"}},
    )
    assert nested.summary["detail_state"] == "masking_failed"
    with pytest.raises(ValueError, match="unsafe_detail_path"):
        DetailPolicy(1, 100, 20, ("/Authorization",), lambda path, value: value)


def test_t25_no_allowed_detail_values_is_not_successful_empty_collection():
    policy = DetailPolicy(1, 100, 20, ("/price",), lambda path, value: value)
    item = event(detail_policy=policy, details={"unapproved": "value"})
    assert item.detail is None
    assert item.summary["detail_state"] == "no_allowed_fields"
    filtered = event(
        detail_policy=DetailPolicy(1, 100, 20, ("/price",), lambda path, value: None),
        details={"price": 42},
    )
    assert filtered.detail is None
    assert filtered.summary["detail_state"] == "no_allowed_fields"


async def test_t23_queue_count_bytes_and_age_bounded_including_inflight():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowStore:
        async def write_batch(self, events):
            entered.set()
            await release.wait()
            return BatchResult(acknowledged=frozenset(item.event_id for item in events))

    collector = BoundedCollector(SlowStore(), limits(max_events=1))
    assert collector.submit(event())
    await entered.wait()
    assert collector.metrics()["queue_depth"] == 1
    assert not collector.submit(event())
    release.set()
    await collector.flush()
    assert collector.metrics()["queue_depth"] == 0
    assert collector.metrics()["queue_bytes"] == 0
    assert collector.counters["dropped_capacity"] == 1
    too_small = BoundedCollector(AcknowledgingStore(), limits(max_bytes=1))
    assert not too_small.submit(event())
    assert too_small.counters["dropped_capacity"] == 1
    expired = BoundedCollector(AcknowledgingStore(), limits())
    assert not expired.submit(event(work_started_at=time.monotonic() - 2))
    assert expired.counters["dropped_expired"] == 1


async def test_t24_partial_ack_retry_only_unconfirmed_and_w_once():
    class PartialStore:
        def __init__(self):
            self.calls = []

        async def write_batch(self, events):
            ids = [item.event_id for item in events]
            self.calls.append(ids)
            if len(self.calls) == 1:
                return BatchResult(acknowledged=frozenset(ids[:1]), failed=frozenset(ids[1:]))
            return BatchResult(acknowledged=frozenset(ids))

    store, counted = PartialStore(), []
    collector = BoundedCollector(store, limits(), on_stored=counted.append)
    first, second = event(), event()
    assert collector.submit(first)
    assert collector.submit(second)
    assert not collector.submit(first)
    await collector.flush()
    assert store.calls == [[first.event_id, second.event_id], [second.event_id]]
    assert counted == [first.event_id, second.event_id]
    assert collector.counters["stored"] == 2
    assert not collector.submit(first)


async def test_partial_ack_releases_payload_before_retry_wait(monkeypatch):
    retrying, release = asyncio.Event(), asyncio.Event()

    async def wait_for_retry(delay):
        retrying.set()
        await release.wait()

    monkeypatch.setattr("api_migration_proxy.collection.collector.asyncio.sleep", wait_for_retry)

    class PartialStore:
        def __init__(self):
            self.calls = []
            self.references = []

        async def write_batch(self, events):
            ids = [item.event_id for item in events]
            self.calls.append(ids)
            if len(self.calls) == 1:
                self.references = [weakref.ref(item) for item in events]
                return BatchResult(acknowledged=frozenset(ids[1:]), failed=frozenset(ids[:1]))
            return BatchResult(acknowledged=frozenset(ids))

    policy = DetailPolicy(1, 2100, 20, ("/price",), lambda path, value: value)
    first, second, third = [
        event(detail_policy=policy, details={"price": "x" * 2000}) for _ in range(3)
    ]
    event_bytes = len(json.dumps({"summary": first.summary, "detail": first.detail}).encode())
    store, counted = PartialStore(), []
    collector = BoundedCollector(
        store,
        limits(max_events=2, max_bytes=2 * event_bytes, max_age_seconds=10),
        on_stored=counted.append,
    )
    assert collector.submit(first)
    assert collector.submit(second)
    try:
        await asyncio.wait_for(retrying.wait(), 1)
        assert collector.metrics()["queue_bytes"] == event_bytes
        assert store.references[0]() is not None
        assert store.references[1]() is None
        assert collector.submit(third)
        assert collector.metrics()["queue_bytes"] == 2 * event_bytes
    finally:
        release.set()
        await collector.flush()
    assert store.calls == [
        [first.event_id, second.event_id],
        [first.event_id],
        [third.event_id],
    ]
    assert counted == [second.event_id, first.event_id, third.event_id]
    assert collector.counters["stored"] == 3
    assert collector.metrics()["queue_bytes"] == 0


async def test_expired_payload_released_before_store_wait(monkeypatch):
    now = 100
    monkeypatch.setattr(
        "api_migration_proxy.collection.collector.time", SimpleNamespace(monotonic=lambda: now)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    expired_references = []

    def remember_outcome(item, outcome):
        if outcome == "dropped_expired":
            expired_references.append(weakref.ref(item))

    class SlowStore:
        async def write_batch(self, events):
            entered.set()
            await release.wait()
            return BatchResult(acknowledged=frozenset(item.event_id for item in events))

    policy = DetailPolicy(1, 2100, 20, ("/price",), lambda path, value: value)
    collector = BoundedCollector(SlowStore(), limits(max_events=2), on_outcome=remember_outcome)
    assert collector.submit(
        event(work_started_at=now, detail_policy=policy, details={"price": "x" * 2000})
    )
    assert collector.submit(
        event(work_started_at=now - 0.5, detail_policy=policy, details={"price": "x" * 2000})
    )
    now += 0.6
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert collector.metrics()["queue_depth"] == 1
        assert collector.counters["dropped_expired"] == 1
        assert expired_references[0]() is None
    finally:
        release.set()
        await collector.flush()
    assert collector.counters["stored"] == 1
    assert collector.metrics()["queue_bytes"] == 0


async def test_t24_copied_event_never_consumes_capacity_or_recounts_storage():
    collector = BoundedCollector(AcknowledgingStore(), limits())
    item = event()
    shallow, deep, replaced = copy.copy(item), copy.deepcopy(item), replace(item)
    assert collector.submit(item)
    for duplicate in (shallow, deep, replaced):
        assert not collector.submit(duplicate)
    await collector.flush()
    assert collector.metrics()["queue_bytes"] == 0
    assert collector.counters["stored"] == 1
    for duplicate in (copy.copy(item), copy.deepcopy(item), replace(item)):
        assert not collector.submit(duplicate)


@pytest.mark.parametrize(
    ("responses", "outcome"),
    [
        (("conflict",), "ack_unknown"),
        (("failed", "conflict"), "ack_unknown"),
        (("conflict", "failed"), "ack_unknown"),
        (("conflict", "conflict"), "ack_unknown"),
        (("conflict", "acknowledged"), "stored"),
    ],
)
async def test_conflicting_storage_results_remain_unknown_until_clean_ack(responses, outcome):
    calls: list[frozenset[str]] = []
    counted: list[str] = []

    class Store:
        async def write_batch(self, events):
            response = responses[len(calls)]
            ids = frozenset(item.event_id for item in events)
            calls.append(ids)
            return BatchResult(
                acknowledged=ids if response != "failed" else frozenset(),
                failed=ids if response != "acknowledged" else frozenset(),
            )

    collector = BoundedCollector(
        Store(), limits(max_attempts=len(responses)), on_stored=counted.append
    )
    item = event()
    assert collector.submit(item)
    await collector.flush()
    assert calls == [frozenset({item.event_id})] * len(responses)
    assert collector.counters[outcome] == 1
    assert collector.counters["dropped_retry_exhausted"] == 0
    assert collector.counters["ack_unknown"] == int(outcome == "ack_unknown")
    assert collector.counters["stored"] == int(outcome == "stored")
    assert collector.metrics()["completeness_known"] is (outcome == "stored")
    assert counted == ([item.event_id] if outcome == "stored" else [])
    assert collector.metrics()["queue_bytes"] == 0


@pytest.mark.parametrize("previous_unknown", [False, True])
async def test_shutdown_between_retries_preserves_ack_certainty(monkeypatch, previous_unknown):
    retrying, release = asyncio.Event(), asyncio.Event()
    calls, outcomes = [], []

    class Store:
        async def write_batch(self, events):
            ids = frozenset(item.event_id for item in events)
            calls.append(ids)
            if previous_unknown and len(calls) == 1:
                return BatchResult(unknown=ids)
            return BatchResult(failed=ids)

    async def wait_for_retry(delay):
        if len(calls) == 1 + int(previous_unknown):
            retrying.set()
            await release.wait()

    monkeypatch.setattr("api_migration_proxy.collection.collector.asyncio.sleep", wait_for_retry)
    collector = BoundedCollector(
        Store(),
        limits(),
        on_outcome=lambda item, outcome: outcomes.append(outcome),
    )
    assert collector.submit(event())
    try:
        await asyncio.wait_for(retrying.wait(), 1)
        assert not collector._writes
        assert not await collector.close(0.01)
        assert outcomes == ["ack_unknown" if previous_unknown else "dropped_shutdown"]
        assert collector.counters["ack_unknown"] == int(previous_unknown)
        assert collector.counters["dropped_shutdown"] == int(not previous_unknown)
        assert collector.metrics()["queue_depth"] == 0
        assert collector.metrics()["queue_bytes"] == 0
        assert collector.metrics()["completeness_known"] is False
    finally:
        release.set()
        await collector.close(1)


async def test_shutdown_after_write_callback_before_ack_processing_is_unknown(monkeypatch):
    saved: set[str] = set()
    callbacks = []
    counted: list[str] = []

    class Store:
        async def write_batch(self, events):
            saved.update(item.event_id for item in events)
            return BatchResult(acknowledged=frozenset(saved))

    collector = BoundedCollector(Store(), limits(), on_stored=counted.append)
    original_done = collector._write_done

    def cancel_after_write_done(event_ids, task):
        original_done(event_ids, task)
        callbacks.append((task.done(), bool(collector._writes), collector.counters["stored"]))
        assert collector._worker is not None
        collector._worker.cancel()

    monkeypatch.setattr(collector, "_write_done", cancel_after_write_done)
    item = event()
    assert collector.submit(item)
    await collector.flush()
    assert callbacks == [(True, False, 0)]
    assert saved == {item.event_id}
    assert counted == []
    assert collector.counters["ack_unknown"] == 1
    assert collector.counters["dropped_shutdown"] == 0
    assert collector.metrics()["queue_bytes"] == 0
    assert collector.metrics()["completeness_known"] is False


async def test_t24_unknown_ack_exhaustion_is_not_definite_loss():
    class LostAck:
        async def write_batch(self, events):
            raise TimeoutError("credentials must not become metric labels")

    collector = BoundedCollector(LostAck(), limits(max_attempts=2))
    assert collector.submit(event())
    await collector.flush()
    assert collector.counters["ack_unknown"] == 1
    assert collector.counters["write_failures"] == 2
    assert collector.metrics()["completeness_known"] is False
    assert not any(key.startswith("dropped") for key in collector.counters)
    assert "credentials" not in json.dumps(collector.metrics())


async def test_t24_definite_failures_drop_once_after_bounded_retry():
    class FailedStore:
        async def write_batch(self, events):
            return BatchResult(failed=frozenset(item.event_id for item in events))

    collector = BoundedCollector(FailedStore(), limits(max_attempts=3))
    collector.submit(event())
    await collector.flush()
    assert collector.counters["write_failures"] == 3
    assert collector.counters["dropped_retry_exhausted"] == 1
    assert collector.counters["ack_unknown"] == 0
    assert collector.metrics()["completeness_known"] is True


async def test_t36_shutdown_keeps_unconfirmed_writes_distinct_from_queued_loss():
    release = asyncio.Event()

    class HungStore:
        async def write_batch(self, events):
            await release.wait()
            return BatchResult(acknowledged=frozenset(item.event_id for item in events))

    collector = BoundedCollector(HungStore(), limits(batch_size=1))
    collector.submit(event())
    collector.submit(event())
    assert not await collector.close(0.01)
    assert collector.counters["ack_unknown"] == 1
    assert collector.counters["dropped_shutdown"] == 1
    assert not collector.metrics()["completeness_known"]
    assert collector.metrics()["queue_depth"] == 1
    release.set()
    async with asyncio.timeout(1):
        while collector.metrics()["queue_depth"]:
            await asyncio.sleep(0)
    assert collector.counters["ack_unknown"] == 1
    assert collector.counters["dropped_shutdown"] == 1


async def test_sqlite_timeout_reserves_payload_until_actual_write_finishes(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    await store.write_batch([])
    calls, references, outcomes, counted = [], [], [], []
    unknown = asyncio.Event()
    original_write = store.write_batch

    async def write_batch(events):
        calls.append([item.event_id for item in events])
        references.extend(weakref.ref(item) for item in events)
        return await original_write(events)

    monkeypatch.setattr(store, "write_batch", write_batch)

    def outcome(item, result):
        outcomes.append((item.event_id, result))
        if result == "ack_unknown":
            unknown.set()

    policy = DetailPolicy(1, 2100, 20, ("/price",), lambda path, value: value)
    first = event(detail_policy=policy, details={"price": "x" * 2000})
    event_bytes = len(json.dumps({"summary": first.summary, "detail": first.detail}).encode())
    collector = BoundedCollector(
        store,
        limits(max_events=2, max_bytes=event_bytes, write_timeout_seconds=0.01),
        on_outcome=outcome,
        on_stored=counted.append,
    )
    try:
        with closing(sqlite3.connect(path)) as locker:
            locker.execute("BEGIN EXCLUSIVE")
            try:
                assert collector.submit(first)
                await asyncio.wait_for(unknown.wait(), 0.5)
                assert collector.metrics()["queue_depth"] == 1
                assert collector.metrics()["queue_bytes"] == event_bytes
                assert references[0]() is not None
                assert calls == [[first.event_id]]
                assert not collector.submit(
                    event(detail_policy=policy, details={"price": "x" * 2000})
                )
            finally:
                locker.rollback()
        await collector.flush()
        assert collector.metrics()["queue_depth"] == 0
        assert collector.metrics()["queue_bytes"] == 0
        assert references[0]() is None
        assert outcomes == [(first.event_id, "ack_unknown")]
        assert counted == []
        assert collector.counters["stored"] == 0
        assert collector.metrics()["completeness_known"] is False
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT count(*) FROM comparison_event").fetchone()[0] == 1
    finally:
        await collector.close(1)
        await store.close()


async def test_shutdown_keeps_live_store_reservation_and_never_counts_late_ack():
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    references, outcomes, counted = [], [], []

    class SlowStore:
        async def write_batch(self, events):
            references.extend(weakref.ref(item) for item in events)
            entered.set()
            await release.wait()
            completed.set()
            return BatchResult(acknowledged=frozenset(item.event_id for item in events))

    collector = BoundedCollector(
        SlowStore(),
        limits(max_events=1, write_timeout_seconds=10),
        on_outcome=lambda item, result: outcomes.append((item.event_id, result)),
        on_stored=counted.append,
    )
    first = event()
    assert collector.submit(first)
    await entered.wait()
    retained_bytes = collector.metrics()["queue_bytes"]
    try:
        async with asyncio.timeout(0.5):
            assert not await collector.close(0.01)
        assert not completed.is_set()
        assert collector.metrics()["queue_depth"] == 1
        assert collector.metrics()["queue_bytes"] == retained_bytes
        assert references[0]() is not None
        assert outcomes == [(first.event_id, "ack_unknown")]
    finally:
        release.set()
    async with asyncio.timeout(1):
        await completed.wait()
        while collector.metrics()["queue_depth"]:
            await asyncio.sleep(0)
    assert collector.metrics()["queue_bytes"] == 0
    assert references[0]() is None
    assert outcomes == [(first.event_id, "ack_unknown")]
    assert counted == []
    assert collector.counters["stored"] == 0


@pytest.mark.parametrize(("write_timeout", "close_early"), [(0.01, False), (2, False), (2, True)])
async def test_sqlite_failure_releases_payload_without_garbage_collection(
    tmp_path, monkeypatch, write_timeout, close_early
):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    await store.write_batch([])
    entered = asyncio.Event()
    original_write = store.write_batch

    async def write_batch(events):
        entered.set()
        return await original_write(events)

    monkeypatch.setattr(store, "write_batch", write_batch)
    references = []
    collector = BoundedCollector(
        store,
        limits(write_timeout_seconds=write_timeout, max_attempts=1, max_age_seconds=3),
        on_outcome=lambda item, result: references.append(weakref.ref(item)),
    )
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with closing(sqlite3.connect(path)) as locker:
            locker.execute("BEGIN EXCLUSIVE")
            try:
                assert collector.submit(event())
                await entered.wait()
                if close_early:
                    assert not await collector.close(0.01)
                    assert collector.metrics()["queue_depth"] == 1
                    async with asyncio.timeout(2):
                        while collector.metrics()["queue_depth"]:
                            await asyncio.sleep(0.005)
                else:
                    await asyncio.wait_for(collector.flush(), 2)
                assert collector.metrics()["queue_bytes"] == 0
                assert collector.counters["ack_unknown"] == 1
                assert references[0]() is None
            finally:
                locker.rollback()
    finally:
        if not close_early:
            await collector.close(1)
        await store.close()
        if was_enabled:
            gc.enable()


async def test_sqlite_detail_uses_the_utf8_size_checked_by_detail_policy(tmp_path):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    policy = DetailPolicy(1, 100, 20, ("/price",), lambda path, value: value)
    item = event(detail_policy=policy, details={"price": "한" * 28}, now=100)
    assert item.summary["detail_state"] == "pending"
    try:
        await store.write_batch([item])
        with closing(sqlite3.connect(path)) as db:
            raw = db.execute("SELECT detail FROM comparison_event").fetchone()[0]
        assert len(raw.encode()) <= policy.max_bytes
        assert json.loads(raw) == item.detail
        rows = await store.query(
            EventQuery(99, 102, frozenset({"catalog"}), 1),
            QueryAccess(frozenset({"catalog"}), 1, 5, True),
            now=101,
        )
        assert rows[0]["detail"] == item.detail
    finally:
        await store.close()


async def test_sqlite_creates_private_files_and_directories_without_changing_existing_parent(
    tmp_path,
):
    tmp_path.chmod(0o755)
    path = tmp_path / "new" / "private" / "events.sqlite"
    store = SQLiteEventStore(str(path))
    try:
        await store.write_batch([event()])
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        def inspect_journal():
            db = store._db()
            with db:
                db.execute("UPDATE comparison_event SET stored_at = stored_at + 1")
                assert stat.S_IMODE(path.with_name(path.name + "-journal").stat().st_mode) == 0o600

        await store._run(inspect_journal)
    finally:
        await store.close()


@pytest.mark.parametrize("create", [True, False])
async def test_sqlite_restricts_existing_database_permissions_before_access(tmp_path, create):
    path = tmp_path / "events.sqlite"
    seed = SQLiteEventStore(str(path))
    try:
        await seed.write_batch([event(now=100)])
    finally:
        await seed.close()
    path.chmod(0o644)
    store = SQLiteEventStore(str(path), create=create)
    try:
        assert (await store.purge_expired(now=201, batch_size=1))["summaries_deleted"] == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        await store.close()


async def test_sqlite_existing_only_does_not_create_missing_parent_or_database(tmp_path):
    path = tmp_path / "missing" / "events.sqlite"
    store = SQLiteEventStore(str(path), create=False)
    try:
        with pytest.raises((OSError, sqlite3.OperationalError)):
            await store.purge_expired(now=100, batch_size=1)
        assert not path.parent.exists()
    finally:
        await store.close()


async def test_sqlite_memory_store_does_not_create_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteEventStore(":memory:")
    try:
        await store.write_batch([event(now=100)])
        assert (await store.purge_expired(now=201, batch_size=1))["summaries_deleted"] == 1
        assert not list(tmp_path.iterdir())
    finally:
        await store.close()


async def test_sqlite_expiry_queries_use_indexes(tmp_path):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    try:
        await store.write_batch([event()])
        with closing(sqlite3.connect(path)) as db:
            for condition in (
                "summary_expires_at <= ?",
                "detail IS NOT NULL AND detail_expires_at <= ?",
            ):
                plan = db.execute(
                    "EXPLAIN QUERY PLAN SELECT event_id FROM comparison_event WHERE "
                    + condition
                    + " LIMIT 1",
                    (100,),
                ).fetchall()
                assert all("SCAN comparison_event" not in row[3] for row in plan)
    finally:
        await store.close()


async def test_sqlite_initialization_recovers_after_database_lock_is_released(tmp_path):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    item = event(now=100)
    try:
        with closing(sqlite3.connect(path)) as locker:
            locker.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                await store.write_batch([item])
            locker.rollback()

        for _ in range(2):
            result = await store.write_batch([item])
            assert result.acknowledged == frozenset({item.event_id})
        rows = await store.query(
            EventQuery(99, 102, frozenset({"catalog"}), 10),
            QueryAccess(frozenset({"catalog"}), 10, 5),
            now=101,
        )
        assert [row["event_id"] for row in rows] == [item.event_id]
        with closing(sqlite3.connect(path)) as db:
            assert db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'event_route_time'"
            ).fetchone() == ("event_route_time",)
    finally:
        await store.close()


async def test_sqlite_writes_by_column_name_when_existing_column_order_differs(tmp_path):
    path = tmp_path / "events.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("""CREATE TABLE comparison_event (
            event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, summary_expires_at REAL NOT NULL,
            detail_expires_at REAL, result TEXT NOT NULL, route_id TEXT NOT NULL, reason TEXT NOT NULL,
            configuration_revision TEXT NOT NULL, comparison_policy_revision TEXT NOT NULL,
            summary TEXT NOT NULL, detail TEXT, stored_at REAL NOT NULL
        )""")
    store = SQLiteEventStore(str(path))
    item = event(now=100)
    try:
        result = await store.write_batch([item])
        assert result.acknowledged == frozenset({item.event_id})
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT route_id, result FROM comparison_event").fetchone() == (
                "catalog",
                "different",
            )
        rows = await store.query(
            EventQuery(99, 102, frozenset({"catalog"}), 1, result="different"),
            QueryAccess(frozenset({"catalog"}), 1, 5),
            now=101,
        )
        assert [row["event_id"] for row in rows] == [item.event_id]
    finally:
        await store.close()


async def test_sqlite_invalid_batch_rolls_back_without_acknowledging_missing_row(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    valid, invalid = event(now=100), event(now=100)
    invalid.summary["route_id"] = None
    query = EventQuery(99, 102, frozenset({"catalog"}), 10)
    access = QueryAccess(frozenset({"catalog"}), 10, 5)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await store.write_batch([valid, invalid])
        assert await store.query(query, access, now=101) == []
        assert (await store.write_batch([valid])).acknowledged == {valid.event_id}
        rows = await store.query(query, access, now=101)
        assert [row["event_id"] for row in rows] == [valid.event_id]
    finally:
        await store.close()


async def test_t24_sqlite_ack_loss_dedup_does_not_extend_original_retention(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    try:
        item = event(now=100)
        await store.write_batch([item])
        item.summary_expires_at = 1000
        await store.write_batch([item])
        rows = await store.query(
            EventQuery(99, 102, frozenset({"catalog"}), 10),
            QueryAccess(frozenset({"catalog"}), 10, 5),
            now=101,
        )
        assert len(rows) == 1
        assert rows[0]["event_id"] == item.event_id
        assert rows[0]["summary_expires_at"] == "1970-01-01T00:03:20+00:00"
    finally:
        await store.close()


async def test_t28_query_scope_time_rows_and_backend_filters(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    try:
        await store.write_batch([event(now=100), event(now=100, summary={"route_id": "private"})])
        access = QueryAccess(frozenset({"catalog"}), 2, 10)
        query = EventQuery(
            99,
            105,
            frozenset({"catalog"}),
            2,
            result="different",
            configuration_revision="config-1",
            backend="v2",
            role="shadow",
            deployment_revision="v2-build",
        )
        rows = await store.query(query, access, now=101)
        assert len(rows) == 1
        assert rows[0]["summary"]["route_id"] == "catalog"
        with pytest.raises(PermissionError):
            await store.query(EventQuery(99, 105, frozenset({"private"}), 1), access)
        with pytest.raises(ValueError, match="query_limit"):
            await store.query(EventQuery(99, 200, frozenset({"catalog"}), 1), access)
        with pytest.raises(ValueError, match="query_limit"):
            await store.query(EventQuery(99, 105, frozenset({"catalog"}), 3), access)
    finally:
        await store.close()


@pytest.mark.parametrize("backend", [None, "v2"])
@pytest.mark.parametrize(
    ("revision", "matches"),
    [(None, True), ("", False), ("missing-build", False), ("v2-build", True)],
)
async def test_query_deployment_revision_filter_distinguishes_empty_from_omitted(
    tmp_path, backend, revision, matches
):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    item = event(now=100)
    try:
        await store.write_batch([item])
        rows = await store.query(
            EventQuery(
                99,
                102,
                frozenset({"catalog"}),
                1,
                backend=backend,
                deployment_revision=revision,
            ),
            QueryAccess(frozenset({"catalog"}), 1, 5),
            now=101,
        )
        assert [row["event_id"] for row in rows] == ([item.event_id] if matches else [])
    finally:
        await store.close()


@pytest.mark.parametrize("now", [True, False, float("nan"), float("inf"), -float("inf"), "100"])
async def test_query_rejects_invalid_clock_without_creating_store(tmp_path, now):
    path = tmp_path / "missing.sqlite"
    store = SQLiteEventStore(str(path))
    try:
        with pytest.raises(ValueError, match="invalid_query_time"):
            await store.query(
                EventQuery(99, 102, frozenset({"catalog"}), 1),
                QueryAccess(frozenset({"catalog"}), 1, 5),
                now=now,
            )
        assert not path.exists()
    finally:
        await store.close()


@pytest.mark.parametrize("expired", ["summary", "detail"])
@pytest.mark.parametrize("waiting", ["store_slot", "database_lock"])
async def test_query_rechecks_default_clock_after_storage_waits(
    tmp_path, monkeypatch, expired, waiting
):
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(str(path))
    current = 100
    monkeypatch.setattr(
        "api_migration_proxy.collection.sqlite.time", SimpleNamespace(time=lambda: current)
    )
    policy = DetailPolicy(1, 100, 20, ("/price",), lambda path, value: value)
    await store.write_batch([event(now=100, detail_policy=policy, details={"price": 42})])
    entered, release = threading.Event(), threading.Event()
    blocker = query_task = None
    locker = None
    try:
        if waiting == "store_slot":

            def hold_slot():
                entered.set()
                assert release.wait(3)

            blocker = asyncio.create_task(store._run(hold_slot))
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
        else:
            original_db = store._db

            class ReadConnection:
                def execute(self, *args):
                    entered.set()
                    return original_db().execute(*args)

            monkeypatch.setattr(store, "_db", ReadConnection)
            locker = sqlite3.connect(path)
            locker.execute("BEGIN EXCLUSIVE")

        query_task = asyncio.create_task(
            store.query(
                EventQuery(99, 102, frozenset({"catalog"}), 1),
                QueryAccess(frozenset({"catalog"}), 1, 5, True),
            )
        )
        await asyncio.sleep(0)
        if waiting == "database_lock":
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
        assert not query_task.done()
        current = 201 if expired == "summary" else 121
        release.set()
        if locker is not None:
            locker.rollback()
        rows = await asyncio.wait_for(query_task, 1)
        if expired == "summary":
            assert rows == []
        else:
            assert len(rows) == 1
            assert rows[0]["detail"] is None
            assert rows[0]["summary"]["detail_state"] == "expired"
    finally:
        release.set()
        if locker is not None:
            locker.rollback()
            locker.close()
        if blocker is not None:
            await blocker
        if query_task is not None:
            await asyncio.gather(query_task, return_exceptions=True)
        await store.close()


async def test_t27_detail_permission_expiry_and_idempotent_bounded_cleanup(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    try:
        policy = DetailPolicy(1, 100, 10, ("/price",), lambda path, value: value)
        await store.write_batch([event(now=100, detail_policy=policy, details={"price": 42})])
        query = EventQuery(99, 101, frozenset({"catalog"}), 1)
        summary_access = QueryAccess(frozenset({"catalog"}), 2, 10)
        detail_access = QueryAccess(frozenset({"catalog"}), 2, 10, True)
        assert (await store.query(query, summary_access, now=101))[0]["detail"] is None
        assert (await store.query(query, detail_access, now=101))[0]["detail"] == {"/price": 42}
        expired = (await store.query(query, detail_access, now=111))[0]
        assert expired["detail"] is None
        assert expired["summary"]["detail_state"] == "expired"
        assert await store.purge_expired(now=111, batch_size=1) == {
            "details_deleted": 1,
            "summaries_deleted": 0,
            "expired_pending": 0,
        }
        assert (await store.purge_expired(now=111, batch_size=1))["details_deleted"] == 0
        assert (await store.purge_expired(now=201, batch_size=1))["summaries_deleted"] == 1
        assert await store.query(query, detail_access, now=201) == []
    finally:
        await store.close()


def test_cleanup_cli_reopens_store_limits_each_batch_and_preserves_unexpired_data(tmp_path):
    path = tmp_path / "events #1?.sqlite"
    unrelated = tmp_path / "other.sqlite"
    unrelated.write_bytes(b"do not modify this store")
    policy = DetailPolicy(1, 100, 90, ("/price",), lambda path, value: value)
    current = time.time()
    items = [
        event(now=100),
        event(now=100),
        event(now=current, detail_policy=policy, details={"price": 42}),
        event(now=current, detail_policy=policy, details={"price": 24}),
    ]
    items[2].detail_expires_at = current - 1

    async def seed():
        store = SQLiteEventStore(str(path))
        try:
            await store.write_batch(items)
        finally:
            await store.close()

    asyncio.run(seed())

    def purge():
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_migration_proxy.cli",
                "purge-events",
                "--event-store",
                str(path),
                "--batch-size",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    assert purge() == {"details_deleted": 1, "summaries_deleted": 1, "expired_pending": 1}
    assert purge() == {"details_deleted": 0, "summaries_deleted": 1, "expired_pending": 0}
    assert purge() == {"details_deleted": 0, "summaries_deleted": 0, "expired_pending": 0}
    with closing(sqlite3.connect(path)) as db:
        rows = {
            row[0]: row[1:]
            for row in db.execute("SELECT event_id, summary, detail FROM comparison_event")
        }
    assert set(rows) == {items[2].event_id, items[3].event_id}
    assert rows[items[2].event_id][1] is None
    assert json.loads(rows[items[2].event_id][0])["detail_state"] == "expired"
    assert json.loads(rows[items[3].event_id][1]) == {"/price": 24}
    assert unrelated.read_bytes() == b"do not modify this store"


@pytest.mark.parametrize("kind", ["missing", "unrelated", "invalid"])
def test_cleanup_cli_never_creates_or_initializes_wrong_store(tmp_path, capsys, kind):
    path = tmp_path / "private-store.sqlite"
    if kind == "unrelated":
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES ('preserved')")
    elif kind == "invalid":
        path.write_bytes(b"not a database")
    before = path.read_bytes() if path.exists() else None
    assert main(["purge-events", "--event-store", str(path), "--batch-size", "1"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Event cleanup failed; verify the store and retry.\n"
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", str(2**63)])
def test_cleanup_cli_rejects_invalid_batch_without_opening_store(tmp_path, value):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(SystemExit) as error:
        main(["purge-events", "--event-store", str(path), "--batch-size", value])
    assert error.value.code == 2
    assert not path.exists()


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), -float("inf")])
async def test_cleanup_rejects_invalid_clock_without_creating_store(tmp_path, now):
    path = tmp_path / "missing.sqlite"
    store = SQLiteEventStore(str(path))
    try:
        with pytest.raises(ValueError, match="invalid_delete_time"):
            await store.purge_expired(now=now, batch_size=1)
        assert not path.exists()
    finally:
        await store.close()


def test_cleanup_cli_locked_store_fails_without_deleting_data_and_can_retry(tmp_path, capsys):
    path = tmp_path / "events.sqlite"

    async def seed():
        store = SQLiteEventStore(str(path))
        try:
            await store.write_batch([event(now=100)])
        finally:
            await store.close()

    asyncio.run(seed())
    args = ["purge-events", "--event-store", str(path), "--batch-size", "1"]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        assert main(args) == 2
        assert db.execute("SELECT count(*) FROM comparison_event").fetchone()[0] == 1
    assert capsys.readouterr().err == "Event cleanup failed; verify the store and retry.\n"
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["summaries_deleted"] == 1


async def test_submit_snapshots_caller_data_and_never_buffers_invalid_payload():
    store = AcknowledgingStore()
    collector = BoundedCollector(store, limits())
    item = event()
    assert collector.submit(item)
    item.summary["route_id"] = "changed-after-submit"
    await collector.flush()
    assert store.events[0].summary["route_id"] == "catalog"
    invalid = event()
    invalid.summary["broken"] = object()
    assert not collector.submit(invalid)
    assert collector.counters["dropped_invalid"] == 1


async def test_t24_ack_lost_after_insert_recovers_same_event_and_counts_once(tmp_path):
    actual = SQLiteEventStore(str(tmp_path / "events.sqlite"))

    class LoseFirstAck:
        def __init__(self):
            self.calls = 0

        async def write_batch(self, events):
            self.calls += 1
            result = await actual.write_batch(events)
            if self.calls == 1:
                raise TimeoutError()
            return result

    try:
        counted = []
        collector = BoundedCollector(LoseFirstAck(), limits(), on_stored=counted.append)
        item = event(now=100)
        collector.submit(item)
        await collector.flush()
        assert counted == [item.event_id]
        rows = await actual.query(
            EventQuery(99, 102, frozenset({"catalog"}), 10),
            QueryAccess(frozenset({"catalog"}), 10, 5),
            now=101,
        )
        assert len(rows) == 1
        assert collector.counters["ack_unknown"] == 0
        assert collector.metrics()["completeness_known"] is True
    finally:
        await actual.close()


async def test_sqlite_close_is_idempotent_and_closed_operations_fail_promptly(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    await store.write_batch([event()])
    await asyncio.gather(store.close(), store.close())
    await store.close()
    for _ in range(2):
        async with asyncio.timeout(1):
            with pytest.raises(RuntimeError, match="closed"):
                await store.write_batch([event()])


async def test_cancelled_sqlite_close_still_releases_connection_and_rejects_queued_writes(
    tmp_path, monkeypatch
):
    import threading

    store = SQLiteEventStore(str(tmp_path / "events.sqlite"))
    entered, release = threading.Event(), threading.Event()
    original_db = store._db

    def delayed_db():
        db = original_db()
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release SQLite operation")
        return db

    monkeypatch.setattr(store, "_db", delayed_db)
    first = asyncio.create_task(store.write_batch([event()]))
    queued = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        queued = asyncio.create_task(store.write_batch([event()]))
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(store.close(), 0.01)
    finally:
        release.set()
        await first
        if queued is not None:
            with pytest.raises(RuntimeError, match="closed"):
                await queued
        await store.close()
    assert store._connection is None
