from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .events import CollectionEvent, _finite


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
