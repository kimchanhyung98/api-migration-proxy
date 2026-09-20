from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import re
import sqlite3
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_PRIVATE = re.compile(
    r"authorization|cookie|token|password|secret|credential|api[_-]?key|signature", re.I
)
_SUMMARY_CODES = frozenset(
    "route_id environment migration_id configuration_revision comparison_policy_revision "
    "epoch_id request_id serving_backend response_source request_outcome assignment_method cohort_class".split()
)
_SUMMARY_TIMES = frozenset(
    "request_started_at request_ended_at backend_completed_at comparison_completed_at".split()
)
_BACKEND_CODES = frozenset(
    "backend role deployment_revision execution_outcome contract_class capture_state reason".split()
)


def _code(value: object) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else "unknown"


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value: object) -> float | int | None:
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and _finite(value)
        and value >= 0
        else None
    )


def _utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "unknown"
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return "unknown"


def _summary(source: Mapping[str, Any], allowed_paths: frozenset[str]) -> dict[str, Any]:
    result: dict[str, Any] = {key: _code(source.get(key)) for key in _SUMMARY_CODES}
    result.update({key: _timestamp(source.get(key)) for key in _SUMMARY_TIMES})
    result.update(
        {key: source.get(key) is True for key in ("shadow_selected", "shadow_dispatched")}
    )
    trace = source.get("trace_id")
    if isinstance(trace, str) and re.fullmatch(r"[a-f0-9]{32}", trace) and int(trace, 16):
        result["trace_id"] = trace
    backends = source.get("backends", {})
    result["backends"] = {}
    for name in ("v1", "v2"):
        raw = backends.get(name, {}) if isinstance(backends, Mapping) else {}
        raw = raw if isinstance(raw, Mapping) else {}
        backend: dict[str, Any] = {key: _code(raw.get(key)) for key in _BACKEND_CODES}
        backend["backend"] = name
        backend.update({key: _number(raw.get(key)) for key in ("duration_ms", "response_bytes")})
        status = raw.get("status_code")
        backend["status_code"] = status if type(status) is int and 100 <= status <= 599 else None
        backend["response_complete"] = raw.get("response_complete") is True
        result["backends"][name] = backend
    raw = source.get("comparison", {})
    raw = raw if isinstance(raw, Mapping) else {}
    comparison: dict[str, Any] = {
        key: _code(raw.get(key)) for key in ("result", "reason", "comparison_class")
    }
    comparison["difference_count"] = _number(raw.get("difference_count"))
    comparison["differences_truncated"] = raw.get("differences_truncated") is True
    paths = raw.get("difference_paths", ())
    paths = paths if isinstance(paths, (tuple, list)) else ()
    comparison["difference_paths"] = [
        path
        for path in paths[:64]
        if isinstance(path, str)
        and path in allowed_paths
        and len(path) <= 256
        and not _PRIVATE.search(path)
    ]
    comparison["paths_truncated"] = raw.get("paths_truncated") is True or len(paths) != len(
        comparison["difference_paths"]
    )
    rules = raw.get("applied_rules", ())
    comparison["applied_rules"] = (
        [_code(rule) for rule in rules[:64]] if isinstance(rules, (tuple, list)) else []
    )
    result["comparison"] = comparison
    context = source.get("context", {})
    result["context"] = (
        {key: _code(context.get(key)) for key in ("source", "revision", "freshness")}
        if isinstance(context, Mapping)
        else {}
    )
    return result


@dataclass(frozen=True)
class DetailPolicy:
    ratio: float = 0
    max_bytes: int = 0
    retention_seconds: float = 0
    allowed_paths: tuple[str, ...] = ()
    masker: Callable[[str, object], object] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not _finite(self.ratio) or not 0 <= self.ratio <= 1:
            raise ValueError("invalid_detail_ratio")
        if self.ratio and (
            type(self.max_bytes) is not int
            or self.max_bytes <= 0
            or not _finite(self.retention_seconds)
            or self.retention_seconds <= 0
        ):
            raise ValueError("detail_limits_required")
        if any(
            not path.startswith("/") or len(path) > 256 or _PRIVATE.search(path)
            for path in self.allowed_paths
        ):
            raise ValueError("unsafe_detail_path")


def _detail(
    source: Mapping[str, Any], policy: DetailPolicy
) -> tuple[str, dict[str, object] | None]:
    if policy.masker is None:
        return "masking_failed", None
    try:
        result: dict[str, object] = {}
        for path in policy.allowed_paths:
            current: object = source
            for key in path[1:].split("/"):
                if not isinstance(current, Mapping):
                    current = None
                    break
                current = current.get(key.replace("~1", "/").replace("~0", "~"))
            if current is None:
                continue
            value = policy.masker(path, current)
            if value is None:
                continue
            if type(value) not in (str, bool, int, float) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                return "masking_failed", None
            result[path] = value
            if len(json.dumps(result, ensure_ascii=False).encode()) > policy.max_bytes:
                return "oversized", None
        return ("pending", result) if result else ("no_allowed_fields", None)
    except Exception:
        return "masking_failed", None


