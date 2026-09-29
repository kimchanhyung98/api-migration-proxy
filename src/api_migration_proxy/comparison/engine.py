from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from decimal import MAX_EMAX, MAX_PREC, Decimal, InvalidOperation, localcontext
from typing import Any

REASON_CODES = frozenset(
    {
        "shadow_not_dispatched",
        "timeout",
        "cancelled",
        "transport_error",
        "unexpected_api_error",
        "execution_outcome_unknown",
        "contract_class_unknown",
        "status_unavailable",
        "body_incomplete",
        "capture_oversized",
        "capture_unavailable",
        "decoded_body_oversized",
        "content_encoding_unsupported",
        "content_encoding_invalid",
        "json_encoding_invalid",
        "json_depth_limit",
        "json_number_limit",
        "json_number_invalid",
        "json_duplicate_key",
        "json_parse_error",
        "json_node_limit",
        "policy_mapping_conflict",
        "logical_request_context_mismatch",
        "logical_request_context_unknown",
        "authorization_context_mismatch",
        "authorization_context_unknown",
        "data_context_mismatch",
        "data_context_unknown",
        "contract_class_mismatch",
        "status_mismatch",
        "required_header_mismatch",
        "json_type_mismatch",
        "json_field_mismatch",
        "json_array_length_mismatch",
        "json_value_mismatch",
        "matched",
    }
)


@dataclass(frozen=True)
class BackendResponse:
    backend: str
    role: str
    execution_outcome: str
    contract_class: str
    status_code: int | None
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    capture_state: str = "complete"
    body_complete: bool = True

    def __post_init__(self) -> None:
        if type(self.body_complete) is not bool:
            raise ValueError("comparison_response_complete_invalid")
        if self.body is not None:
            if not isinstance(self.body, (bytes, bytearray, memoryview)):
                raise ValueError("comparison_response_body_invalid")
            object.__setattr__(self, "body", bytes(self.body))
        headers = _sequence(self.headers, "comparison_response_headers_invalid")
        normalized = []
        for header in headers:
            pair = _sequence(header, "comparison_response_headers_invalid")
            if len(pair) != 2 or any(not isinstance(value, str) for value in pair):
                raise ValueError("comparison_response_headers_invalid")
            normalized.append(pair)
        object.__setattr__(self, "headers", tuple(normalized))


@dataclass(frozen=True)
class ComparisonContext:
    logical_request_equal: bool | None = None
    authorization_equal: bool | None = None
    data_comparable: bool | None = None


@dataclass(frozen=True)
class FieldMapping:
    v1_path: str
    v2_path: str


@dataclass(frozen=True)
class Tolerance:
    path: str
    absolute: Decimal


