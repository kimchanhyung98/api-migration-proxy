"""Evidence records for manual rollout, propagation, rollback and retirement."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class Stage(StrEnum):
    BASELINE = "S0"
    V1_ONLY = "S1"
    SHADOW = "S2"
    CANARY = "S3"
    V2_PRIMARY = "S4"
    SHADOW_OFF = "S5"
    RETIRED = "S6"


GATES = tuple(f"G-{i:02d}" for i in range(1, 9))
STATUSES = frozenset({"pass", "fail", "unknown"})


def _references(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("evidence must be a sequence of references")
    result = tuple(values)
    if any(not isinstance(v, str) or not v.strip() for v in result):
        raise ValueError("evidence references must be nonempty")
    return result


@dataclass(frozen=True)
class GateEvidence:
    gate: str
    status: str
    epoch: str
    evidence: tuple[str, ...] = ()
    applicable: bool = True
    exclusion_reason: str | None = None
    alternative_evidence: tuple[str, ...] = ()

    def __post_init__(self):
        if self.gate not in GATES or self.status not in STATUSES or not self.epoch:
            raise ValueError("invalid gate evidence")
        object.__setattr__(self, "evidence", _references(self.evidence))
        object.__setattr__(self, "alternative_evidence", _references(self.alternative_evidence))
        if self.status == "pass" and not self.evidence:
            raise ValueError("a passing gate requires observed evidence")
        if not self.applicable and (
            self.status != "unknown" or not self.exclusion_reason or not self.alternative_evidence
        ):
            raise ValueError(
                "an excluded gate is not a pass and requires reason and alternative evidence"
            )


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    blockers: tuple[str, ...]
    excluded: tuple[str, ...]


def evaluate_promotion(
    *,
    stage,
    epoch,
    evidence,
    comparison_active,
    prior_comparison_evidence=(),
    current_serving_evidence=(),
):
    """Validate supplied manual decisions; do not infer thresholds or apply changes."""
    stage = Stage(stage)
    evidence = tuple(evidence)
    prior_comparison_evidence = _references(prior_comparison_evidence)
    current_serving_evidence = _references(current_serving_evidence)
    if not epoch:
        raise ValueError("an observation epoch is required")
    if len({e.gate for e in evidence}) != len(evidence):
        raise ValueError("a gate must have one current assessment")
    by_gate = {e.gate: e for e in evidence}
    blockers, excluded = [], []
    planned_end = stage in {Stage.SHADOW_OFF, Stage.RETIRED} or (
        stage == Stage.V2_PRIMARY and not comparison_active
    )
    if stage in {Stage.SHADOW_OFF, Stage.RETIRED} and comparison_active:
        blockers.append("stage_requires_comparison_end")
    if stage == Stage.SHADOW and not comparison_active:
        blockers.append("shadow_stage_requires_shadow")
    if stage in {Stage.BASELINE, Stage.V1_ONLY} and comparison_active:
        blockers.append("stage_requires_no_shadow")
    for gate in GATES:
        item = by_gate.get(gate)
        if item is None or item.epoch != epoch:
            blockers.append(f"{gate}:unknown")
            continue
        if not item.applicable:
            excluded.append(gate)
            if gate == "G-03" and comparison_active:
                blockers.append("G-03:active_comparison_cannot_be_excluded")
            continue
        if gate == "G-03" and (not comparison_active or stage in {Stage.BASELINE, Stage.V1_ONLY}):
            blockers.append("G-03:comparison_not_running_requires_exclusion")
        if item.status != "pass":
            blockers.append(f"{gate}:{item.status}")
    if planned_end and (not prior_comparison_evidence or not current_serving_evidence):
        blockers.append("comparison_end_requires_historical_and_current_evidence")
    return PromotionDecision(not blockers, tuple(blockers), tuple(excluded))


class RevisionConflict(ValueError):
    pass


@dataclass(frozen=True)
class PropagationStatus:
    desired_revision: str
    workers: Mapping[str, str]
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    failed: tuple[str, ...]
    stable: bool


class RevisionTracker:
    """Each identity denotes a configuration-reading worker, not a load balancer."""

    def __init__(self, *, workers, revision):
        workers = tuple(workers)
        if (
            not revision
            or not workers
            or len(set(workers)) != len(workers)
            or any(not w for w in workers)
        ):
            raise ValueError("explicit unique worker identities and revision are required")
        self._expected = frozenset(workers)
        self._desired = revision
        self._reports = {}
        self._failures = set()
        self._lock = threading.RLock()

    def request_change(self, *, previous_revision, next_revision):
        with self._lock:
            if previous_revision != self._desired:
                raise RevisionConflict("previous revision is stale")
            if not next_revision or next_revision == previous_revision:
                raise ValueError("a distinct next revision is required")
            self._desired = next_revision
            self._failures.clear()

    def report(self, worker, revision, *, failed=False):
        with self._lock:
            if worker not in self._expected or not revision:
                raise ValueError("unknown worker or empty revision")
            self._reports[worker] = revision
            if failed:
                self._failures.add(worker)
            else:
                self._failures.discard(worker)

    def status(self):
        with self._lock:
            missing = tuple(sorted(self._expected - self._reports.keys()))
            mismatched = tuple(
                sorted(w for w, rev in self._reports.items() if rev != self._desired)
            )
            failed = tuple(sorted(self._failures))
            return PropagationStatus(
                self._desired,
                MappingProxyType(dict(self._reports)),
                missing,
                mismatched,
                failed,
                not (missing or mismatched or failed),
            )


ROLLBACK_CHECKS = frozenset(
    {"data_freshness", "schema", "authorization", "contract", "capacity", "path"}
)


@dataclass(frozen=True)
class RollbackReadiness:
    checks: Mapping[str, str]
    evidence: tuple[str, ...]
    measured_recovery_seconds: float | None
    target_recovery_seconds: float | None

    def __post_init__(self):
        if set(self.checks) != ROLLBACK_CHECKS or any(
            v not in STATUSES for v in self.checks.values()
        ):
            raise ValueError("rollback requires all current readiness checks")
        for value in (self.measured_recovery_seconds, self.target_recovery_seconds):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("recovery times must be finite and nonnegative")
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "evidence", _references(self.evidence))

    @property
    def status(self):
        if "fail" in self.checks.values():
            return "fail"
        if (
            "unknown" in self.checks.values()
            or not self.evidence
            or self.measured_recovery_seconds is None
            or self.target_recovery_seconds is None
        ):
            return "unknown"
        return "pass" if self.measured_recovery_seconds <= self.target_recovery_seconds else "fail"


@dataclass(frozen=True)
class ControlRatios:
    v2_serve_ratio: float
    shadow_sample_ratio: float

    def __post_init__(self):
        if any(
            isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1
            for v in (self.v2_serve_ratio, self.shadow_sample_ratio)
        ):
            raise ValueError("ratios must be finite values in [0, 1]")

    def stop_shadow(self):
        return ControlRatios(self.v2_serve_ratio, 0)

    def rollback_serving(self, readiness):
        if readiness.status != "pass":
            raise ValueError("v1 rollback readiness has not been demonstrated")
        return ControlRatios(0, self.shadow_sample_ratio)


RETIREMENT_CHECKS = frozenset(
    {
        "v2_business_cycle",
        "unregistered_routes",
        "batch_clients",
        "admin_clients",
        "internal_clients",
        "sessions",
        "shadow_off_load",
        "cold_rollback",
        "direct_v2_contract",
        "direct_observability",
        "rollback_window",
        "service_data_recovery",
        "retention_ownership",
        "final_report",
    }
)


@dataclass(frozen=True)
class RetirementEvidence:
    checks: Mapping[str, str]
    references: tuple[str, ...]

    def __post_init__(self):
        if set(self.checks) != RETIREMENT_CHECKS or any(
            v not in STATUSES for v in self.checks.values()
        ):
            raise ValueError(
                "retirement checks must include residual callers, direct path and retention"
            )
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "references", _references(self.references))

    @property
    def status(self):
        if "fail" in self.checks.values():
            return "fail"
        if "unknown" in self.checks.values() or not self.references:
            return "unknown"
        return "pass"


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    route_or_group: str
    owner: str
    reason: str
    previous_revision: str
    next_revision: str
    previous_stage: Stage
    next_stage: Stage
    ratios: ControlRatios
    epoch: str
    requested_at: str
    rollback_revision: str | None
    gate_evidence: tuple[GateEvidence, ...] = ()
    result: str = "requested"
    applied_at: str | None = None
    applied_workers: tuple[str, ...] = ()
    recovery_seconds: float | None = None
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if any(
            not v
            for v in (
                self.change_id,
                self.route_or_group,
                self.owner,
                self.reason,
                self.previous_revision,
                self.next_revision,
                self.epoch,
                self.requested_at,
            )
        ):
            raise ValueError("change identity, actor, reason, revisions and timing are required")
        if self.previous_revision == self.next_revision:
            raise ValueError("change requires distinct revisions")
        object.__setattr__(self, "previous_stage", Stage(self.previous_stage))
        object.__setattr__(self, "next_stage", Stage(self.next_stage))
        if self.result not in {
            "requested",
            "applying",
            "applied",
            "failed",
            "partially_applied",
            "rolled_back",
        }:
            raise ValueError("invalid change result")
        if self.result in {"applied", "rolled_back"} and (
            not self.applied_at or not self.applied_workers
        ):
            raise ValueError("applied change requires actual time and worker evidence")
        if self.recovery_seconds is not None and (
            not math.isfinite(self.recovery_seconds) or self.recovery_seconds < 0
        ):
            raise ValueError("recovery time must be finite and nonnegative")
