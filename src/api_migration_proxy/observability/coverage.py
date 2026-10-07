"""명시적 요청 코호트의 처리 단계 및 비교 커버리지 집계."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from .metrics import STEPS


@dataclass(frozen=True)
class Ratio:
    """집계 비율과 known·unknown·no_data·ended 상태."""

    value: float | None
    state: str


def ratio(numerator, denominator, *, complete=True, planned_end=False):
    """집계 완전성과 분모 상태를 반영한 비율 계산.

    Args:
        numerator: 분자 건수(int). None이면 미확인.
        denominator: 분모 건수(int). None이면 미확인.
        complete: 집계 완전성 확인 여부(bool).
        planned_end: 비교가 계획대로 종료되었는지 여부(bool).

    Returns:
        비율과 상태를 담은 Ratio.

    Raises:
        ValueError: 완전한 집계의 건수가 음수·비정수이거나 분자가 분모 초과.
    """
    if not complete or numerator is None or denominator is None:
        return Ratio(None, "unknown")
    if (
        any(isinstance(v, bool) or not isinstance(v, int) for v in (numerator, denominator))
        or numerator < 0
        or denominator < 0
        or numerator > denominator
    ):
        raise ValueError("invalid ratio counts")
    if denominator == 0:
        return Ratio(None, "ended" if planned_end else "no_data")
    return Ratio(numerator / denominator, "known")


class CohortWindow:
    """요청 시작 시각으로 지정한 관측 집단. 저장 이벤트로 모집단 재구성 금지."""

    def __init__(self, route_id, epoch, start, end, *, planned_end=False):
        if (
            not route_id
            or not epoch
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start >= end
        ):
            raise ValueError("a cohort requires route, epoch and a finite half-open interval")
        self.route_id, self.epoch, self.start, self.end = route_id, epoch, start, end
        self.planned_end = planned_end
        self.counts = dict.fromkeys("ESDTCMXW", 0)
        self.pending = 0
        self.complete = True
        self.closed = False
        self._lock = threading.RLock()

    def begin(self, metrics, *, route_id, epoch, started_at):
        """라우트·에포크·시작 시각을 확인하고 코호트에 요청 관측 등록."""
        observation = PipelineObservation(metrics, route_id, cohort=self)
        with self._lock:
            if (
                self.closed
                or route_id != self.route_id
                or epoch != self.epoch
                or not self.start <= started_at < self.end
            ):
                raise ValueError("request is outside this cohort")
            self.pending += 1
        return observation

    def mark_unknown(self):
        """관측 누락으로 코호트 집계의 완전성 미확인 처리."""
        with self._lock:
            self.complete = False

    def close(self, now):
        """시간 구간이 끝나고 모든 요청 관측이 마감된 코호트 종료."""
        with self._lock:
            if not math.isfinite(now) or now < self.end or self.pending:
                raise ValueError("cohort still has an open time interval or unsettled requests")
            self.closed = True

    def report(self):
        """단계별 건수·분모별 비율과 잠정·종료·미확인 상태 반환."""
        with self._lock:
            c = dict(self.counts)
            known = self.complete
            ratios = {
                name: ratio(c[n], c[d], complete=known, planned_end=self.planned_end)
                for name, n, d in (
                    ("selection", "S", "E"),
                    ("dispatch", "D", "S"),
                    ("comparison_completion", "C", "S"),
                    ("coverage", "C", "E"),
                    ("difference", "X", "C"),
                    ("storage", "W", "S"),
                )
            }
            return {
                "route": self.route_id,
                "epoch": self.epoch,
                "counts": c,
                "ratios": ratios,
                "state": "unknown" if not known else "closed" if self.closed else "provisional",
                "pending": self.pending,
                "comparison_state": "ended" if self.planned_end else "active",
            }


class PipelineObservation:
    """요청 하나의 단계 전이와 최종 저장 결과 추적."""

    def __init__(self, metrics, route_id, *, cohort=None):
        metrics._key("comparison_pipeline_total", {"route": route_id, "step": "eligible"})
        self.metrics, self.route_id, self.cohort = metrics, route_id, cohort
        self._seen = set()
        self._result = None
        self._finished = False
        self._lock = threading.RLock()

    def _advance(self, symbol, prerequisite=None):
        with self._lock:
            if symbol in self._seen:
                return False
            if symbol == "D" and self._result is not None:
                raise ValueError("invalid pipeline transition")
            if self._finished or (prerequisite and prerequisite not in self._seen):
                raise ValueError("invalid pipeline transition")
            self.metrics.increment(
                "comparison_pipeline_total", route=self.route_id, step=STEPS[symbol]
            )
            self._seen.add(symbol)
            if self.cohort:
                with self.cohort._lock:
                    self.cohort.counts[symbol] += 1
            return True

    def eligible(self):
        return self._advance("E")

    def selected(self):
        return self._advance("S", "E")

    def dispatched(self):
        return self._advance("D", "S")

    def terminal(self):
        return self._advance("T", "D")

    def compared(self, result, comparison_class="unavailable", reason="none"):
        """실행 종료 상태를 검증하고 비교 판정을 한 번만 확정."""
        with self._lock:
            if self._result is not None:
                if self._result != (result, comparison_class, reason):
                    raise ValueError("comparison result already finalized")
                return False
            if self._finished or "S" not in self._seen:
                raise ValueError("comparison requires a selected open observation")
            if result == "not_executed":
                if "D" in self._seen:
                    raise ValueError("dispatched requests cannot be not_executed")
            elif "T" not in self._seen:
                raise ValueError("both backends must finish before comparison")
            comparable = result in {"matched", "different"}
            if comparable == (comparison_class == "unavailable") or (
                result == "matched" and comparison_class == "mixed"
            ):
                raise ValueError("result and comparison class disagree")
            self.metrics.increment(
                "comparison_results_total",
                route=self.route_id,
                result=result,
                comparison_class=comparison_class,
                reason=reason,
            )
            self._result = (result, comparison_class, reason)
            if comparable:
                if self.cohort:
                    with self.cohort._lock:
                        self._advance("C", "T")
                        self.cohort.counts["M" if result == "matched" else "X"] += 1
                else:
                    self._advance("C", "T")
            return True

    def stored(self):
        """비교 결과 확정 후 저장 ACK를 한 번만 집계."""
        with self._lock:
            if self._result is None:
                raise ValueError("storage confirmation requires a finalized event")
            return self._advance("W", "S")

    def finish(self, *, dropped=False, acknowledgement_unknown=False):
        """요청 관측을 마감하고 코호트의 미완료 건수 해제.

        저장 확인·확정 폐기·미선택 시 마감 가능. 결과 불명 시 코호트 완전성 미확인 처리.

        Args:
            dropped: 폐기가 확정되었는지 여부(bool).
            acknowledgement_unknown: 저장 ACK 등 종료 결과가 불명인지 여부(bool).

        Raises:
            ValueError: 종료·저장 확인 없이 선택된 관측을 마감하려는 경우.
        """
        with self._lock:
            if self._finished:
                return
            if "D" in self._seen and "T" not in self._seen and not acknowledgement_unknown:
                raise ValueError("executions have not reached terminal state")
            if (
                "S" in self._seen
                and "W" not in self._seen
                and not dropped
                and not acknowledgement_unknown
            ):
                raise ValueError("selected observations require ACK or an explicit loss state")
            self._finished = True
            if self.cohort:
                with self.cohort._lock:
                    self.cohort.pending -= 1
                    if acknowledgement_unknown:
                        self.cohort.complete = False