@dataclass(frozen=True)
class ComparisonPolicy:
    revision: str
    max_body_bytes: int
    max_decoded_body_bytes: int
    max_json_depth: int
    max_json_nodes: int
    max_number_digits: int
    max_number_exponent: int
    max_differences: int
    max_diff_paths: int
    required_headers: tuple[str, ...] = ()
    ignore_paths: tuple[str, ...] = ()
    tolerances: tuple[Tolerance, ...] = ()
    mappings: tuple[FieldMapping, ...] = ()
    allowed_diff_paths: frozenset[str] = frozenset()
    allow_empty_body: bool = False
    status_equivalences: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("comparison_policy_revision_invalid")
        if type(self.allow_empty_body) is not bool:
            raise ValueError("comparison_policy_empty_body_invalid")
        for name in ("required_headers", "ignore_paths", "tolerances", "mappings"):
            object.__setattr__(
                self, name, _sequence(getattr(self, name), "comparison_policy_rules_invalid")
            )
        if not isinstance(self.allowed_diff_paths, (tuple, list, set, frozenset)):
            raise ValueError("comparison_policy_paths_invalid")
        if any(not isinstance(path, str) for path in self.allowed_diff_paths):
            raise ValueError("comparison_policy_paths_invalid")
        object.__setattr__(self, "allowed_diff_paths", frozenset(self.allowed_diff_paths))
        pairs = _sequence(self.status_equivalences, "comparison_policy_status_invalid")
        object.__setattr__(
            self,
            "status_equivalences",
            tuple(_sequence(pair, "comparison_policy_status_invalid") for pair in pairs),
        )
        if any(type(rule) is not Tolerance for rule in self.tolerances):
            raise ValueError("comparison_policy_tolerance_invalid")
        if any(type(rule) is not FieldMapping for rule in self.mappings):
            raise ValueError("comparison_policy_mapping_invalid")
        limits = (
            self.max_body_bytes,
            self.max_decoded_body_bytes,
            self.max_json_depth,
            self.max_json_nodes,
            self.max_number_digits,
            self.max_number_exponent,
            self.max_differences,
            self.max_diff_paths,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("comparison_policy_limits_invalid")
        if self.max_json_depth > 128:
            raise ValueError("comparison_policy_depth_unsupported")
        if (
            2 * self.max_number_exponent + self.max_number_digits + 2 > MAX_PREC
            or self.max_number_exponent + self.max_number_digits + 1 > MAX_EMAX
        ):
            raise ValueError("comparison_policy_number_limit_unsupported")
        paths = (*self.ignore_paths, *self.allowed_diff_paths, *(t.path for t in self.tolerances))
        for path in paths:
            _parts(path)
        if len(self.ignore_paths) != len(set(self.ignore_paths)):
            raise ValueError("comparison_policy_duplicate_rule")
        tolerance_paths = [rule.path for rule in self.tolerances]
        if len(tolerance_paths) != len(set(tolerance_paths)):
            raise ValueError("comparison_policy_duplicate_rule")
        for tolerance in self.tolerances:
            if not isinstance(tolerance.absolute, Decimal):
                raise ValueError("comparison_policy_tolerance_invalid")
            if not tolerance.absolute.is_finite() or tolerance.absolute < 0:
                raise ValueError("comparison_policy_tolerance_invalid")
            _check_number(tolerance.absolute, self)
        mapping_paths: list[str] = []
        for mapping in self.mappings:
            if not _parts(mapping.v1_path) or not _parts(mapping.v2_path):
                raise ValueError("comparison_policy_mapping_invalid")
            if mapping.v1_path == mapping.v2_path:
                raise ValueError("comparison_policy_mapping_invalid")
            mapping_paths.extend((mapping.v1_path, mapping.v2_path))
        for index, left in enumerate(mapping_paths):
            for right in mapping_paths[index + 1 :]:
                if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                    raise ValueError("comparison_policy_mapping_overlap")
        if any(
            not isinstance(h, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", h)
            for h in self.required_headers
        ):
            raise ValueError("comparison_policy_header_invalid")
        if len({h.lower() for h in self.required_headers}) != len(self.required_headers):
            raise ValueError("comparison_policy_duplicate_header")
        for pair in self.status_equivalences:
            if len(pair) != 2 or any(
                type(status) is not int or not 100 <= status <= 599 for status in pair
            ):
                raise ValueError("comparison_policy_status_invalid")


@dataclass(frozen=True)
class ComparisonResult:
    result: str
    reason: str
    comparison_class: str
    policy_revision: str
    difference_count: int = 0
    difference_count_limited: bool = False
    difference_paths: tuple[str, ...] = ()
    difference_paths_truncated: bool = False
    redacted_difference_count: int = 0
    raw_difference_count: int = 0
    raw_difference_count_limited: bool = False
    applied_rules: tuple[str, ...] = ()


class _NotComparable(ValueError):
    pass


def _sequence(value: Any, reason: str) -> tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(reason)
    return tuple(value)


def _parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("comparison_policy_path_invalid")
    if re.search(r"~(?![01])", path):
        raise ValueError("comparison_policy_path_invalid")
    if not path:
        return ()
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/"))


def _path(parent: str, key: str) -> str:
    return parent + "/" + key.replace("~", "~0").replace("/", "~1")


def _check_number(number: Decimal, policy: ComparisonPolicy) -> None:
    _, digits, exponent = number.as_tuple()
    if len(digits) > policy.max_number_digits or abs(int(exponent)) > policy.max_number_exponent:
        raise _NotComparable("json_number_limit")


def _header_values(response: BackendResponse, name: str) -> tuple[str, ...]:
    return tuple(value for key, value in response.headers if key.lower() == name.lower())


def _decode(response: BackendResponse, policy: ComparisonPolicy) -> bytes:
    body = response.body
    if body is None:
        raise _NotComparable("capture_unavailable")
    if len(body) > policy.max_body_bytes:
        raise _NotComparable("capture_oversized")
    encoding = ",".join(_header_values(response, "content-encoding")).strip().lower()
    if encoding in ("", "identity"):
        if len(body) > policy.max_decoded_body_bytes:
            raise _NotComparable("decoded_body_oversized")
        return body
    if encoding not in ("gzip", "deflate"):
        raise _NotComparable("content_encoding_unsupported")
    remaining = body
    decoded = bytearray()
    try:
        while remaining:
            decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
            decoded.extend(
                decoder.decompress(remaining, policy.max_decoded_body_bytes - len(decoded) + 1)
            )
            if len(decoded) > policy.max_decoded_body_bytes or decoder.unconsumed_tail:
                raise _NotComparable("decoded_body_oversized")
            if not decoder.eof:
                raise _NotComparable("content_encoding_invalid")
            remaining = decoder.unused_data
            if remaining and encoding != "gzip":
                raise _NotComparable("content_encoding_invalid")
    except zlib.error:
        raise _NotComparable("content_encoding_invalid") from None
    if not body:
        raise _NotComparable("content_encoding_invalid")
    return bytes(decoded)


def _parse(body: bytes, policy: ComparisonPolicy) -> Any:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _NotComparable("json_encoding_invalid") from None
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > policy.max_json_depth:
                raise _NotComparable("json_depth_limit")
        elif char in "]}":
            depth -= 1

    def number(value: str) -> Decimal:
        if (
            sum(char.isdigit() for char in value.split("e")[0].split("E")[0])
            > policy.max_number_digits
        ):
            raise _NotComparable("json_number_limit")
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise _NotComparable("json_number_invalid") from None
        _check_number(result, policy)
        return result

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _NotComparable("json_duplicate_key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise _NotComparable("json_number_invalid")

    try:
        result = json.loads(
            text,
            parse_int=number,
            parse_float=number,
            parse_constant=invalid_constant,
            object_pairs_hook=object_pairs,
        )
    except (json.JSONDecodeError, RecursionError):
        raise _NotComparable("json_parse_error") from None
    pending = [result]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if nodes > policy.max_json_nodes:
            raise _NotComparable("json_node_limit")
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return result


@dataclass
class _Differences:
    policy: ComparisonPolicy
    count: int = 0
    limited: bool = False
    paths: list[str] = field(default_factory=list)
    redacted: int = 0
    paths_truncated: bool = False
    reason: str = "matched"

    def add(self, path: str, reason: str) -> None:
        if self.count == self.policy.max_differences:
            self.limited = True
            return
        if not self.count:
            self.reason = reason
        self.count += 1
        if path not in self.policy.allowed_diff_paths:
            self.redacted += 1
        elif len(self.paths) < self.policy.max_diff_paths:
            self.paths.append(path)
        else:
            self.paths_truncated = True


_MISSING = object()


def _walk(left: Any, right: Any, differences: _Differences, applied: set[str] | None) -> None:
    policy = differences.policy
    ignored = (
        {path: index for index, path in enumerate(policy.ignore_paths)}
        if applied is not None
        else {}
    )
    tolerances = (
        {rule.path: (index, rule.absolute) for index, rule in enumerate(policy.tolerances)}
        if applied is not None
        else {}
    )
    pending = [("", left, right)]
    while pending:
        path, a, b = pending.pop()
        if differences.limited:
            break
        if path in ignored:
            if applied is not None:
                applied.add(f"ignore:{ignored[path]}")
            continue
        if type(a) is not type(b):
            differences.add(
                path,
                "json_type_mismatch"
                if a is not _MISSING and b is not _MISSING
                else "json_field_mismatch",
            )
        elif isinstance(a, dict):
            keys = list(a)
            keys.extend(key for key in b if key not in a)
            pending.extend(
                (_path(path, key), a.get(key, _MISSING), b.get(key, _MISSING))
                for key in reversed(keys)
            )
        elif isinstance(a, list):
            if len(a) != len(b):
                differences.add(path, "json_array_length_mismatch")
            pending.extend(
                (_path(path, str(index)), a[index], b[index])
                for index in reversed(range(min(len(a), len(b))))
            )
        elif a != b:
            if isinstance(a, Decimal) and path in tolerances:
                index, absolute = tolerances[path]
                with localcontext() as context:
                    context.prec = 2 * policy.max_number_exponent + policy.max_number_digits + 2
                    context.Emax = policy.max_number_exponent + policy.max_number_digits + 1
                    context.Emin = -context.Emax
                    delta = abs(a - b)
                if delta <= absolute:
                    if applied is not None:
                        applied.add(f"tolerance:{index}")
                    continue
            differences.add(path, "json_value_mismatch")


def _mapping_parent(value: Any, parts: tuple[str, ...]) -> dict[str, Any] | None:
    for part in parts[:-1]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, dict) else None


def _map_fields(value: Any, policy: ComparisonPolicy, applied: set[str]) -> None:
    for index, mapping in enumerate(policy.mappings):
        source = _parts(mapping.v2_path)
        target = _parts(mapping.v1_path)
        source_parent = _mapping_parent(value, source)
        target_parent = _mapping_parent(value, target)
        if source_parent is None or source[-1] not in source_parent:
            if target_parent is not None and target[-1] in target_parent:
                raise _NotComparable("policy_mapping_conflict")
            continue
        if target_parent is None or target[-1] in target_parent:
            raise _NotComparable("policy_mapping_conflict")
        target_parent[target[-1]] = source_parent.pop(source[-1])
        applied.add(f"mapping:{index}")


def _compare_metadata(
    v1: BackendResponse, v2: BackendResponse, differences: _Differences, applied: set[str] | None
) -> str:
    policy = differences.policy
    comparison_class = v1.contract_class if v1.contract_class == v2.contract_class else "mixed"
    if comparison_class == "mixed":
        differences.add("@contract_class", "contract_class_mismatch")
    if v1.status_code != v2.status_code:
        if applied is not None and (v1.status_code, v2.status_code) in policy.status_equivalences:
            applied.add(
                f"status:{policy.status_equivalences.index((v1.status_code, v2.status_code))}"
            )
        else:
            differences.add("@status", "status_mismatch")
    for name in policy.required_headers:
        left, right = _header_values(v1, name), _header_values(v2, name)
        if not left or not right or left != right:
            differences.add("@header", "required_header_mismatch")
    return comparison_class


def compare(
    v1: BackendResponse, v2: BackendResponse, policy: ComparisonPolicy, context: ComparisonContext
) -> ComparisonResult:
    def unavailable(result: str, reason: str) -> ComparisonResult:
        return ComparisonResult(result, reason, "unavailable", policy.revision)

    responses = (v1, v2)
    if v1.backend != "v1" or v2.backend != "v2" or {v1.role, v2.role} != {"serving", "shadow"}:
        raise ValueError("comparison_backend_roles_invalid")
    if any(response.execution_outcome == "not_dispatched" for response in responses):
        return unavailable("not_executed", "shadow_not_dispatched")
    for response in responses:
        if response.execution_outcome in ("timeout", "cancelled", "transport_error"):
            return unavailable("execution_error", response.execution_outcome)
        if response.contract_class == "unexpected_error":
            return unavailable("execution_error", "unexpected_api_error")
    if any(response.execution_outcome != "http_response" for response in responses):
        return unavailable("not_comparable", "execution_outcome_unknown")
    for response in responses:
        if response.contract_class not in ("success", "expected_rejection"):
            return unavailable("not_comparable", "contract_class_unknown")
        if response.status_code is None:
            return unavailable("not_comparable", "status_unavailable")
        if not response.body_complete:
            return unavailable("not_comparable", "body_incomplete")
        if response.capture_state != "complete":
            if not (
                response.capture_state == "not_needed"
                and policy.allow_empty_body
                and response.body == b""
            ):
                return unavailable(
                    "not_comparable",
                    "capture_oversized"
                    if response.capture_state == "oversized"
                    else "capture_unavailable",
                )
    for name, value in (
        ("logical_request", context.logical_request_equal),
        ("authorization", context.authorization_equal),
        ("data", context.data_comparable),
    ):
        if value is not True:
            return unavailable(
                "not_comparable",
                name + ("_context_mismatch" if value is False else "_context_unknown"),
            )
    applied: set[str] = set()
    try:
        bodies = tuple(_decode(response, policy) for response in responses)
        if policy.allow_empty_body and all(body == b"" for body in bodies):
            parsed = (None, None)
        else:
            parsed = tuple(_parse(body, policy) for body in bodies)
        raw = _Differences(policy)
        _compare_metadata(v1, v2, raw, None)
        _walk(parsed[0], parsed[1], raw, None)
        _map_fields(parsed[1], policy, applied)
    except _NotComparable as error:
        return unavailable("not_comparable", str(error))
    differences = _Differences(policy)
    comparison_class = _compare_metadata(v1, v2, differences, applied)
    _walk(parsed[0], parsed[1], differences, applied)
    return ComparisonResult(
        "different" if differences.count else "matched",
        differences.reason,
        comparison_class,
        policy.revision,
        differences.count,
        differences.limited,
        tuple(differences.paths),
        differences.paths_truncated or differences.limited,
        differences.redacted,
        raw.count,
        raw.limited,
        tuple(sorted(applied)),
    )
