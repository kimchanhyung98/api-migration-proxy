from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .collector import BatchResult
from .events import CollectionEvent, _finite, _utc
from .query import EventQuery, QueryAccess


class SQLiteEventStore:
    """SQLite event storage for the Proxy CLI; production storage remains undecided."""

    def __init__(self, path: str, *, create: bool = True):
        self._path = path
        self._create = create
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comparison-store")
        self._slot = asyncio.Semaphore(1)
        self._connection: sqlite3.Connection | None = None
        self._closing: asyncio.Future | None = None
        self.counters: Counter[str] = Counter()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            if not self._create:
                self._connection = sqlite3.connect(
                    Path(self._path).resolve().as_uri() + "?mode=rw", uri=True, timeout=1
                )
                return self._connection
            self._connection = sqlite3.connect(self._path, timeout=1)
            self._connection.execute("""CREATE TABLE IF NOT EXISTS comparison_event (
                event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, summary_expires_at REAL NOT NULL,
                detail_expires_at REAL, route_id TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
                configuration_revision TEXT NOT NULL, comparison_policy_revision TEXT NOT NULL,
                summary TEXT NOT NULL, detail TEXT, stored_at REAL NOT NULL
            )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS event_route_time ON comparison_event(route_id, created_at)"
            )
        return self._connection

    async def _run(self, operation: Callable[[], Any]) -> Any:
        if self._closing is not None:
            raise RuntimeError("event store is closed")
        await self._slot.acquire()
        try:
            if self._closing is not None:
                raise RuntimeError("event store is closed")
            future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        except BaseException:
            self._slot.release()
            raise
        future.add_done_callback(lambda _: self._slot.release())
        return await asyncio.shield(future)

    async def write_batch(self, events: Sequence[CollectionEvent]) -> BatchResult:
        def write() -> BatchResult:
            db = self._db()
            with db:
                for event in events:
                    summary = dict(event.summary)
                    if event.detail is not None:
                        summary["detail_state"] = "stored"
                    db.execute(
                        "INSERT OR IGNORE INTO comparison_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                            json.dumps(event.detail, allow_nan=False)
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
        current = time.time() if now is None else now

        def read() -> list[dict[str, Any]]:
            clauses = [
                "created_at >= ?",
                "created_at < ?",
                "summary_expires_at > ?",
                f"route_id IN ({','.join('?' for _ in query.routes)})",
            ]
            parameters: list[Any] = [query.start, query.end, current, *sorted(query.routes)]
            for name in (
                "result",
                "reason",
                "configuration_revision",
                "comparison_policy_revision",
                "event_id",
            ):
                value = getattr(query, name)
                if value is not None:
                    clauses.append(f"{name} = ?")
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
                            filters.append(
                                f"json_extract(summary, '$.backends.{backend}.{name}') = ?"
                            )
                            parameters.append(value)
                    backend_clauses.append("(" + " AND ".join(filters or ["1 = 1"]) + ")")
                clauses.append("(" + " OR ".join(backend_clauses) + ")")
            rows = (
                self._db()
                .execute(
                    "SELECT event_id, created_at, summary_expires_at, detail_expires_at, summary, detail, stored_at FROM comparison_event WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at, event_id LIMIT ?",
                    (*parameters, query.limit),
                )
                .fetchall()
            )
            results = []
            for event_id, created, expires, detail_expires, raw, detail, stored in rows:
                summary = json.loads(raw)
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
                        "detail": json.loads(detail)
                        if detail and access.allow_details and not expired
                        else None,
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

        def purge() -> dict[str, int]:
            db = self._db()
            with db:
                details = db.execute(
                    "UPDATE comparison_event SET detail = NULL, summary = json_set(summary, '$.detail_state', 'expired') WHERE event_id IN (SELECT event_id FROM comparison_event WHERE detail IS NOT NULL AND detail_expires_at <= ? LIMIT ?)",
                    (current, batch_size),
                ).rowcount
                summaries = db.execute(
                    "DELETE FROM comparison_event WHERE event_id IN (SELECT event_id FROM comparison_event WHERE summary_expires_at <= ? LIMIT ?)",
                    (current, batch_size),
                ).rowcount
            pending = db.execute(
                "SELECT count(*) FROM comparison_event WHERE summary_expires_at <= ? OR (detail IS NOT NULL AND detail_expires_at <= ?)",
                (current, current),
            ).fetchone()[0]
            return {
                "details_deleted": details,
                "summaries_deleted": summaries,
                "expired_pending": pending,
            }

        try:
            outcome = await self._run(purge)
        except Exception:
            self.counters["purge_failures"] += 1
            raise
        self.counters["expired_pending"] = outcome["expired_pending"]
        return outcome

    async def close(self) -> None:
        def close_db() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

        if self._closing is None:
            # The single executor closes the connection after any in-flight write,
            # even when its caller has stopped waiting for an acknowledgement.
            self._closing = asyncio.get_running_loop().run_in_executor(self._executor, close_db)
            self._executor.shutdown(wait=False)
        await asyncio.shield(self._closing)
