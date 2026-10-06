"""Explicit request-cohort coverage and pipeline accounting."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from .metrics import STEPS


@dataclass(frozen=True)
class Ratio:
    value: float | None
    state: str


def ratio(numerator, denominator, *, complete=True, planned_end=False):
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
    """An explicitly selected start-time population, never reconstructed from storage."""

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
        with self._lock:
            self.complete = False

    def close(self, now):
        with self._lock:
            if not math.isfinite(now) or now < self.end or self.pending:
                raise ValueError("cohort still has an open time interval or unsettled requests")
            self.closed = True

    def report(self):
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
        with self._lock:
            if self._result is None:
                raise ValueError("storage confirmation requires a finalized event")
            return self._advance("W", "S")

    def finish(self, *, dropped=False, acknowledgement_unknown=False):
        """Release accounting only after final ACK, confirmed drop, or no selection."""
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
