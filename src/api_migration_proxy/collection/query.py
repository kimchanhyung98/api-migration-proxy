"""이벤트 조회 조건과 접근 범위 정의."""

from __future__ import annotations

from dataclasses import dataclass

from .events import _finite


@dataclass(frozen=True)
class QueryAccess:
    """조회 허용 라우트·기간·행 수 및 상세 접근 권한."""

    routes: frozenset[str]
    max_rows: int
    max_period_seconds: float
    allow_details: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.max_rows) is not int
            or self.max_rows <= 0
            or not _finite(self.max_period_seconds)
            or self.max_period_seconds <= 0
            or type(self.allow_details) is not bool
        ):
            raise ValueError("invalid_query_access_limits")


@dataclass(frozen=True)
class EventQuery:
    """시작 포함·종료 제외 시간 구간과 이벤트 필터."""

    start: float
    end: float
    routes: frozenset[str]
    limit: int
    result: str | None = None
    reason: str | None = None
    configuration_revision: str | None = None
    comparison_policy_revision: str | None = None
    event_id: str | None = None
    backend: str | None = None
    role: str | None = None
    deployment_revision: str | None = None
