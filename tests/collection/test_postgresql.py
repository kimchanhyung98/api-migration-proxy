import asyncio
import gc
import os
import time
import traceback
import uuid
import weakref
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from api_migration_proxy.collection import postgresql
from api_migration_proxy.collection.collector import BoundedCollector, CollectionLimits
from api_migration_proxy.collection.events import DetailPolicy, make_event
from api_migration_proxy.collection.postgresql import PostgreSQLEventStore
from api_migration_proxy.collection.query import EventQuery, QueryAccess
from api_migration_proxy.collection.sqlite import SQLiteEventStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("API_PROXY_TEST_POSTGRES_DSN"),
    reason="API_PROXY_TEST_POSTGRES_DSN must name an isolated test PostgreSQL database",
)


@pytest.fixture
def postgres_dsn():
    schema = "adapter_test_" + uuid.uuid4().hex
    dsn = os.environ["API_PROXY_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={schema}")
        finally:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def event(*, route="catalog", now=100, details=False):
    return make_event(
        {
            "route_id": route,
            "configuration_revision": "config-1",
            "comparison_policy_revision": "compare-1",
            "backends": {
                "v1": {"role": "serving", "deployment_revision": "v1-build"},
                "v2": {"role": "shadow", "deployment_revision": "v2-build"},
            },
            "comparison": {"result": "different", "reason": "json_value_mismatch"},
            "authorization": "must-not-be-stored",
        },
        retention_seconds=100,
        now=now,
        detail_policy=DetailPolicy(1, 100, 10, ("/price",), lambda path, value: value)
        if details
        else None,
        details={"price": "한" * 20, "password": "must-not-be-stored"},
    )


def query():
    return EventQuery(99, 102, frozenset({"catalog"}), 10)


def access(*, details=False):
    return QueryAccess(frozenset({"catalog"}), 10, 10, details)


async def test_concurrent_initialization_and_dedup_preserve_first_write(postgres_dsn):
    stores = [PostgreSQLEventStore(postgres_dsn) for _ in range(2)]
    item = event(details=True)
    try:
        results = await asyncio.gather(*(store.write_batch([item]) for store in stores))
        assert all(result.acknowledged == {item.event_id} for result in results)
        original = await stores[0].query(query(), access(details=True), now=101)
        assert len(original) == 1
        item.summary_expires_at = item.detail_expires_at = 1000
        item.detail = {"/price": "changed"}
        await stores[1].write_batch([item])
        assert await stores[0].query(query(), access(details=True), now=101) == original
        assert "must-not-be-stored" not in str(original)
        assert original[0]["detail"] == {"/price": "한" * 20}
        assert original[0]["summary"]["detail_state"] == "stored"
        with psycopg.connect(postgres_dsn) as db:
            assert db.execute("SELECT count(*) FROM comparison_event").fetchone() == (1,)
    finally:
        await asyncio.gather(*(store.close() for store in stores))


async def test_write_uses_column_names_with_precreated_table(postgres_dsn):
    with psycopg.connect(postgres_dsn, autocommit=True) as db:
        db.execute("""CREATE TABLE comparison_event (
            event_id TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL,
            summary_expires_at DOUBLE PRECISION NOT NULL, detail_expires_at DOUBLE PRECISION,
            result TEXT NOT NULL, route_id TEXT NOT NULL, reason TEXT NOT NULL,
            configuration_revision TEXT NOT NULL, comparison_policy_revision TEXT NOT NULL,
            summary JSON NOT NULL, backends JSONB NOT NULL,
            detail JSON, stored_at DOUBLE PRECISION NOT NULL
        )""")
    store = PostgreSQLEventStore(postgres_dsn)
    item = event()
    try:
        assert (await store.write_batch([item])).acknowledged == {item.event_id}
        with psycopg.connect(postgres_dsn) as db:
            assert db.execute("SELECT route_id, result FROM comparison_event").fetchone() == (
                "catalog",
                "different",
            )
        rows = await store.query(replace(query(), result="different"), access(), now=101)
        assert [row["event_id"] for row in rows] == [item.event_id]
    finally:
        await store.close()


