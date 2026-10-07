"""응답 표본의 v2 분배 비율과 오류 여부 검증."""

from __future__ import annotations

import math
from decimal import Decimal


def validate_parameters(requests: int, v2_ratio: float, tolerance: float) -> None:
    """요청 수·v2 비율·허용 오차 검증. 위반 시 ValueError 발생."""
    if type(requests) is not int or requests <= 0:
        raise ValueError("requests must be a positive integer")
    for name, value in (("v2_ratio", v2_ratio), ("tolerance", tolerance)):
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if not 0 <= v2_ratio <= 1:
        raise ValueError("v2_ratio must be between 0 and 1")
    if not 0 <= tolerance < 0.5:
        raise ValueError("tolerance must be at least 0 and less than 0.5")


def _validate_report(report: dict) -> None:
    try:
        backend = report["backend_versions"]
        counts, statuses = backend["counts"], report["status_counts"]
        if not isinstance(counts, dict) or set(counts) != {"v1", "v2", "unknown"}:
            raise ValueError
        if backend["denominator"] != "responses" or not isinstance(statuses, dict):
            raise ValueError
        values = [report[key] for key in ("attempts", "responses", "transport_errors")]
        values.extend(counts.values())
        values.extend(statuses.values())
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid User report counters or denominator") from None


def evaluate(report: dict, *, requests: int, v2_ratio: float, tolerance: float) -> dict:
    """User 집계 결과를 기대 응답 분배와 비교.

    Args:
        report: User CLI의 요청 결과 집계.
        requests: 예정한 총 요청 수.
        v2_ratio: 기대 v2 응답 비율.
        tolerance: 비율 허용 오차. 목표 비율 0 또는 1에서는 무시.

    Returns:
        통과 여부·실패 사유·기대 범위·관측값.

    Raises:
        ValueError: 매개변수 또는 보고서 집계 형식 오류.
    """
    validate_parameters(requests, v2_ratio, tolerance)
    _validate_report(report)
    if v2_ratio in {0, 1}:
        tolerance = 0
    ratio, margin = Decimal(str(v2_ratio)), Decimal(str(tolerance))
    lower = math.ceil(max(Decimal(0), ratio - margin) * requests)
    upper = math.floor(min(Decimal(1), ratio + margin) * requests)
    counts = report["backend_versions"]["counts"]
    observed = counts["v2"] / report["responses"] if report["responses"] else None
    failures = []
    if report["attempts"] != requests:
        failures.append("attempts must equal the requested sample size")
    if report["responses"] != requests or report["transport_errors"] != 0:
        failures.append("every attempt must receive a response without transport errors")
    if report["status_counts"] != {"200": requests}:
        failures.append("every response must have HTTP status 200")
    if counts["unknown"] != 0 or counts["v1"] + counts["v2"] != requests:
        failures.append("every response must identify either v1 or v2")
    if not lower <= counts["v2"] <= upper:
        failures.append("v2 response count is outside the allowed range")
    return {
        "passed": not failures,
        "failures": failures,
        "expected": {
            "requests": requests,
            "v2_ratio": v2_ratio,
            "tolerance": tolerance,
            "v2_count_min": lower,
            "v2_count_max": upper,
        },
        "observed": {
            "attempts": report["attempts"],
            "responses": report["responses"],
            "transport_errors": report["transport_errors"],
            "status_counts": report["status_counts"],
            "backend_counts": counts,
            "v2_ratio": observed,
        },
    }
