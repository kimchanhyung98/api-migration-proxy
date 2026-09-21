from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .collection import BoundedCollector, DetailPolicy, make_event
from .comparison import BackendResponse, ComparisonContext, ComparisonPolicy, compare
from .config import ConfigurationError
from .observability import Metrics


def _utc() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkLimits:
    compare_max_jobs: int
    compare_max_bytes: int
    compare_max_age_seconds: float
    compare_workers: int
    compare_timeout_seconds: float
    event_retention_seconds: float
    environment: str
    migration_id: str
    epoch_id: str

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name in {"environment", "migration_id", "epoch_id"}:
                if not isinstance(value, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value
                ):
                    raise ValueError("invalid observation identifier")
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("work budgets must be explicitly positive and finite")
        for name in ("compare_max_jobs", "compare_max_bytes", "compare_workers"):
            if type(getattr(self, name)) is not int:
                raise ValueError("work counts must be integers")


@dataclass
class _ComparisonJob:
    summary: dict
    v1: BackendResponse
    v2: BackendResponse
    policy: ComparisonPolicy
    context: ComparisonContext
    submitted: float
    size: int


class ComparisonPipeline:
    """Own bounded comparison and masking work independently of HTTP request lifetimes."""

    def __init__(
        self,
        limits: WorkLimits,
        collector: BoundedCollector,
        metrics: Metrics,
        *,
        detail_policy: DetailPolicy | None = None,
        detail_provider: Callable | None = None,
    ):
        if detail_policy and detail_policy.ratio and detail_provider is None:
            raise ConfigurationError("enabled detail collection needs an approved field provider")
        self.limits, self.collector, self.metrics = limits, collector, metrics
        self.detail_policy, self.detail_provider = detail_policy, detail_provider
        self._queue: asyncio.Queue[_ComparisonJob] = asyncio.Queue(limits.compare_max_jobs)
        self._workers: list[asyncio.Task] = []
        self._pool: ThreadPoolExecutor | None = None
        self._jobs = self._job_bytes = 0
        self._open = False

    def start(self) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=self.limits.compare_workers, thread_name_prefix="comparison"
        )
        self._workers = [
            asyncio.create_task(self._compare_worker()) for _ in range(self.limits.compare_workers)
        ]
        self._open = True

    def status(self) -> dict[str, int]:
        return {"comparison_jobs": self._jobs, "comparison_bytes": self._job_bytes}

    def _drop(self, reason: str) -> None:
        self.metrics.increment("collection_dropped_total", reason=reason)

    def submit(self, summary, v1, v2, policy, context):
        if not self._open:
            self._drop("shutdown")
            return
        size = len(v1.body or b"") + len(v2.body or b"") + 4096
        size += len(json.dumps(summary).encode())
        size += sum(
            len(k.encode()) + len(v.encode()) + 128
            for response in (v1, v2)
            for k, v in response.headers
        )
        if (
            self._jobs >= self.limits.compare_max_jobs
            or self._job_bytes + size > self.limits.compare_max_bytes
        ):
            self._drop("queue_full")
            return
        self._jobs += 1
        self._job_bytes += size
        self._queue.put_nowait(
            _ComparisonJob(summary, v1, v2, policy, context, time.monotonic(), size)
        )

    def _summary_event(self, job, result, selected):
        summary = dict(job.summary)
        summary["comparison_completed_at"] = _utc()
        summary["comparison"] = {
            "result": result.result,
            "reason": result.reason,
            "comparison_class": result.comparison_class,
            "difference_count": result.difference_count,
            "differences_truncated": result.difference_count_limited,
            "paths_truncated": result.difference_paths_truncated,
            "difference_paths": list(result.difference_paths),
            "applied_rules": list(result.applied_rules),
        }
        return make_event(
            summary,
            retention_seconds=self.limits.event_retention_seconds,
            allowed_difference_paths=job.policy.allowed_diff_paths,
            work_started_at=job.submitted,
            detail_policy=replace(self.detail_policy, masker=None) if self.detail_policy else None,
            sample=lambda: 0.0 if selected else 1.0,
        )

    def _detail_event(self, job, result, base):
        assert self.detail_provider is not None
        details = self.detail_provider(job.v1, job.v2, result)
        event = make_event(
            base.summary,
            retention_seconds=self.limits.event_retention_seconds,
            allowed_difference_paths=job.policy.allowed_diff_paths,
            work_started_at=job.submitted,
            now=base.created_at,
            detail_policy=self.detail_policy,
            details=details,
            sample=lambda: 0.0,
        )
        return event.summary, event.detail, event.detail_expires_at

    async def _compare_worker(self):
        while True:
            job = await self._queue.get()
            route_id = job.summary["route_id"]
            future: asyncio.Future[Any] | None = None
            settled = False
            try:
                age = time.monotonic() - job.submitted
                self.metrics.observe(
                    "comparison_duration_seconds", age, route=route_id, phase="queue"
                )
                if age > self.limits.compare_max_age_seconds:
                    self._drop("queue_expired")
                    continue
                started = time.monotonic()
                future = asyncio.get_running_loop().run_in_executor(
                    self._pool, compare, job.v1, job.v2, job.policy, job.context
                )
                done, _ = await asyncio.wait(
                    {future},
                    timeout=min(
                        self.limits.compare_timeout_seconds,
                        self.limits.compare_max_age_seconds - age,
                    ),
                )
                if not done:
                    self._drop("timeout")
                    settled = True
                    # The thread still owns its input and slot until computation actually ends.
                    await asyncio.shield(future)
                    continue
                result = future.result()
                self.metrics.observe(
                    "comparison_duration_seconds",
                    time.monotonic() - started,
                    route=route_id,
                    phase="compute",
                )
                self.metrics.increment(
                    "comparison_results_total",
                    route=route_id,
                    result=result.result,
                    comparison_class=result.comparison_class,
                    reason=result.reason,
                )
                if result.result in {"matched", "different"}:
                    self.metrics.increment(
                        "comparison_pipeline_total", route=route_id, step="comparable"
                    )
                selected = bool(
                    self.detail_policy
                    and secrets.randbelow(2**53) / 2**53 < self.detail_policy.ratio
                )
                event = self._summary_event(job, result, selected)
                detail_pending = False
                if selected:
                    future = asyncio.get_running_loop().run_in_executor(
                        self._pool, self._detail_event, job, result, event
                    )
                    done, _ = await asyncio.wait(
                        {future},
                        timeout=max(
                            0,
                            min(
                                self.limits.compare_timeout_seconds - (time.monotonic() - started),
                                self.limits.compare_max_age_seconds
                                - (time.monotonic() - job.submitted),
                            ),
                        ),
                    )
                    if done:
                        try:
                            event.summary, event.detail, event.detail_expires_at = future.result()
                        except Exception:
                            pass  # The already-safe summary retains masking_failed.
                    else:
                        detail_pending = True
                before = self.collector.counters.copy()
                if not self.collector.submit(event):
                    rejection_reasons = {
                        "dropped_expired": "queue_expired",
                        "dropped_invalid": "masking_failed",
                        "dropped_shutdown": "shutdown",
                        "dropped_capacity": "queue_full",
                    }
                    reason = next(
                        (
                            mapped
                            for counter, mapped in rejection_reasons.items()
                            if self.collector.counters[counter] > before[counter]
                        ),
                        "queue_full",
                    )
                    self._drop(reason)
                settled = True
                if detail_pending:
                    # The safe summary is submitted, but this slot still owns the raw detail task.
                    try:
                        await asyncio.shield(future)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                if not settled:
                    self._drop("shutdown")
                if future is not None and not future.done():
                    try:
                        await asyncio.shield(future)
                    except Exception:
                        pass
                raise
            except Exception:
                if not settled:
                    self._drop("comparison_dropped")
            finally:
                self._jobs -= 1
                self._job_bytes -= job.size
                self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()

    async def close(self, deadline: float) -> None:
        self._open = False
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await self._queue.join()
        except TimeoutError:
            pass  # Each unfinished job accounts for its own terminal outcome below.
        for worker in self._workers:
            worker.cancel()
        # Cancellation does not release a running CPU job. Unfinished workers stay tracked.
        if self._workers:
            await asyncio.wait(self._workers, timeout=max(0, deadline - time.monotonic()))
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._jobs -= 1
            self._job_bytes -= job.size
            self._queue.task_done()
            self._drop("shutdown")
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
