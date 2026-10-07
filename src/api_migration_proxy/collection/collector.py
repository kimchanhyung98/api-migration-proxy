"""용량 제한 큐와 재시도를 통한 비교 이벤트 저장."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from traceback import clear_frames
from typing import Protocol

from .events import CollectionEvent, _finite


@dataclass(frozen=True)
class BatchResult:
    """이벤트 ID별 저장 확인·실패·확인 불가 결과."""

    acknowledged: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()


class EventStore(Protocol):
    """이벤트 묶음 저장과 ID별 결과 반환 계약."""

    async def write_batch(self, events: Sequence[CollectionEvent]) -> BatchResult: ...


@dataclass(frozen=True)
class CollectionLimits:
    """수집 큐 용량, 대기 시간 및 저장 재시도 한도."""

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
    """접수 이벤트의 저장 결과와 메모리 점유를 추적하는 수집기."""

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
        self._writes: dict[str, asyncio.Task[BatchResult]] = {}
        self._finished: set[str] = set()
        self._worker: asyncio.Task[None] | None = None
        self._bytes = 0
        self._closed = False
        self.completeness_known = True

    def submit(self, event: CollectionEvent) -> bool:
        """수집 한도 내에서 이벤트 복사본을 큐에 접수.

        Args:
            event: 허용 필드 검증과 마스킹을 마친 저장 이벤트.

        Returns:
            접수 성공 여부. True는 저장 완료를 의미하지 않음.
        """
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
        """수집 결과와 처리 중 이벤트의 점유 현황 반환.

        저장 ACK 불명 또는 종료 중 누락은 완전성 미확인 처리. 확정된 폐기는 집계에 포함.

        Returns:
            건수·바이트·최장 대기 시간 및 저장 결과 집계의 완전성.
        """
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
        entry = self._pending.get(event.event_id)
        if entry is None or event.event_id in self._finished:
            return
        if event.event_id in self._writes:
            self._finished.add(event.event_id)
        else:
            self._pending.pop(event.event_id)
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

    def _write_done(self, event_ids: tuple[str, ...], task: asyncio.Task[BatchResult]) -> None:
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                # 완료된 페이로드가 예외 traceback 순환 참조에 남지 않도록 해제.
                clear_frames(failure.__traceback__)
                failure.__traceback__ = None
        for event_id in event_ids:
            self._writes.pop(event_id)
            if event_id in self._finished:
                self._finished.remove(event_id)
                self._bytes -= self._pending.pop(event_id)[1]

    async def _run(self) -> None:
        try:
            while self._queue:
                batch = [
                    self._queue.popleft()[0]
                    for _ in range(min(len(self._queue), self.limits.batch_size))
                ]
                await self._write(batch)
        except asyncio.CancelledError:
            pass  # 작업자가 취소된 대기 프레임과 저장 작업을 계속 참조하지 않도록 해제.
        finally:
            for event, _ in list(self._pending.values()):
                self._finish(event, "dropped_shutdown")
                del event
            self._queue.clear()

    async def _write(self, batch: list[CollectionEvent]) -> None:
        uncertain: set[str] = set()
        attempt_pending = False
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
                    del event
                if not batch:
                    return
                remaining = min(
                    self.limits.max_age_seconds - (now - event.created_monotonic) for event in batch
                )
                attempt_pending = True
                write = asyncio.create_task(self.store.write_batch(tuple(batch)))
                event_ids = tuple(event.event_id for event in batch)
                self._writes.update((event_id, write) for event_id in event_ids)
                write.add_done_callback(partial(self._write_done, event_ids))
                try:
                    done, _pending = await asyncio.wait(
                        {write}, timeout=min(remaining, self.limits.write_timeout_seconds)
                    )
                    if not done:
                        self.counters["write_failures"] += 1
                        self.counters["ack_unknown_attempts"] += len(batch)
                        for event in batch:
                            self._finish(event, "ack_unknown")
                        return
                    result = write.result()
                except Exception:
                    self.counters["write_failures"] += 1
                    result = BatchResult(unknown=frozenset(event.event_id for event in batch))
                finally:
                    del write
                    done = _pending = set()
                acknowledged = result.acknowledged - result.failed - result.unknown
                if result.failed:
                    self.counters["write_failures"] += 1
                for event in list(batch):
                    if event.event_id in acknowledged:
                        self._finish(event, "stored")
                        batch.remove(event)
                    elif (
                        event.event_id not in result.failed
                        or event.event_id in result.unknown
                        or event.event_id in result.acknowledged
                    ):
                        uncertain.add(event.event_id)
                        self.counters["ack_unknown_attempts"] += 1
                    del event
                attempt_pending = False
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
                self._finish(
                    event,
                    "ack_unknown"
                    if attempt_pending or event.event_id in uncertain
                    else "dropped_shutdown",
                )
                del event
            batch.clear()
            self.completeness_known = False
            raise

    async def flush(self) -> None:
        """진행 중인 수집 작업과 저장 요청이 정리될 때까지 대기."""
        if self._worker is not None:
            await asyncio.shield(self._worker)
        if self._writes:
            await asyncio.wait(set(self._writes.values()))

    async def close(self, timeout: float) -> bool:
        """접수를 중지하고 제한 시간 내 수집 작업 종료.

        Args:
            timeout: 종료 대기 시간(초).

        Returns:
            제한 시간 내 수집 작업 종료 여부. 모든 이벤트의 저장 성공을 의미하지 않음.

        Raises:
            ValueError: 종료 대기 시간이 양의 유한 수가 아닌 경우.
        """
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