@dataclass
class _DeliveryState:
    submitted: bool = False

    def __deepcopy__(self, memo: dict[int, object]) -> _DeliveryState:
        return self


@dataclass
class CollectionEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()), init=False)
    created_at: float
    summary_expires_at: float
    summary: dict[str, Any]
    detail_expires_at: float | None = None
    detail: dict[str, object] | None = None
    created_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _delivery: _DeliveryState = field(default_factory=_DeliveryState, repr=False)


def make_event(
    summary: Mapping[str, Any],
    *,
    retention_seconds: float,
    detail_policy: DetailPolicy | None = None,
    details: Mapping[str, Any] | None = None,
    allowed_difference_paths: frozenset[str] = frozenset(),
    now: float | None = None,
    work_started_at: float | None = None,
    sample: Callable[[], float] = random.random,
) -> CollectionEvent:
    if not _finite(retention_seconds) or retention_seconds <= 0:
        raise ValueError("invalid_summary_retention")
    created = time.time() if now is None else now
    policy = detail_policy or DetailPolicy()
    selected = policy.ratio > 0 and sample() < policy.ratio
    state, detail = (
        _detail(details or {}, policy)
        if selected
        else ("disabled" if policy.ratio == 0 else "not_selected", None)
    )
    safe = _summary(summary, allowed_difference_paths)
    safe.update(
        schema_version=1,
        event_created_at=_utc(created),
        detail_sampled=selected,
        detail_state=state,
    )
    return CollectionEvent(
        created_at=created,
        summary_expires_at=created + retention_seconds,
        summary=safe,
        detail=detail,
        detail_expires_at=created + min(retention_seconds, policy.retention_seconds)
        if selected
        else None,
        created_monotonic=time.monotonic() if work_started_at is None else work_started_at,
    )


@dataclass(frozen=True)
class BatchResult:
    acknowledged: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()


class EventStore(Protocol):
    async def write_batch(self, events: Sequence[CollectionEvent]) -> BatchResult: ...


@dataclass(frozen=True)
class CollectionLimits:
    max_events: int
    max_bytes: int
    max_age_seconds: float
    batch_size: int
    write_timeout_seconds: float
    max_attempts: int
    retry_delay_seconds: float

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_events, self.max_bytes, self.batch_size, self.max_attempts)
        ):
            raise ValueError("invalid_collection_count_limit")
        if any(
            not _finite(value) or value <= 0
            for value in (self.max_age_seconds, self.write_timeout_seconds)
        ):
            raise ValueError("invalid_collection_time_limit")
        if not _finite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("invalid_retry_delay")


