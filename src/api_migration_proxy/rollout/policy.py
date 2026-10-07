"""수동 전환·설정 전파·복구·철수 판단의 근거 검증."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class Stage(StrEnum):
    """기준선부터 프록시 철수까지의 마이그레이션 단계."""

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
    """단계 진행 조건별 현재 판정과 관측 근거."""

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
    """단계 진행 허용 여부, 차단 사유 및 제외 조건."""

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
    """제공된 수동 판정의 근거와 단계 진행 조건 검증.

    임계값 추론이나 실제 전환은 수행하지 않음.

    Args:
        stage: 진행 대상 Stage 또는 단계 문자열.
        epoch: 판정 대상 관측 에포크(str).
        evidence: GateEvidence 반복 가능 객체.
        comparison_active: 현재 비교 실행 여부(bool).
        prior_comparison_evidence: 과거 비교 근거 문자열 목록.
        current_serving_evidence: 현재 응답 제공 근거 문자열 목록.

    Returns:
        설정 변경 없이 반환하는 PromotionDecision.

    Raises:
        ValueError: 단계·에포크·근거 형식 오류 또는 중복 조건 평가.
    """
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
    """변경 요청의 기준 리비전이 최신 상태와 불일치."""

    pass


@dataclass(frozen=True)
class PropagationStatus:
    """목표 리비전에 대한 워커별 적용·누락·실패 상태."""

    desired_revision: str
    workers: Mapping[str, str]
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    failed: tuple[str, ...]
    stable: bool


class RevisionTracker:
    """로드 밸런서가 아닌 설정을 직접 읽는 워커 단위의 리비전 추적."""

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
        """기준 리비전 확인 후 목표 리비전 교체 및 실패 기록 초기화."""
        with self._lock:
            if previous_revision != self._desired:
                raise RevisionConflict("previous revision is stale")
            if not next_revision or next_revision == previous_revision:
                raise ValueError("a distinct next revision is required")
            self._desired = next_revision
            self._failures.clear()

    def report(self, worker, revision, *, failed=False):
        """등록 워커의 적용 리비전과 실패 여부 갱신."""
        with self._lock:
            if worker not in self._expected or not revision:
                raise ValueError("unknown worker or empty revision")
            self._reports[worker] = revision
            if failed:
                self._failures.add(worker)
            else:
                self._failures.discard(worker)

    def status(self):
        """모든 워커의 보고 여부·리비전 일치·실패를 종합한 전파 상태 반환."""
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
    """현재 복구 점검 결과, 근거 및 실측·목표 복구 시간."""

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
    """서로 독립적인 v2 응답 비율과 shadow 표본 비율."""

    v2_serve_ratio: float
    shadow_sample_ratio: float

    def __post_init__(self):
        if any(
            isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 1
            for v in (self.v2_serve_ratio, self.shadow_sample_ratio)
        ):
            raise ValueError("ratios must be finite values in [0, 1]")

    def stop_shadow(self):
        """v2 응답 비율을 유지하면서 shadow 비율을 0으로 변경한 복사본 반환."""
        return ControlRatios(self.v2_serve_ratio, 0)

    def rollback_serving(self, readiness):
        """복구 준비 통과 시 shadow 비율을 유지한 v1 전환 복사본 반환."""
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
    """잔여 호출자·직접 경로·보존 책임을 포함한 철수 근거."""

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
    """전환 주체, 리비전, 조건별 근거 및 적용·복구 결과 기록."""

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
