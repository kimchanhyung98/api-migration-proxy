from __future__ import annotations

from dataclasses import dataclass

from .events import _finite


@dataclass(frozen=True)
class QueryAccess:
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
