"""요청 라우트 매칭, 코호트 배정 및 원본 경로 보존."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .configuration import Cohort, Route, Snapshot, template_parts


@dataclass(frozen=True)
class RouteMatch:
    """매칭된 라우트와 변경 불가능한 경로 매개변수."""

    route: Route
    parameters: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class Assignment:
    """응답 백엔드 배정 결과, 선택 사유 및 해시 구간 값."""

    backend: str
    reason: str
    bucket: float | None


def match_route(snapshot: Snapshot, method: str, path: str) -> RouteMatch | None:
    """메서드 대소문자와 경로 세그먼트가 일치하는 라우트 반환. 미등록은 None."""
    request_parts = tuple(path.split("/"))
    for route in snapshot.routes:
        parts = template_parts(route.path_template)
        if route.method != method or len(parts) != len(request_parts):
            continue
        parameters = {}
        for expected, actual in zip(parts, request_parts):
            if expected.startswith("{") and expected.endswith("}") and actual:
                parameters[expected[1:-1]] = actual
            elif expected != actual:
                break
        else:
            return RouteMatch(route, parameters)
    return None


def cohort_bucket(cohort: Cohort, key: str) -> float:
    """코호트 키를 결정적인 [0, 1) 구간 값으로 변환."""
    if not isinstance(key, str) or not key:
        raise ValueError("cohort key must be a nonempty string")
    digest = hashlib.sha256()
    for value in (cohort.algorithm, cohort.serialization, cohort.group, key, cohort.salt):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    # 최댓값이 1로 반올림되지 않도록 53비트만 사용.
    return (int.from_bytes(digest.digest()[:8], "big") >> 11) / 2**53


def choose_serving(
    route: Route,
    *,
    trusted_identity: Mapping[str, str] | None = None,
    request_key: str | None = None,
) -> Assignment:
    """코호트 정책에 따라 응답 백엔드 배정.

    Args:
        route: 대상 라우트의 전환·코호트 정책.
        trusted_identity: 검증된 사용자·세션·테넌트 식별자.
        request_key: 요청 단위 코호트의 독립 키.

    Returns:
        백엔드·사유·구간 값을 담은 배정 결과. 전환 비활성화 또는 키 부재 시 v1.
    """
    if not route.rollout_enabled:
        return Assignment("v1", "rollout_disabled", None)
    cohort = route.cohort
    key = (
        request_key if cohort.mode == "request" else (trusted_identity or {}).get(cohort.key_source)
    )
    if not isinstance(key, str) or not key:
        return Assignment("v1", "missing_cohort_key", None)
    try:
        bucket = cohort_bucket(cohort, key)
    except UnicodeEncodeError:
        return Assignment("v1", "missing_cohort_key", None)
    return Assignment("v2" if bucket < route.v2_serve_ratio else "v1", "cohort", bucket)


def backend_url(
    snapshot: Snapshot,
    match: RouteMatch | None,
    backend: str,
    raw_path: bytes,
    query_string: bytes = b"",
) -> str:
    """원본 인코딩을 유지하여 백엔드 URL 구성.

    Args:
        snapshot: 기본 v1 원본 주소를 포함한 설정.
        match: 매칭 결과. None이면 기본 v1 사용.
        backend: 대상 백엔드 v1 또는 v2.
        raw_path: ASCII로 인코딩된 원본 요청 경로.
        query_string: 선행 물음표를 제외한 ASCII 원본 쿼리.

    Returns:
        필요한 경로 매핑을 적용한 백엔드 URL.

    Raises:
        ValueError: 백엔드·원본 경로·쿼리 제약 위반 또는 ASCII 디코딩 실패.
    """
    if backend not in {"v1", "v2"} or (match is None and backend != "v1"):
        raise ValueError("unregistered requests must use the default v1 backend")
    if (
        not raw_path.startswith(b"/")
        or b"?" in raw_path
        or b"#" in raw_path
        or any(byte < 33 for byte in raw_path)
    ):
        raise ValueError("invalid raw request path")
    origin = snapshot.default_v1 if match is None else getattr(match.route, backend)
    mapping = None if match is None else getattr(match.route, f"{backend}_path_template")
    if match is None or mapping is None:
        path = raw_path.decode("ascii")
    else:
        raw_parts = raw_path.decode("ascii").split("/")
        registered_parts = template_parts(match.route.path_template)
        if len(raw_parts) != len(registered_parts):
            raise ValueError("raw path does not match the registered path template")
        raw_parameters = {
            registered: raw
            for registered, raw in zip(registered_parts, raw_parts)
            if registered.startswith("{") and registered.endswith("}")
        }
        path = "/".join(raw_parameters.get(part, part) for part in template_parts(mapping))
    if b"#" in query_string or any(byte < 32 or byte == 127 for byte in query_string):
        raise ValueError("invalid raw query string")
    query = "?" + query_string.decode("ascii") if query_string else ""
    return origin + path + query