@pytest.mark.parametrize("summary_path", ["/value", "/field\x00name", "/field\ud800name"])
async def test_allowed_json_strings_match_sqlite_query_cleanup_and_batch_ack(
    postgres_dsn, tmp_path, summary_path
):
    summary = event().summary
    summary["comparison"]["difference_paths"] = [summary_path]
    item = make_event(
        summary,
        retention_seconds=100,
        now=100,
        detail_policy=DetailPolicy(1, 100, 10, ("/value",), lambda path, value: value),
        details={"value": "allowed\x00payload"},
        allowed_difference_paths=frozenset({summary_path}),
    )
    items = [event(), item]
    stores = [
        SQLiteEventStore(str(tmp_path / "nul-detail.sqlite")),
        PostgreSQLEventStore(postgres_dsn),
    ]
    try:
        expected = {entry.event_id for entry in items}
        outcomes = []
        filtered = replace(query(), backend="v2", role="shadow", deployment_revision="v2-build")
        for store in stores:
            result = await store.write_batch(items)
            assert result.acknowledged == expected
            rows = await store.query(filtered, access(details=True), now=101)
            assert {row["event_id"] for row in rows} == expected
            row = next(row for row in rows if row["event_id"] == item.event_id)
            assert row["detail"] == {"/value": "allowed\x00payload"}
            assert row["summary"]["detail_state"] == "stored"
            assert row["summary"]["comparison"]["difference_paths"] == [summary_path]
            assert all(
                row["detail"] is None for row in await store.query(filtered, access(), now=101)
            )
            expired = await store.query(
                replace(filtered, event_id=item.event_id), access(details=True), now=110
            )
            assert expired[0]["detail"] is None
            assert expired[0]["summary"]["detail_state"] == "expired"
            assert (await store.purge_expired(now=110, batch_size=1))["details_deleted"] == 1
            after = await store.query(filtered, access(details=True), now=110)
            outcomes.append(
                [{key: value for key, value in row.items() if key != "stored_at"} for row in after]
            )
            assert await store.query(filtered, access(details=True), now=200) == []
        assert outcomes[0] == outcomes[1]
    finally:
        await asyncio.gather(*(store.close() for store in stores))


async def test_failed_batch_rolls_back_and_sanitizes_exception(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    valid, invalid = event(), event()
    invalid.summary["route_id"] = None
    invalid.summary["configuration_revision"] = "private-error-marker"
    try:
        with pytest.raises(RuntimeError, match="^event store operation failed$") as failure:
            await store.write_batch([valid, invalid])
        assert failure.value.__context__ is None
        assert failure.value.__cause__ is None
        output = "".join(traceback.format_exception(failure.value))
        assert postgres_dsn not in output
        assert "private-error-marker" not in output
        assert await store.query(query(), access(), now=101) == []
        assert (await store.write_batch([valid])).acknowledged == {valid.event_id}
    finally:
        await store.close()


async def test_connection_errors_do_not_expose_dsn_or_driver_context(postgres_dsn, caplog):
    private_dsn = make_conninfo(
        postgres_dsn, port=1, user="private-user-marker", password="private-password-marker"
    )
    store = PostgreSQLEventStore(private_dsn)
    try:
        with pytest.raises(RuntimeError, match="^event store operation failed$") as failure:
            await store.write_batch([event()])
        assert failure.value.__context__ is None
        output = repr(store) + str(failure.value) + caplog.text
        assert private_dsn not in output
        assert "private-user-marker" not in output
        assert "private-password-marker" not in output
    finally:
        await store.close()


async def test_query_scope_filters_and_detail_expiry(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    item = event(details=True)
    try:
        await store.write_batch([item, event(route="private"), event(now=102)])
        filtered = replace(
            query(),
            result="different",
            reason="json_value_mismatch",
            configuration_revision="config-1",
            comparison_policy_revision="compare-1",
            event_id=item.event_id,
            backend="v2",
            role="shadow",
            deployment_revision="v2-build",
        )
        rows = await store.query(filtered, access(), now=101)
        assert [row["event_id"] for row in rows] == [item.event_id]
        assert rows[0]["detail"] is None
        assert (await store.query(filtered, access(details=True), now=109))[0]["detail"]
        expired = (await store.query(filtered, access(details=True), now=110))[0]
        assert expired["detail"] is None
        assert expired["summary"]["detail_state"] == "expired"
        assert await store.query(filtered, access(details=True), now=200) == []
        assert (
            await store.query(
                replace(filtered, backend=None, role="serving", deployment_revision="v2-build"),
                access(),
                now=101,
            )
            == []
        )
        assert len(await store.query(replace(filtered, backend=None), access(), now=101)) == 1
    finally:
        await store.close()


@pytest.mark.parametrize("wait_at", ["slot", "database"])
@pytest.mark.parametrize("expires", ["summary", "detail"])
async def test_query_rechecks_default_clock_after_waiting(
    postgres_dsn, monkeypatch, wait_at, expires
):
    store = PostgreSQLEventStore(postgres_dsn)
    item = event(details=True)
    if expires == "summary":
        item.summary_expires_at = 110
    await store.write_batch([item])
    clock = [101]
    monkeypatch.setattr(postgresql.time, "time", lambda: clock[0])
    reading = None
    try:
        if wait_at == "slot":
            await store._slot.acquire()
            try:
                reading = asyncio.create_task(store.query(query(), access(details=True)))
                await asyncio.sleep(0)
                clock[0] = 111
            finally:
                store._slot.release()
        else:
            with psycopg.connect(postgres_dsn) as locker:
                locker.execute("LOCK TABLE comparison_event IN ACCESS EXCLUSIVE MODE")
                reading = asyncio.create_task(store.query(query(), access(details=True)))
                with psycopg.connect(postgres_dsn, autocommit=True) as observer:
                    async with asyncio.timeout(1):
                        while observer.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                            (store._connection.info.backend_pid,),
                        ).fetchone() != ("Lock",):
                            await asyncio.sleep(0)
                clock[0] = 111
        rows = await reading
        if expires == "summary":
            assert rows == []
        else:
            assert rows[0]["detail"] is None
            assert rows[0]["summary"]["detail_state"] == "expired"
        fixed = await store.query(query(), access(details=True), now=101)
        assert fixed[0]["detail"] == item.detail
        assert fixed[0]["summary"]["detail_state"] == "stored"
    finally:
        if reading is not None:
            await asyncio.gather(reading, return_exceptions=True)
        await store.close()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"routes": frozenset()}, PermissionError),
        ({"routes": frozenset({"private"})}, PermissionError),
        ({"start": float("nan")}, ValueError),
        ({"end": float("inf")}, ValueError),
        ({"end": 99}, ValueError),
        ({"end": 200}, ValueError),
        ({"limit": 11}, ValueError),
        ({"limit": True}, ValueError),
        ({"limit": 0}, ValueError),
        ({"backend": "v3"}, ValueError),
        ({"role": "other"}, ValueError),
    ],
)
async def test_invalid_queries_never_open_connection(postgres_dsn, changes, error):
    store = PostgreSQLEventStore(postgres_dsn)
    try:
        with pytest.raises(error):
            await store.query(replace(query(), **changes), access(), now=101)
        assert store._connection is None
    finally:
        await store.close()


