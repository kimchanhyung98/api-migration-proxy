from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


class RevisionConflict(ConfigurationError):
    pass


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a nonempty string")


def _ratio(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        raise ConfigurationError(f"{name} must be a finite number between 0 and 1")


def _positive(value: object, name: str, *, integer: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int if integer else (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value <= 0
    ):
        raise ConfigurationError(f"{name} must be a positive {'integer' if integer else 'number'}")


def backend_origin(value: str) -> str:
    _text(value, "backend")
    if any(ord(char) < 33 or ord(char) > 126 for char in value) or "\\" in value:
        raise ConfigurationError("backend must be an ASCII HTTP origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ConfigurationError("invalid backend origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
        or "?" in value
        or "#" in value
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigurationError(
            "backend must be a fixed origin without credentials, path or query"
        )
    return value.rstrip("/")


_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")


def template_parts(template: str) -> tuple[str, ...]:
    _text(template, "path_template")
    if (
        not template.startswith("/")
        or template.startswith("//")
        or any(char in template for char in "?#*\\")
        or any(ord(char) < 33 for char in template)
    ):
        raise ConfigurationError(
            "path template must be an absolute path with whole-segment parameters"
        )
    parts = tuple(template.split("/"))
    names = []
    for part in parts:
        parameter = _PARAMETER.fullmatch(part)
        if parameter:
            names.append(parameter[1])
        elif "{" in part or "}" in part or part in {".", ".."}:
            raise ConfigurationError("invalid path template segment")
    if len(set(names)) != len(names):
        raise ConfigurationError("path parameters must be unique")
    return parts


@dataclass(frozen=True)
class RuntimeBudgets:
    serving_timeout_seconds: float
    shadow_timeout_seconds: float
    client_send_timeout_seconds: float
    shutdown_grace_seconds: float
    serving_max_inflight: int
    shadow_max_inflight: int
    request_capture_limit_bytes: int
    response_capture_limit_bytes: int

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            _positive(value, name, integer=not name.endswith("seconds"))


@dataclass(frozen=True)
class Cohort:
    mode: str
    group: str
    key_source: str
    salt: str = field(repr=False)
    algorithm: str = "sha256-v1"
    serialization: str = "length-prefix-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"user", "session", "tenant", "request"}:
            raise ConfigurationError("unsupported cohort mode")
        for name in ("group", "key_source", "salt"):
            _text(getattr(self, name), name)
        if self.mode == "request" and self.key_source != "request":
            raise ConfigurationError("request cohort must use an independent request key")
        if self.mode != "request" and self.key_source == "request":
            raise ConfigurationError("stable cohort requires a trusted identity key source")
        if self.algorithm != "sha256-v1" or self.serialization != "length-prefix-v1":
            raise ConfigurationError("unsupported cohort hash or serialization version")


@dataclass(frozen=True)
class ShadowPolicy:
    eligible: bool
    sample_ratio: float
    review_ref: str | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool or type(self.stopped) is not bool:
            raise ConfigurationError("shadow switches must be boolean")
        _ratio(self.sample_ratio, "shadow sample ratio")
        if self.review_ref is not None:
            _text(self.review_ref, "shadow review reference")
        if self.eligible:
            _text(self.review_ref, "shadow effects, authorization and data review reference")


@dataclass(frozen=True)
class ResponseContract:
    success_statuses: frozenset[int]
    expected_rejection_statuses: frozenset[int] = frozenset()
    unexpected_error_statuses: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        seen: set[int] = set()
        for name in (
            "success_statuses",
            "expected_rejection_statuses",
            "unexpected_error_statuses",
        ):
            statuses = frozenset(getattr(self, name))
            if any(type(status) is not int or not 100 <= status <= 599 for status in statuses):
                raise ConfigurationError(
                    "contract status codes must be integers between 100 and 599"
                )
            if seen.intersection(statuses):
                raise ConfigurationError("contract status classes overlap")
            if name != "unexpected_error_statuses" and any(status >= 500 for status in statuses):
                raise ConfigurationError(
                    "server errors cannot be registered as successful or expected"
                )
            seen.update(statuses)
            object.__setattr__(self, name, statuses)

    def classify(self, status_code: int | None, *, body_complete: bool = True) -> str:
        if status_code is not None and (
            status_code >= 500 or status_code in self.unexpected_error_statuses
        ):
            return "unexpected_error"
        if not body_complete or status_code is None:
            return "unknown"
        if status_code in self.success_statuses:
            return "success"
        if status_code in self.expected_rejection_statuses:
            return "expected_rejection"
        return "unknown"


@dataclass(frozen=True)
class Route:
    route_id: str
    method: str
    path_template: str
    v1: str
    v2: str
    owner: str
    rollout_enabled: bool
    v2_serve_ratio: float
    cohort: Cohort
    shadow: ShadowPolicy
    contract: ResponseContract
    comparison_policy_revision: str | None = None
    v1_path_template: str | None = None
    v2_path_template: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not _IDENTIFIER.fullmatch(self.route_id):
            raise ConfigurationError("route_id must be a stable identifier")
        if not isinstance(self.method, str) or not re.fullmatch(r"[A-Z]+", self.method):
            raise ConfigurationError("method must be an uppercase HTTP method")
        if type(self.rollout_enabled) is not bool:
            raise ConfigurationError("rollout_enabled must be boolean")
        _text(self.owner, "route owner")
        _ratio(self.v2_serve_ratio, "v2 serving ratio")
        for name, kind in (
            ("cohort", Cohort),
            ("shadow", ShadowPolicy),
            ("contract", ResponseContract),
        ):
            if not isinstance(getattr(self, name), kind):
                raise ConfigurationError(f"{name} must be a validated {kind.__name__}")
        parts = template_parts(self.path_template)
        names = {part for part in parts if _PARAMETER.fullmatch(part)}
        for name in ("v1_path_template", "v2_path_template"):
            mapped = getattr(self, name)
            if mapped is not None:
                mapped_names = {
                    part for part in template_parts(mapped) if _PARAMETER.fullmatch(part)
                }
                if mapped_names != names:
                    raise ConfigurationError("path mapping must preserve all registered parameters")
        object.__setattr__(self, "v1", backend_origin(self.v1))
        object.__setattr__(self, "v2", backend_origin(self.v2))
        if self.comparison_policy_revision is not None:
            _text(self.comparison_policy_revision, "comparison policy revision")
        if self.rollout_enabled and self.shadow.eligible and self.shadow.sample_ratio > 0:
            _text(self.comparison_policy_revision, "comparison policy revision")


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    revision: str
    previous_revision: str | None
    change_reason: str
    default_v1: str
    allowed_backends: frozenset[str]
    routes: tuple[Route, ...]
    budgets: RuntimeBudgets

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigurationError("unsupported configuration schema version")
        _text(self.revision, "revision")
        _text(self.change_reason, "change reason")
        if self.previous_revision is not None:
            _text(self.previous_revision, "previous revision")
            if self.previous_revision == self.revision:
                raise ConfigurationError("new revision must differ from previous revision")
        if not isinstance(self.budgets, RuntimeBudgets):
            raise ConfigurationError("runtime budgets are required")
        allowed = frozenset(backend_origin(value) for value in self.allowed_backends)
        object.__setattr__(self, "allowed_backends", allowed)
        object.__setattr__(self, "default_v1", backend_origin(self.default_v1))
        object.__setattr__(self, "routes", tuple(self.routes))
        if self.default_v1 not in allowed:
            raise ConfigurationError("default backend is outside the operator allowlist")
        ids: set[str] = set()
        groups: dict[str, tuple[object, ...]] = {}
        for index, route in enumerate(self.routes):
            if not isinstance(route, Route):
                raise ConfigurationError("routes must be validated Route instances")
            if route.route_id in ids:
                raise ConfigurationError("duplicate route id")
            ids.add(route.route_id)
            if route.v1 not in allowed or route.v2 not in allowed:
                raise ConfigurationError("route backend is outside the operator allowlist")
            policy = (route.rollout_enabled, route.v2_serve_ratio, route.cohort)
            previous = groups.setdefault(route.cohort.group, policy)
            if previous != policy:
                raise ConfigurationError("cohort group policies must be identical")
            parts = template_parts(route.path_template)
            for other in self.routes[:index]:
                other_parts = template_parts(other.path_template)
                if route.method != other.method or len(parts) != len(other_parts):
                    continue
                if all(
                    a == b or _PARAMETER.fullmatch(a) or _PARAMETER.fullmatch(b)
                    for a, b in zip(parts, other_parts)
                ):
                    raise ConfigurationError("ambiguous route templates")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Snapshot:
        try:
            data: dict[str, Any] = dict(value)
            data["budgets"] = RuntimeBudgets(**data["budgets"])
            routes = []
            for raw in data["routes"]:
                route = dict(raw)
                route["cohort"] = Cohort(**route["cohort"])
                route["shadow"] = ShadowPolicy(**route["shadow"])
                route["contract"] = ResponseContract(**route["contract"])
                routes.append(Route(**route))
            data["routes"] = tuple(routes)
            return cls(**data)
        except (TypeError, KeyError, AttributeError) as exc:
            raise ConfigurationError("incomplete or unknown configuration fields") from exc


@dataclass(frozen=True)
class ChangeRecord:
    revision: str
    previous_revision: str | None
    actor: str
    reason: str
    requested_at: str
    applied_at: str


class ConfigManager:
    def __init__(
        self,
        snapshot: Snapshot | None = None,
        *,
        worker_id: str = "local",
        history_limit: int = 1000,
    ) -> None:
        _text(worker_id, "worker id")
        _positive(history_limit, "history limit", integer=True)
        self._lock = RLock()
        self._snapshot: Snapshot | None = None
        self._history: deque[ChangeRecord] = deque(maxlen=history_limit)
        self._history_limit = history_limit
        self._used_revisions: set[str] = set()
        self._validators: list[Callable[[Snapshot], None]] = []
        self.worker_id = worker_id
        if snapshot is not None:
            self.apply(snapshot, actor="startup", expected_revision=None)

    @property
    def current(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def readiness(self) -> bool:
        return self.current is not None

    @property
    def history(self) -> tuple[ChangeRecord, ...]:
        with self._lock:
            return tuple(self._history)

    @property
    def worker_status(self) -> Mapping[str, str | None]:
        snapshot = self.current
        return MappingProxyType({self.worker_id: snapshot.revision if snapshot else None})

    def apply(
        self, snapshot: Snapshot, *, actor: str, expected_revision: str | None
    ) -> ChangeRecord:
        if not isinstance(snapshot, Snapshot):
            raise ConfigurationError("only validated snapshots may be applied")
        _text(actor, "change actor")
        requested = datetime.now(timezone.utc).isoformat()
        with self._lock:
            previous = self._snapshot.revision if self._snapshot else None
            if previous != expected_revision or (
                previous is not None and snapshot.previous_revision != previous
            ):
                raise RevisionConflict("configuration revision has changed")
            if snapshot.revision in self._used_revisions:
                raise RevisionConflict("configuration revision must be unique")
            if len(self._used_revisions) >= self._history_limit:
                raise ConfigurationError("configuration history capacity reached")
            for validator in self._validators:
                validator(snapshot)
            self._snapshot = snapshot
            self._used_revisions.add(snapshot.revision)
            record = ChangeRecord(
                snapshot.revision,
                previous,
                actor,
                snapshot.change_reason,
                requested,
                datetime.now(timezone.utc).isoformat(),
            )
            self._history.append(record)
            return record

    def add_validator(self, validator: Callable[[Snapshot], None]) -> None:
        with self._lock:
            if self._snapshot is not None:
                validator(self._snapshot)
            self._validators.append(validator)


def snapshot_from_dict(value: Mapping[str, object]) -> Snapshot:
    return Snapshot.from_dict(value)


def read_config(path: str | Path) -> Snapshot:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("unable to read a valid JSON configuration") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("configuration must be a JSON object")
    return snapshot_from_dict(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ConfigurationError("configuration contains duplicate object fields")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ConfigurationError("configuration contains a non-finite number")
