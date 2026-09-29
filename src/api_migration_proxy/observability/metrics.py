"""Bounded process metrics and worker snapshot aggregation."""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import AbstractSet, Any, Mapping

BACKENDS = frozenset({"v1", "v2"})
CONTRACT_CLASSES = frozenset({"success", "expected_rejection", "unexpected_error", "unknown"})
RESULTS = frozenset({"matched", "different", "not_executed", "execution_error", "not_comparable"})
COMPARISON_CLASSES = frozenset({"success", "expected_rejection", "mixed", "unavailable"})
STEPS = {
    "E": "eligible",
    "S": "selected",
    "D": "dispatched",
    "T": "terminal",
    "C": "comparable",
    "W": "stored",
}
DEFAULT_REASONS = frozenset(
    {
        "none",
        "matched",
        "json_value_mismatch",
        "status_mismatch",
        "header_mismatch",
        "contract_mismatch",
        "queue_full",
        "queue_expired",
        "retry_exhausted",
        "shutdown",
        "timeout",
        "cancelled",
        "transport_error",
        "slot_exhausted",
        "oversized",
        "capture_unavailable",
        "context_missing",
        "context_mismatch",
        "parse_error",
        "policy_limit",
        "unexpected_error",
        "unknown_contract",
        "request_oversized",
        "response_oversized",
        "storage_failure",
        "masking_failed",
        "comparison_dropped",
    }
)
_SCHEMAS = {
    "proxy_assignments_total": ("counter", {"route", "serving"}),
    "proxy_requests_total": ("counter", {"route", "serving", "outcome", "source"}),
    "proxy_request_duration_seconds": ("histogram", {"route", "serving"}),
    "backend_started_total": ("counter", {"route", "backend", "role"}),
    "backend_completed_total": (
        "counter",
        {"route", "backend", "role", "outcome", "contract_class"},
    ),
    "backend_duration_seconds": ("histogram", {"route", "backend", "role"}),
    "backend_inflight": ("gauge", {"backend", "role"}),
    "shadow_decisions_total": ("counter", {"route", "decision"}),
    "shadow_not_dispatched_total": ("counter", {"route", "reason"}),
    "comparison_pipeline_total": ("counter", {"route", "step"}),
    "comparison_results_total": ("counter", {"route", "result", "comparison_class", "reason"}),
    "comparison_duration_seconds": ("histogram", {"route", "phase"}),
    "collection_queue_depth": ("gauge", set()),
    "collection_oldest_age_seconds": ("gauge", set()),
    "collection_dropped_total": ("counter", {"reason"}),
    "collection_write_failures_total": ("counter", set()),
    "collection_expired_pending": ("gauge", set()),
}
MetricKey = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class Histogram:
    bounds: tuple[float, ...]
    buckets: tuple[int, ...]
    count: int
    total: float


@dataclass(frozen=True)
class MetricSnapshot:
    counters: Mapping[MetricKey, int]
    gauges: Mapping[MetricKey, float]
    histograms: Mapping[MetricKey, Histogram]


