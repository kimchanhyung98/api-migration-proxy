"""응답 캡처의 비교·상세 마스킹·수집 전달 작업 관리."""

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
from traceback import clear_frames
from typing import Any

from api_migration_proxy.collection.collector import BoundedCollector
from api_migration_proxy.collection.events import DetailPolicy, make_event
from api_migration_proxy.comparison.engine import (
    BackendResponse,
    ComparisonContext,
    ComparisonPolicy,
    compare,
)
from api_migration_proxy.observability.metrics import Metrics
from api_migration_proxy.routing.configuration import ConfigurationError


def _utc() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkLimits:
    """비교 작업 수·메모리·시간 한도와 관측 식별자."""

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
    """HTTP 요청 수명과 독립적으로 비교·마스킹 작업의 자원 한도 관리."""

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
        """비교용 스레드 풀과 비동기 큐 소비 작업 시작."""
        self._pool = ThreadPoolExecutor(
            max_workers=self.limits.compare_workers, thread_name_prefix="comparison"
        )
        self._workers = [
            asyncio.create_task(self._compare_worker()) for _ in range(self.limits.compare_workers)
        ]
        self._open = True

    def status(self) -> dict[str, int]:
        """대기·실행 중인 비교 작업 수와 예약 바이트 반환."""
        return {"comparison_jobs": self._jobs, "comparison_bytes": self._job_bytes}

    def _drop(self, reason: str) -> None:
        self.metrics.increment("collection_dropped_total", reason=reason)

    def submit(self, summary, v1, v2, policy, context):
        """작업 수·바이트 한도 내에서 비교 작업 접수.

        종료 상태 또는 한도 초과 시 폐기 사유 집계 후 반환.

        Args:
            summary: 요청과 두 백엔드 실행 결과를 담은 dict.
            v1: v1의 BackendResponse.
            v2: v2의 BackendResponse.
            policy: 적용할 ComparisonPolicy.
            context: 비교 가능 근거를 담은 ComparisonContext.
        """
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
            result = event = None
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
                done, _pending = await asyncio.wait(
                    {future},
                    timeout=min(
                        self.limits.compare_timeout_seconds,
                        self.limits.compare_max_age_seconds - age,
                    ),
                )
                if not done:
                    self._drop("timeout")
                    settled = True
                    # 시간 초과 뒤에도 실제 계산 종료까지 입력과 작업 슬롯 유지.
                    await asyncio.wait({future})
                    future.result()
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
                    done, _pending = await asyncio.wait(
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
                            pass  # 검증된 요약의 masking_failed 상태 유지.
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
                    # 요약 접수 후에도 원본 상세 작업이 끝날 때까지 슬롯 유지.
                    try:
                        await asyncio.wait({future})
                        future.result()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                if not settled:
                    self._drop("shutdown")
                if future is not None:
                    while not future.done():
                        try:
                            await asyncio.wait({future})
                        except asyncio.CancelledError:
                            pass
                    if not future.cancelled():
                        failure = future.exception()
                        if failure is not None:
                            clear_frames(failure.__traceback__)
                            failure.__traceback__ = None
                return
            except Exception:
                if not settled:
                    self._drop("comparison_dropped")
            finally:
                self._jobs -= 1
                self._job_bytes -= job.size
                self._queue.task_done()
                # 유휴 작업자에 완료된 캡처와 예외 traceback 참조가 남지 않도록 해제.
                del job
                future = None
                done = _pending = set()
                result = event = None

    async def flush(self) -> None:
        """접수한 비교·마스킹 작업이 큐에서 모두 정리될 때까지 대기."""
        await self._queue.join()

    async def close(self, deadline: float) -> None:
        """접수를 중지하고 기한 내 큐와 작업자 정리.

        기한 후에도 실행 중인 CPU 작업은 완료 시점까지 자원 점유 추적.

        Args:
            deadline: 종료 대기 기한의 단조 시각.
        """
        self._open = False
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await self._queue.join()
        except TimeoutError:
            pass  # 아래 종료 처리에서 미완료 작업별 최종 결과 집계.
        for worker in self._workers:
            worker.cancel()
        # 취소로 CPU 작업이 중단되지는 않으므로 미완료 작업자 추적 유지.
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
