"""저장용 이벤트의 필드 제한, 상세 마스킹 및 보존 기한 설정."""

from __future__ import annotations

import json
import math
import random
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
    """승인 필드만 대상으로 하는 상세 표본·마스킹·보존 정책."""

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
    """이벤트 복사본 간 재접수 방지를 위해 공유하는 제출 상태."""

    submitted: bool = False

    def __deepcopy__(self, memo: dict[int, object]) -> _DeliveryState:
        return self


@dataclass
class CollectionEvent:
    """검증된 요약·선택적 상세와 각각의 보존 기한."""

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
    """요약 필드 제한과 상세 마스킹을 거쳐 수집 이벤트 생성.

    Args:
        summary: 요청·실행·비교 결과를 담은 요약 원본.
        retention_seconds: 요약 보존 기간(초).
        detail_policy: 상세 수집 정책. None이면 상세 수집 비활성화.
        details: 마스킹 전 상세 필드.
        allowed_difference_paths: 요약에 기록을 허용한 차이 경로.
        now: 생성 시각의 Unix 타임스탬프. None이면 현재 시각.
        work_started_at: 전체 작업 시작 시점의 단조 시각. None이면 현재 시각.
        sample: 상세 표본 선택에 사용할 난수 함수.

    Returns:
        저장 가능한 필드와 보존 기한을 갖춘 이벤트.

    Raises:
        ValueError: 보존 기간 또는 생성 시각 오류.
    """
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
