from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Sequence
from traceback import clear_frames
from typing import Any

import psycopg

from .collector import BatchResult
from .events import CollectionEvent, _finite, _utc
from .query import EventQuery, QueryAccess

_OPERATION_TIMEOUT_SECONDS = 10


class PostgreSQLEventStore:
    def __init__(self, dsn: str, *, create: bool = True):
        self._dsn = dsn
        self._create = create
        self._slot = asyncio.Lock()
        self._connection: psycopg.AsyncConnection | None = None
        self._closing: asyncio.Task[None] | None = None
        self.counters: Counter[str] = Counter()

    async def _db(self) -> psycopg.AsyncConnection:
        if self._connection is None or self._connection.closed:
            self._connection = await psycopg.AsyncConnection.connect(
                self._dsn, autocommit=True, connect_timeout=5
            )
            await self._connection.execute("SET statement_timeout = '5s'")
            await self._connection.execute("SET lock_timeout = '1s'")
            await self._connection.execute("SET idle_in_transaction_session_timeout = '10s'")
            if self._create:
                async with self._connection.transaction():
                    # Serialize first-time DDL across proxy processes.
                    await self._connection.execute("SELECT pg_advisory_xact_lock(684204682412)")
                    await self._connection.execute("""CREATE TABLE IF NOT EXISTS comparison_event (
                        event_id TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL,
                        summary_expires_at DOUBLE PRECISION NOT NULL,
                        detail_expires_at DOUBLE PRECISION, route_id TEXT NOT NULL,
                        result TEXT NOT NULL, reason TEXT NOT NULL,
                        configuration_revision TEXT NOT NULL,
                        comparison_policy_revision TEXT NOT NULL,
                        summary JSON NOT NULL, backends JSONB NOT NULL,
                        detail JSON, stored_at DOUBLE PRECISION NOT NULL
                    )""")
                    for name, columns in (
                        ("event_route_time", "route_id, created_at"),
                        ("event_summary_expiry", "summary_expires_at"),
                        ("event_detail_expiry", "detail_expires_at"),
                    ):
                        await self._connection.execute(
                            f"CREATE INDEX IF NOT EXISTS {name} ON comparison_event({columns})"
                        )
        return self._connection

    async def _disconnect(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def _stop(self, task: asyncio.Task) -> None:
        # Close first so cancellation never waits on another network round trip.
        await self._disconnect()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        failure = None if task.cancelled() else task.exception()
        while failure is not None:
            clear_frames(failure.__traceback__)
            failure.__traceback__ = None
            previous = failure.__context__ or failure.__cause__
            failure.__context__ = failure.__cause__ = None
            failure = previous

    async def _run(self, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        if self._closing is not None:
            raise RuntimeError("event store is closed")
        async with self._slot:
            if self._closing is not None:
                raise RuntimeError("event store is closed")
            task = asyncio.create_task(operation())
            try:
                done, _ = await asyncio.wait({task}, timeout=_OPERATION_TIMEOUT_SECONDS)
                if not done:
                    raise TimeoutError
                result = task.result()
            except asyncio.CancelledError:
                await self._stop(task)
                raise
            except Exception:
                await self._stop(task)
            else:
                return result
            # Raising outside the handler drops driver diagnostics and traceback chains.
            raise RuntimeError("event store operation failed")

    async def write_batch(self, events: Sequence[CollectionEvent]) -> BatchResult:
        async def write() -> BatchResult:
            db = await self._db()
            async with db.transaction():
                for event in events:
                    summary = dict(event.summary)
                    if event.detail is not None:
                        summary["detail_state"] = "stored"
                    await db.execute(
                        "INSERT INTO comparison_event (event_id, created_at, summary_expires_at, detail_expires_at, route_id, result, reason, configuration_revision, comparison_policy_revision, summary, backends, detail, stored_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::json, %s::jsonb, %s::json, %s) ON CONFLICT (event_id) DO NOTHING",
                        (
                            event.event_id,
                            event.created_at,
                            event.summary_expires_at,
                            event.detail_expires_at,
                            summary["route_id"],
                            summary["comparison"]["result"],
                            summary["comparison"]["reason"],
                            summary["configuration_revision"],
                            summary["comparison_policy_revision"],
                            json.dumps(summary, allow_nan=False),
                            json.dumps(summary["backends"], allow_nan=False),
                            json.dumps(event.detail, allow_nan=False, ensure_ascii=False)
                            if event.detail is not None
                            else None,
                            time.time(),
                        ),
                    )
            return BatchResult(acknowledged=frozenset(event.event_id for event in events))

        return await self._run(write)

    async def query(
        self, query: EventQuery, access: QueryAccess, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        if not query.routes or not query.routes <= access.routes:
            raise PermissionError("query_route_denied")
        if (
            not all(_finite(value) for value in (query.start, query.end, access.max_period_seconds))
            or not 0 < query.end - query.start <= access.max_period_seconds
            or type(query.limit) is not int
            or not 0 < query.limit <= access.max_rows
        ):
            raise ValueError("query_limit_exceeded")
        if query.backend not in (None, "v1", "v2") or query.role not in (None, "serving", "shadow"):
            raise ValueError("invalid_backend_filter")
        if now is not None and not _finite(now):
            raise ValueError("invalid_query_time")

        async def read() -> list[dict[str, Any]]:
            db = await self._db()
            current = time.time() if now is None else now
            clauses = [
                "created_at >= %s",
                "created_at < %s",
                "summary_expires_at > %s",
                "route_id = ANY(%s)",
            ]
            parameters: list[Any] = [query.start, query.end, current, sorted(query.routes)]
            for name in (
                "result",
                "reason",
                "configuration_revision",
                "comparison_policy_revision",
                "event_id",
            ):
                value = getattr(query, name)
                if value is not None:
                    clauses.append(f"{name} = %s")
                    parameters.append(value)
            if query.backend or query.role or query.deployment_revision:
                backend_clauses = []
                for backend in (query.backend,) if query.backend else ("v1", "v2"):
                    filters = []
                    for name, value in (
                        ("role", query.role),
                        ("deployment_revision", query.deployment_revision),
                    ):
                        if value is not None:
                            filters.append(f"backends #>> '{{{backend},{name}}}' = %s")
                            parameters.append(value)
                    backend_clauses.append("(" + " AND ".join(filters or ["TRUE"]) + ")")
                clauses.append("(" + " OR ".join(backend_clauses) + ")")
            cursor = await db.execute(
                "SELECT event_id, created_at, summary_expires_at, detail_expires_at, summary, CASE WHEN %s AND detail_expires_at > %s THEN detail END, stored_at FROM comparison_event WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, event_id LIMIT %s",
                (access.allow_details, current, *parameters, query.limit),
            )
            rows = await cursor.fetchall()
            current = time.time() if now is None else now
            results = []
            for (
                event_id,
                created,
                expires,
                detail_expires,
                summary,
                detail,
                stored,
            ) in rows:
                if expires <= current:
                    continue
                expired = detail_expires is not None and detail_expires <= current
                if expired and summary["detail_state"] == "stored":
                    summary["detail_state"] = "expired"
                results.append(
                    {
                        "event_id": event_id,
                        "created_at": _utc(created),
                        "summary_expires_at": _utc(expires),
                        "stored_at": _utc(stored),
                        "summary": summary,
                        "detail": detail if access.allow_details and not expired else None,
                    }
                )
            return results

        return await self._run(read)

    async def purge_expired(self, *, now: float | None = None, batch_size: int) -> dict[str, int]:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("invalid_delete_batch")
        current = time.time() if now is None else now
        if not _finite(current):
            raise ValueError("invalid_delete_time")

        async def purge() -> dict[str, int]:
            db = await self._db()
            async with db.transaction():
                details = await db.execute(
                    "SELECT event_id, summary FROM comparison_event WHERE detail IS NOT NULL AND detail_expires_at <= %s ORDER BY detail_expires_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (current, batch_size),
                )
                updates = []
                for event_id, summary in await details.fetchall():
                    summary["detail_state"] = "expired"
                    updates.append((json.dumps(summary, allow_nan=False), event_id))
                async with db.cursor() as cursor:
                    await cursor.executemany(
                        "UPDATE comparison_event SET detail = NULL, summary = %s::json WHERE event_id = %s",
                        updates,
                    )
                    details_deleted = cursor.rowcount
                summaries = await db.execute(
                    "DELETE FROM comparison_event WHERE event_id IN (SELECT event_id FROM comparison_event WHERE summary_expires_at <= %s ORDER BY summary_expires_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED)",
                    (current, batch_size),
                )
                pending = await db.execute(
                    "SELECT count(*) FROM comparison_event WHERE summary_expires_at <= %s OR (detail IS NOT NULL AND detail_expires_at <= %s)",
                    (current, current),
                )
                row = await pending.fetchone()
            assert row is not None
            return {
                "details_deleted": details_deleted,
                "summaries_deleted": summaries.rowcount,
                "expired_pending": row[0],
            }

        try:
            outcome = await self._run(purge)
        except Exception:
            self.counters["purge_failures"] += 1
            raise
        self.counters["expired_pending"] = outcome["expired_pending"]
        return outcome

    async def close(self) -> None:
        async def close_db() -> None:
            async with self._slot:
                await self._disconnect()

        if self._closing is None:
            self._closing = asyncio.create_task(close_db())
        await asyncio.shield(self._closing)