class BoundedCollector:
    def __init__(
        self,
        store: EventStore,
        limits: CollectionLimits,
        *,
        on_stored: Callable[[str], None] | None = None,
        on_outcome: Callable[[CollectionEvent, str], None] | None = None,
    ):
        self.store, self.limits, self.on_stored = store, limits, on_stored
        self.on_outcome = on_outcome
        self.counters: Counter[str] = Counter()
        self._queue: deque[tuple[CollectionEvent, int]] = deque()
        self._pending: dict[str, tuple[CollectionEvent, int]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._bytes = 0
        self._closed = False
        self.completeness_known = True

    def submit(self, event: CollectionEvent) -> bool:
        if event._delivery.submitted or event.event_id in self._pending:
            return False
        if self._closed:
            self.counters["dropped_shutdown"] += 1
            return False
        try:
            payload = json.dumps(
                {"summary": event.summary, "detail": event.detail}, allow_nan=False
            )
            size = len(payload.encode())
        except (TypeError, ValueError, OverflowError):
            self.counters["dropped_invalid"] += 1
            return False
        if (
            len(self._pending) >= self.limits.max_events
            or self._bytes + size > self.limits.max_bytes
        ):
            self.counters["dropped_capacity"] += 1
            return False
        if time.monotonic() - event.created_monotonic >= self.limits.max_age_seconds:
            self.counters["dropped_expired"] += 1
            return False
        event._delivery.submitted = True
        safe = json.loads(payload)
        event = copy.copy(event)
        event.summary, event.detail = safe["summary"], safe["detail"]
        self._queue.append((event, size))
        self._pending[event.event_id] = (event, size)
        self._bytes += size
        self.counters["submitted"] += 1
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return True

    def metrics(self) -> dict[str, float | int | bool]:
        """Completeness excludes unresolved storage ACKs and shutdown gaps, not known drops."""
        now = time.monotonic()
        return {
            **self.counters,
            "queue_depth": len(self._pending),
            "queue_bytes": self._bytes,
            "oldest_age_seconds": max(
                (now - event.created_monotonic for event, _ in self._pending.values()), default=0
            ),
            "completeness_known": self.completeness_known,
        }

    def _finish(self, event: CollectionEvent, outcome: str) -> None:
        entry = self._pending.pop(event.event_id, None)
        if entry is None:
            return
        self._bytes -= entry[1]
        self.counters[outcome] += 1
        if outcome == "ack_unknown":
            self.completeness_known = False
        if self.on_outcome is not None:
            try:
                self.on_outcome(event, outcome)
            except Exception:
                self.counters["notification_failed"] += 1
        if outcome == "stored" and self.on_stored is not None:
            try:
                self.on_stored(event.event_id)
            except Exception:
                self.counters["notification_failed"] += 1

    async def _run(self) -> None:
        try:
            while self._queue:
                batch = [
                    self._queue.popleft()[0]
                    for _ in range(min(len(self._queue), self.limits.batch_size))
                ]
                await self._write(batch)
        finally:
            for event, _ in list(self._pending.values()):
                self._finish(event, "dropped_shutdown")
            self._queue.clear()

    async def _write(self, batch: list[CollectionEvent]) -> None:
        uncertain: set[str] = set()
        try:
            for attempt in range(self.limits.max_attempts):
                now = time.monotonic()
                for event in list(batch):
                    if now - event.created_monotonic >= self.limits.max_age_seconds:
                        self._finish(
                            event,
                            "ack_unknown" if event.event_id in uncertain else "dropped_expired",
                        )
                        batch.remove(event)
                if not batch:
                    return
                remaining = min(
                    self.limits.max_age_seconds - (now - event.created_monotonic) for event in batch
                )
                try:
                    result = await asyncio.wait_for(
                        self.store.write_batch(batch),
                        min(remaining, self.limits.write_timeout_seconds),
                    )
                except Exception:
                    self.counters["write_failures"] += 1
                    result = BatchResult(unknown=frozenset(event.event_id for event in batch))
                acknowledged = result.acknowledged - result.failed - result.unknown
                if result.failed:
                    self.counters["write_failures"] += 1
                for event in list(batch):
                    if event.event_id in acknowledged:
                        self._finish(event, "stored")
                        batch.remove(event)
                    elif event.event_id not in result.failed or event.event_id in result.unknown:
                        uncertain.add(event.event_id)
                        self.counters["ack_unknown_attempts"] += 1
                if batch and attempt + 1 < self.limits.max_attempts:
                    self.counters["retries"] += len(batch)
                    remaining = min(
                        self.limits.max_age_seconds - (time.monotonic() - event.created_monotonic)
                        for event in batch
                    )
                    await asyncio.sleep(max(0, min(self.limits.retry_delay_seconds, remaining)))
            for event in batch:
                self._finish(
                    event,
                    "ack_unknown" if event.event_id in uncertain else "dropped_retry_exhausted",
                )
        except asyncio.CancelledError:
            for event in batch:
                self._finish(event, "ack_unknown")
            self.completeness_known = False
            raise

    async def flush(self) -> None:
        if self._worker is not None:
            await asyncio.shield(self._worker)

    async def close(self, timeout: float) -> bool:
        if not _finite(timeout) or timeout <= 0:
            raise ValueError("invalid_close_timeout")
        self._closed = True
        try:
            await asyncio.wait_for(self.flush(), timeout)
            return True
        except TimeoutError:
            self.completeness_known = False
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            return False


@dataclass(frozen=True)
class QueryAccess:
    routes: frozenset[str]
    max_rows: int
    max_period_seconds: float
    allow_details: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.max_rows) is not int
            or self.max_rows <= 0
            or not _finite(self.max_period_seconds)
            or self.max_period_seconds <= 0
            or type(self.allow_details) is not bool
        ):
            raise ValueError("invalid_query_access_limits")


@dataclass(frozen=True)
class EventQuery:
    start: float
    end: float
    routes: frozenset[str]
    limit: int
    result: str | None = None
    reason: str | None = None
    configuration_revision: str | None = None
    comparison_policy_revision: str | None = None
    event_id: str | None = None
    backend: str | None = None
    role: str | None = None
    deployment_revision: str | None = None


class SQLiteEventStore:
    """Local synthetic/integration adapter; does not choose the production storage stack."""

    def __init__(self, path: str):
        self._path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comparison-store")
        self._slot = asyncio.Semaphore(1)
        self._connection: sqlite3.Connection | None = None
        self.counters: Counter[str] = Counter()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
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
        await self._slot.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, operation)
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

        await self._run(close_db)
        self._executor.shutdown(wait=True)