class Metrics:
    def __init__(
        self,
        route_ids,
        *,
        reason_codes=DEFAULT_REASONS,
        histogram_bounds=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
    ):
        routes = frozenset(route_ids)
        reasons = frozenset(reason_codes)
        if not routes or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", v)
            for v in routes
        ):
            raise ValueError("routes must use registered stable identifiers")
        if not reasons or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", v) for v in reasons
        ):
            raise ValueError("reasons must use a bounded registered identifier set")
        bounds = tuple(histogram_bounds)
        if (
            not bounds
            or any(not math.isfinite(v) or v <= 0 for v in bounds)
            or tuple(sorted(set(bounds))) != bounds
        ):
            raise ValueError("histogram bounds must be positive and strictly increasing")
        self._labels = {
            "route": routes,
            "serving": BACKENDS | {"unknown"},
            "backend": BACKENDS,
            "role": {"serving", "shadow"},
            "contract_class": CONTRACT_CLASSES,
            "result": RESULTS,
            "comparison_class": COMPARISON_CLASSES,
            "reason": reasons,
            "step": set(STEPS.values()),
            "decision": {"selected", "sampled_out", "ineligible"},
            "source": {"backend", "proxy", "none"},
            "phase": {"queue", "compute"},
        }
        self._bounds = bounds
        self._counters: Counter[MetricKey] = Counter()
        self._gauges = {}
        self._histograms = {}
        self._lock = threading.RLock()

    def _key(self, name, labels, kind=None):
        if name not in _SCHEMAS:
            raise ValueError("unregistered metric")
        declared_kind, required = _SCHEMAS[name]
        if (kind and declared_kind != kind) or labels.keys() != required:
            raise ValueError("metric kind or labels do not match the registered schema")
        for label, value in labels.items():
            allowed: AbstractSet[str]
            if label == "outcome":
                allowed = (
                    {"http_response", "transport_error", "timeout", "cancelled"}
                    if name == "backend_completed_total"
                    else {
                        "completed",
                        "transport_error",
                        "timeout",
                        "cancelled",
                        "rejected",
                        "incomplete",
                    }
                )
            else:
                allowed = self._labels[label]
            if value not in allowed:
                raise ValueError("unregistered metric label value")
        if name == "proxy_assignments_total" and labels["serving"] == "unknown":
            raise ValueError("an assignment must identify its backend")
        return name, tuple(sorted(labels.items()))

    def increment(self, name, value=1, **labels):
        key = self._key(name, labels, "counter")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("counter increments must be nonnegative integers")
        with self._lock:
            self._counters[key] += value

    def observe(self, name, seconds, **labels):
        key = self._key(name, labels, "histogram")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("durations must be finite nonnegative seconds")
        with self._lock:
            previous = self._histograms.get(
                key, Histogram(self._bounds, (0,) * len(self._bounds), 0, 0.0)
            )
            self._histograms[key] = Histogram(
                self._bounds,
                tuple(
                    n + int(seconds <= bound) for n, bound in zip(previous.buckets, self._bounds)
                ),
                previous.count + 1,
                previous.total + seconds,
            )

    def set_gauge(self, name, value, **labels):
        key = self._key(name, labels, "gauge")
        if not math.isfinite(value) or value < 0:
            raise ValueError("gauge must be finite and nonnegative")
        with self._lock:
            self._gauges[key] = value

    def adjust_gauge(self, name, delta, **labels):
        key = self._key(name, labels, "gauge")
        with self._lock:
            value = self._gauges.get(key, 0) + delta
            if not math.isfinite(value) or value < 0:
                raise ValueError("gauge must remain finite and nonnegative")
            self._gauges[key] = value

    def value(self, name, **labels):
        key = self._key(name, labels)
        with self._lock:
            kind = _SCHEMAS[name][0]
            if kind == "histogram":
                return self._histograms.get(key)
            return self._counters.get(key, 0) if kind == "counter" else self._gauges.get(key)

    def snapshot(self):
        with self._lock:
            return MetricSnapshot(
                MappingProxyType(dict(self._counters)),
                MappingProxyType(dict(self._gauges)),
                MappingProxyType(dict(self._histograms)),
            )

    def export(self):
        snapshot = self.snapshot()
        result: list[dict[str, Any]] = []
        for kind, values in (("counter", snapshot.counters), ("gauge", snapshot.gauges)):
            result.extend(
                {"name": name, "labels": dict(labels), "kind": kind, "value": value}
                for (name, labels), value in sorted(values.items())
            )
        result.extend(
            {
                "name": name,
                "labels": dict(labels),
                "kind": "histogram",
                "bounds": list(item.bounds),
                "buckets": list(item.buckets),
                "count": item.count,
                "sum": item.total,
            }
            for (name, labels), item in sorted(snapshot.histograms.items())
        )
        return result


@dataclass(frozen=True)
class WorkerMetrics:
    worker_id: str
    generation: str
    snapshot: MetricSnapshot
    alive: bool = True
    complete: bool = True


def merge_workers(workers, *, expected_workers, previous_generations=None):
    workers = tuple(workers)
    ids = [worker.worker_id for worker in workers]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate worker snapshots must not be counted twice")
    if not expected_workers or any(not w.worker_id or not w.generation for w in workers):
        raise ValueError("worker topology and process generations are required")
    counters: Counter[MetricKey] = Counter()
    gauges: dict[MetricKey, float] = {}
    histograms: dict[MetricKey, Histogram] = {}
    complete = set(ids) == set(expected_workers)
    for worker in workers:
        complete = complete and worker.complete and worker.alive
        if (
            previous_generations
            and previous_generations.get(worker.worker_id, worker.generation) != worker.generation
        ):
            complete = False
        counters.update(worker.snapshot.counters)
        if worker.alive:
            for key, value in worker.snapshot.gauges.items():
                gauges[key] = (
                    max(gauges.get(key, 0), value)
                    if key[0] == "collection_oldest_age_seconds"
                    else gauges.get(key, 0) + value
                )
        for key, item in worker.snapshot.histograms.items():
            previous = histograms.get(key)
            if previous is None:
                histograms[key] = item
            else:
                if previous.bounds != item.bounds:
                    raise ValueError("incompatible histogram buckets")
                histograms[key] = Histogram(
                    item.bounds,
                    tuple(a + b for a, b in zip(previous.buckets, item.buckets)),
                    previous.count + item.count,
                    previous.total + item.total,
                )
    return MetricSnapshot(
        MappingProxyType(dict(counters)), MappingProxyType(gauges), MappingProxyType(histograms)
    ), complete