async def test_cleanup_is_bounded_and_keeps_unexpired_rows(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    items = [event(details=True) for _ in range(3)] + [event(now=150, details=True)]
    try:
        await store.write_batch(items)
        assert await store.purge_expired(now=110, batch_size=1) == {
            "details_deleted": 1,
            "summaries_deleted": 0,
            "expired_pending": 2,
        }
        assert store.counters["expired_pending"] == 2
        assert await store.purge_expired(now=200, batch_size=1) == {
            "details_deleted": 1,
            "summaries_deleted": 1,
            "expired_pending": 3,
        }
        for _ in range(3):
            outcome = await store.purge_expired(now=200, batch_size=1)
            assert outcome["details_deleted"] <= 1
            assert outcome["summaries_deleted"] <= 1
        assert await store.purge_expired(now=200, batch_size=1) == {
            "details_deleted": 0,
            "summaries_deleted": 0,
            "expired_pending": 0,
        }
        with psycopg.connect(postgres_dsn) as db:
            assert db.execute("SELECT event_id FROM comparison_event").fetchall() == [
                (items[-1].event_id,)
            ]
    finally:
        await store.close()


async def test_create_false_never_initializes_missing_schema(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn, create=False)
    try:
        with pytest.raises(RuntimeError, match="event store operation failed"):
            await store.purge_expired(now=200, batch_size=1)
        assert store.counters["purge_failures"] == 1
        with psycopg.connect(postgres_dsn) as db:
            assert db.execute("SELECT to_regclass('comparison_event')").fetchone() == (None,)
    finally:
        await store.close()


async def test_cleanup_existing_schema_and_failure_recovery(postgres_dsn):
    seed = PostgreSQLEventStore(postgres_dsn)
    await seed.write_batch([event()])
    await seed.close()
    store = PostgreSQLEventStore(postgres_dsn, create=False)
    try:
        with psycopg.connect(postgres_dsn) as locker:
            locker.execute("LOCK TABLE comparison_event IN ACCESS EXCLUSIVE MODE")
            with pytest.raises(RuntimeError, match="event store operation failed"):
                await store.purge_expired(now=200, batch_size=1)
        assert store.counters["purge_failures"] == 1
        assert (await store.purge_expired(now=200, batch_size=1))["summaries_deleted"] == 1
    finally:
        await store.close()


@pytest.mark.parametrize("failure", [False, True])
async def test_collector_timeout_retains_reservation_until_write_finishes(
    postgres_dsn, monkeypatch, failure
):
    store = PostgreSQLEventStore(postgres_dsn)
    await store.write_batch([])
    original_write = store.write_batch
    references, outcomes = [], []
    unknown = asyncio.Event()

    async def write(events):
        references.extend(weakref.ref(item) for item in events)
        return await original_write(events)

    def outcome(item, result):
        outcomes.append(result)
        unknown.set()

    monkeypatch.setattr(store, "write_batch", write)
    collector = BoundedCollector(
        store,
        CollectionLimits(1, 10_000, 10, 1, 0.01, 1, 0),
        on_outcome=outcome,
    )
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with psycopg.connect(postgres_dsn) as locker:
            locker.execute("LOCK TABLE comparison_event IN ACCESS EXCLUSIVE MODE")
            assert collector.submit(event(details=True))
            await asyncio.wait_for(unknown.wait(), 1)
            assert collector.metrics()["queue_depth"] == 1
            assert collector.metrics()["queue_bytes"] > 0
            assert references[0]() is not None
            assert not collector.submit(event())
            if failure:
                await asyncio.wait_for(collector.flush(), 3)
        await collector.flush()
        assert collector.metrics()["queue_depth"] == 0
        assert collector.metrics()["queue_bytes"] == 0
        assert references[0]() is None
        assert outcomes == ["ack_unknown"]
        assert collector.counters["stored"] == 0
        assert collector.counters["retries"] == 0
        assert collector.completeness_known is False
        with psycopg.connect(postgres_dsn) as db:
            assert db.execute("SELECT count(*) FROM comparison_event").fetchone() == (
                0 if failure else 1,
            )
    finally:
        await collector.close(1)
        await store.close()
        if was_enabled:
            gc.enable()


async def test_operation_deadline_disconnects_and_recovers(postgres_dsn, monkeypatch):
    store = PostgreSQLEventStore(postgres_dsn)
    await store.write_batch([])
    connection = store._connection
    monkeypatch.setattr(postgresql, "_OPERATION_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()

    async def slow():
        await connection.execute("SELECT pg_sleep(30)")

    try:
        with pytest.raises(RuntimeError, match="event store operation failed"):
            await store._run(slow)
        assert time.monotonic() - started < 1
        assert connection.closed
        monkeypatch.setattr(postgresql, "_OPERATION_TIMEOUT_SECONDS", 10)
        assert (await store.write_batch([event()])).acknowledged
    finally:
        await store.close()


async def test_cancel_and_concurrent_close_finish_without_reusing_connection(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    await store.write_batch([])
    connection = store._connection
    entered = asyncio.Event()

    async def slow():
        entered.set()
        await connection.execute("SELECT pg_sleep(30)")

    task = asyncio.create_task(store._run(slow))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed
    await asyncio.gather(store.close(), store.close())
    with pytest.raises(RuntimeError, match="event store is closed"):
        await store.write_batch([event()])


async def test_cancelled_write_releases_payload_without_garbage_collection(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    await store.write_batch([])
    connection = store._connection
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with psycopg.connect(postgres_dsn) as locker:
            locker.execute("LOCK TABLE comparison_event IN ACCESS EXCLUSIVE MODE")
            item = event(details=True)
            reference = weakref.ref(item)
            writing = asyncio.create_task(store.write_batch([item]))
            del item
            async with asyncio.timeout(1):
                while connection.pgconn.transaction_status != psycopg.pq.TransactionStatus.ACTIVE:
                    await asyncio.sleep(0)
            writing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writing
            del writing
        await asyncio.sleep(0)
        assert reference() is None
        assert connection.closed
    finally:
        await store.close()
        if was_enabled:
            gc.enable()


async def test_close_waits_for_write_and_survives_cancelled_waiter(postgres_dsn):
    store = PostgreSQLEventStore(postgres_dsn)
    await store.write_batch([])
    connection = store._connection
    with psycopg.connect(postgres_dsn) as locker:
        locker.execute("LOCK TABLE comparison_event IN ACCESS EXCLUSIVE MODE")
        writing = asyncio.create_task(store.write_batch([event()]))
        while not store._slot.locked():
            await asyncio.sleep(0)
        closing = asyncio.create_task(store.close())
        while store._closing is None:
            await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not connection.closed
    assert (await writing).acknowledged
    await store.close()
    assert connection.closed


@pytest.mark.parametrize("batch_size", [True, 0, -1, 1.5])
async def test_invalid_cleanup_batch_never_opens_connection(postgres_dsn, batch_size):
    store = PostgreSQLEventStore(postgres_dsn)
    try:
        with pytest.raises(ValueError, match="invalid_delete_batch"):
            await store.purge_expired(now=200, batch_size=batch_size)
        assert store._connection is None
    finally:
        await store.close()


@pytest.mark.parametrize("now", [True, float("nan"), float("inf")])
async def test_invalid_cleanup_clock_never_opens_connection(postgres_dsn, now):
    store = PostgreSQLEventStore(postgres_dsn)
    try:
        with pytest.raises(ValueError, match="invalid_delete_time"):
            await store.purge_expired(now=now, batch_size=1)
        with pytest.raises(ValueError, match="invalid_query_time"):
            await store.query(query(), access(), now=now)
        assert store._connection is None
    finally:
        await store.close()
