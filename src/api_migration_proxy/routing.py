from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .config import Cohort, Route, Snapshot, template_parts


@dataclass(frozen=True)
class RouteMatch:
    route: Route
    parameters: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class Assignment:
    backend: str
    reason: str
    bucket: float | None


def match_route(snapshot: Snapshot, method: str, path: str) -> RouteMatch | None:
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
    if not isinstance(key, str) or not key:
        raise ValueError("cohort key must be a nonempty string")
    digest = hashlib.sha256()
    for value in (cohort.algorithm, cohort.serialization, cohort.group, key, cohort.salt):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    # 53 bits avoid rounding the largest 256-bit digest up to a bucket of 1.
    return (int.from_bytes(digest.digest()[:8], "big") >> 11) / 2**53


def choose_serving(
    route: Route,
    *,
    trusted_identity: Mapping[str, str] | None = None,
    request_key: str | None = None,
) -> Assignment:
    if not route.rollout_enabled:
        return Assignment("v1", "rollout_disabled", None)
    cohort = route.cohort
    key = (
        request_key if cohort.mode == "request" else (trusted_identity or {}).get(cohort.key_source)
    )
    if not isinstance(key, str) or not key:
        return Assignment("v1", "missing_cohort_key", None)
    bucket = cohort_bucket(cohort, key)
    return Assignment("v2" if bucket < route.v2_serve_ratio else "v1", "cohort", bucket)


def backend_url(
    snapshot: Snapshot,
    match: RouteMatch | None,
    backend: str,
    raw_path: bytes,
    query_string: bytes = b"",
) -> str:
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
        path = mapping
        for registered, raw in zip(registered_parts, raw_parts):
            if registered.startswith("{") and registered.endswith("}"):
                path = path.replace(registered, raw)
    if b"#" in query_string or any(byte < 32 or byte == 127 for byte in query_string):
        raise ValueError("invalid raw query string")
    query = "?" + query_string.decode("ascii") if query_string else ""
    return origin + path + query
