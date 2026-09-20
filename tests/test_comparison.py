import gzip
import json
import zlib
from dataclasses import asdict, replace
from decimal import Decimal

import pytest

from api_migration_proxy.comparison import (
    BackendResponse,
    ComparisonContext,
    ComparisonPolicy,
    FieldMapping,
    Tolerance,
    compare,
)


@pytest.fixture
def policy():
    return ComparisonPolicy(
        revision="fixture-comparison-1",
        max_body_bytes=4096,
        max_decoded_body_bytes=4096,
        max_json_depth=16,
        max_json_nodes=100,
        max_number_digits=100,
        max_number_exponent=100,
        max_differences=20,
        max_diff_paths=10,
    )


CONTEXT = ComparisonContext(True, True, True)


def response(body=b"{}", *, backend="v1", **kwargs):
    return BackendResponse(
        backend=backend,
        role="serving" if backend == "v1" else "shadow",
        execution_outcome="http_response",
        contract_class="success",
        status_code=200,
        body=body,
        **kwargs,
    )


def pair(left, right, policy, context=CONTEXT):
    return compare(response(left), response(right, backend="v2"), policy, context)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (b'{"a":1,"b":2}', b'{"b":2,"a":1}', "matched"),
        (b"[1,2]", b"[2,1]", "different"),
        (b"[1,1]", b"[1]", "different"),
        (b'{"a":null}', b"{}", "different"),
        (b"true", b"1", "different"),
        (b'"1"', b"1", "different"),
        (b"1", b"1.00", "matched"),
        (b"9007199254740993", b"9007199254740992", "different"),
        (b"0.1234567890123456789012345678901", b"0.1234567890123456789012345678902", "different"),
        (b'"Mixed"', b'"mixed"', "different"),
        (b'"e\\u0301"', b'"\\u00e9"', "different"),
    ],
)
def test_json_contract_preserves_meaning(policy, left, right, expected):
    assert pair(left, right, policy).result == expected


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'{"a":1,"a":1}', "json_duplicate_key"),
        (b'{"a": {"nested": 1,"nested":1}}', "json_duplicate_key"),
        (b'{"a":', "json_parse_error"),
        (b"NaN", "json_number_invalid"),
        (b"Infinity", "json_number_invalid"),
        (b"-Infinity", "json_number_invalid"),
        (b"1e101", "json_number_limit"),
        (b"1e-101", "json_number_limit"),
        (b"1e9999999999999999999999999", "json_number_invalid"),
        (b"9" * 101, "json_number_limit"),
        (b"\xff", "json_encoding_invalid"),
        (b"[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]", "json_depth_limit"),
        (b"[] trailing data", "json_parse_error"),
    ],
)
def test_ambiguous_or_bounded_input_is_never_matched(policy, body, reason):
    result = pair(body, body, policy)
    assert (result.result, result.reason, result.comparison_class) == (
        "not_comparable",
        reason,
        "unavailable",
    )


def test_depth_scanner_ignores_brackets_and_escaped_quotes_in_strings(policy):
    body = json.dumps({"value": '["' * 50}).encode()
    assert pair(body, body, policy).result == "matched"


def test_node_limit_and_wire_limit(policy):
    body = json.dumps(list(range(100))).encode()
    assert pair(body, body, policy).reason == "json_node_limit"
    body = b" " * 4096 + b"{}"
    assert pair(body, body, policy).reason == "capture_oversized"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_json_is_compared_without_mutating_wire_response(policy, encoding):
    body = b'{"price":12345}'
    compressed = gzip.compress(body) if encoding == "gzip" else zlib.compress(body)
    v1 = response(compressed, headers=(("Content-Encoding", encoding),))
    v2 = response(body, backend="v2")
    assert compare(v1, v2, policy, CONTEXT).result == "matched"
    assert v1.body == compressed


def test_concatenated_gzip_and_inflation_limit(policy):
    compressed = gzip.compress(b'{"x":') + gzip.compress(b"1}")
    result = compare(
        response(compressed, headers=(("content-encoding", "gzip"),)),
        response(b'{"x":1}', backend="v2"),
        policy,
        CONTEXT,
    )
    assert result.result == "matched"
    bomb = gzip.compress(b" " * 5000 + b"{}")
    result = compare(
        response(bomb, headers=(("content-encoding", "gzip"),)),
        response(backend="v2"),
        policy,
        CONTEXT,
    )
    assert result.reason == "decoded_body_oversized"


@pytest.mark.parametrize(
    ("encoding", "body", "reason"),
    [
        ("br", b"anything", "content_encoding_unsupported"),
        ("gzip, gzip", b"anything", "content_encoding_unsupported"),
        ("gzip", b"not gzip", "content_encoding_invalid"),
        ("gzip", gzip.compress(b"{}")[:-1], "content_encoding_invalid"),
        ("deflate", zlib.compress(b"{}") + b"trailing", "content_encoding_invalid"),
        ("gzip", b"", "content_encoding_invalid"),
    ],
)
def test_unsupported_and_invalid_compression(policy, encoding, body, reason):
    result = compare(
        response(body, headers=(("content-encoding", encoding),)),
        response(backend="v2"),
        policy,
        CONTEXT,
    )
    assert (result.result, result.reason) == ("not_comparable", reason)


def test_exact_ignore_tolerance_mapping_are_auditable_and_do_not_change_responses(policy):
    policy = replace(
        policy,
        ignore_paths=("/request_id",),
        tolerances=(Tolerance("/price", Decimal("0.01")),),
        mappings=(FieldMapping("/id", "/item_id"),),
    )
    left = b'{"id":1,"price":120.10,"request_id":"a"}'
    right = b'{"item_id":1,"price":120.11,"request_id":"b"}'
    result = pair(left, right, policy)
    assert result.result == "matched"
    assert result.raw_difference_count == 4
    assert result.applied_rules == ("ignore:0", "mapping:0", "tolerance:0")
    assert result.policy_revision == policy.revision
    assert left == b'{"id":1,"price":120.10,"request_id":"a"}'
    assert right == b'{"item_id":1,"price":120.11,"request_id":"b"}'


def test_tolerance_decimal_boundary_and_adjacent_path_unchanged(policy):
    policy = replace(
        policy, tolerances=(Tolerance("/amount", Decimal("0.0000000000000000000000000000001")),)
    )
    assert (
        pair(
            b'{"amount":1.0000000000000000000000000000000}',
            b'{"amount":1.0000000000000000000000000000001}',
            policy,
        ).result
        == "matched"
    )
    assert (
        pair(
            b'{"amount":1.0000000000000000000000000000000}',
            b'{"amount":1.0000000000000000000000000000002}',
            policy,
        ).result
        == "different"
    )
    assert (
        pair(
            b'{"nested":{"amount":1}}',
            b'{"nested":{"amount":1.0000000000000000000000000000001}}',
            policy,
        ).result
        == "different"
    )


def test_numeric_policy_controls_decimal_exponent_range(policy):
    policy = replace(policy, max_number_exponent=1_000_000, tolerances=(Tolerance("", Decimal(0)),))
    assert pair(b"1e1000000", b"0", policy).result == "different"
    assert pair(b"1e-1000000", b"0", policy).result == "different"


def test_ignore_is_exact_and_default_never_ignores_named_ids(policy):
    assert pair(b'{"request_id":"a"}', b'{"request_id":"b"}', policy).result == "different"
    policy = replace(policy, ignore_paths=("/request_id",))
    assert (
        pair(b'{"nested":{"request_id":"a"}}', b'{"nested":{"request_id":"b"}}', policy).result
        == "different"
    )


def test_json_pointer_escaping_and_mapping_conflict(policy):
    policy = replace(policy, mappings=(FieldMapping("/a~1b", "/x~0y"),))
    assert pair(b'{"a/b":1}', b'{"x~y":1}', policy).result == "matched"
    assert pair(b'{"a/b":1}', b'{"x~y":1,"a/b":1}', policy).reason == "policy_mapping_conflict"
    assert pair(b'{"a/b":1}', b'{"a/b":1}', policy).reason == "policy_mapping_conflict"


@pytest.mark.parametrize(
    ("left_class", "right_class", "expected", "comparison_class"),
    [
        ("success", "success", "matched", "success"),
        ("expected_rejection", "expected_rejection", "matched", "expected_rejection"),
        ("success", "expected_rejection", "different", "mixed"),
        ("expected_rejection", "success", "different", "mixed"),
        ("unexpected_error", "unexpected_error", "execution_error", "unavailable"),
        ("unknown", "unknown", "not_comparable", "unavailable"),
        ("unknown", "success", "not_comparable", "unavailable"),
    ],
)
def test_contract_classes_cannot_be_inferred_from_equal_statuses(
    policy, left_class, right_class, expected, comparison_class
):
    v1 = replace(response(), contract_class=left_class, status_code=403)
    v2 = replace(response(backend="v2"), contract_class=right_class, status_code=403)
    result = compare(v1, v2, policy, CONTEXT)
    assert (result.result, result.comparison_class) == (expected, comparison_class)


def test_identical_500_is_execution_error(policy):
    v1 = replace(response(), status_code=500, contract_class="unexpected_error")
    v2 = replace(response(backend="v2"), status_code=500, contract_class="unexpected_error")
    assert compare(v1, v2, policy, CONTEXT).result == "execution_error"


@pytest.mark.parametrize("outcome", ["timeout", "cancelled", "transport_error"])
def test_execution_failure_dominates_context_or_capture_limit(policy, outcome):
    v1 = replace(response(), capture_state="oversized")
    v2 = replace(response(backend="v2"), execution_outcome=outcome)
    result = compare(v1, v2, policy, ComparisonContext())
    assert (result.result, result.reason) == ("execution_error", outcome)


def test_not_dispatched_has_first_priority_and_reversed_serving_roles_work(policy):
    v1 = replace(response(), role="shadow", execution_outcome="not_dispatched")
    v2 = replace(response(backend="v2"), role="serving", execution_outcome="timeout")
    assert compare(v1, v2, policy, CONTEXT).result == "not_executed"
    v1 = replace(v1, execution_outcome="http_response")
    v2 = replace(v2, execution_outcome="http_response")
    assert compare(v1, v2, policy, CONTEXT).result == "matched"


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (ComparisonContext(), "logical_request_context_unknown"),
        (ComparisonContext(False, True, True), "logical_request_context_mismatch"),
        (ComparisonContext(True, False, True), "authorization_context_mismatch"),
        (ComparisonContext(True, None, True), "authorization_context_unknown"),
        (ComparisonContext(True, True, False), "data_context_mismatch"),
        (ComparisonContext(True, True, None), "data_context_unknown"),
    ],
)
def test_context_absence_or_mismatch_is_not_a_match(policy, context, reason):
    result = pair(b"{}", b"{}", policy, context)
    assert (result.result, result.reason, result.comparison_class) == (
        "not_comparable",
        reason,
        "unavailable",
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"capture_state": "oversized"}, "capture_oversized"),
        ({"capture_state": "unavailable"}, "capture_unavailable"),
        ({"body": None}, "capture_unavailable"),
        ({"body_complete": False}, "body_incomplete"),
        ({"status_code": None}, "status_unavailable"),
    ],
)
def test_incomplete_capture_cannot_be_compared(policy, changes, reason):
    result = compare(replace(response(), **changes), response(backend="v2"), policy, CONTEXT)
    assert result.reason == reason


def test_required_header_and_status_policies_are_explicit(policy):
    v1 = response(headers=(("X-Version", "one"),))
    v2 = response(backend="v2", headers=(("x-version", "two"),))
    assert compare(v1, v2, policy, CONTEXT).result == "matched"
    policy = replace(policy, required_headers=("x-version",))
    assert compare(v1, v2, policy, CONTEXT).reason == "required_header_mismatch"
    assert (
        compare(response(), response(backend="v2"), policy, CONTEXT).reason
        == "required_header_mismatch"
    )
    policy = replace(policy, required_headers=())
    v2 = replace(v2, status_code=201)
    assert compare(v1, v2, policy, CONTEXT).reason == "status_mismatch"
    policy = replace(policy, status_equivalences=((200, 201),))
    assert compare(v1, v2, policy, CONTEXT).result == "matched"


def test_empty_body_requires_explicit_contract(policy):
    assert pair(b"", b"", policy).result == "not_comparable"
    policy = replace(policy, allow_empty_body=True)
    v1 = replace(response(b""), status_code=204, capture_state="not_needed")
    v2 = replace(response(b"", backend="v2"), status_code=204, capture_state="not_needed")
    assert compare(v1, v2, policy, CONTEXT).result == "matched"


def test_sensitive_values_and_dynamic_object_keys_never_escape_result(policy):
    left = b'{"email@example.test":{"token":"secret-left"},"price":1}'
    right = b'{"email@example.test":{"token":"secret-right"},"price":2}'
    policy = replace(policy, allowed_diff_paths=frozenset({"/price"}))
    result = pair(left, right, policy)
    assert result.difference_paths == ("/price",)
    assert result.redacted_difference_count == 1
    serialized = json.dumps(asdict(result))
    for forbidden in ("email@example.test", "token", "secret-left", "secret-right"):
        assert forbidden not in serialized


def test_differences_count_limit_and_path_limit_are_separate(policy):
    left, right = b'{"a":0,"b":0,"c":0}', b'{"a":1,"b":1,"c":1}'
    policy = replace(
        policy,
        max_differences=2,
        max_diff_paths=1,
        allowed_diff_paths=frozenset({"/a", "/b", "/c"}),
    )
    result = pair(left, right, policy)
    assert result.difference_count == 2
    assert result.difference_count_limited is True
    assert result.difference_paths == ("/a",)
    assert result.difference_paths_truncated is True
    policy = replace(policy, max_differences=3)
    assert pair(left, right, policy).difference_count_limited is False


@pytest.mark.parametrize(
    "changes",
    [
        {"max_json_depth": 129},
        {"max_body_bytes": 0},
        {"max_json_nodes": True},
        {"ignore_paths": ("bad",)},
        {"ignore_paths": ("/x~2",)},
        {"ignore_paths": ("/x", "/x")},
        {"tolerances": (Tolerance("/x", Decimal("NaN")),)},
        {"tolerances": (Tolerance("/x", Decimal("-1")),)},
        {"tolerances": (Tolerance("/x", 0.1),)},
        {"mappings": (FieldMapping("/a", "/a/b"),)},
        {"required_headers": ("x-a", "X-A")},
        {"required_headers": ("x-a\r\nsecret",)},
        {"status_equivalences": ((200, 999),)},
    ],
)
def test_invalid_policy_fails_before_comparison(policy, changes):
    with pytest.raises(ValueError):
        replace(policy, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 1},
        {"revision": True},
        {"revision": []},
        {"revision": " "},
        {"allow_empty_body": "false"},
        {"allow_empty_body": 0},
        {"allow_empty_body": []},
        {"required_headers": "content-type"},
        {"required_headers": [1]},
        {"ignore_paths": "/a"},
        {"allowed_diff_paths": "/a"},
        {"allowed_diff_paths": [["/a"]]},
        {"tolerances": [{"path": "/a", "absolute": "1"}]},
        {"mappings": [{"v1_path": "/a", "v2_path": "/b"}]},
        {"status_equivalences": ["200"]},
    ],
)
def test_invalid_policy_types_are_rejected(policy, changes):
    with pytest.raises(ValueError):
        replace(policy, **changes)


def test_policy_snapshot_does_not_follow_mutation_of_input_collections(policy):
    headers = ["x-version"]
    ignored = ["/id"]
    tolerances = [Tolerance("/price", Decimal("1"))]
    mappings = [FieldMapping("/name", "/label")]
    paths = {"/price"}
    status_pairs = [[200, 201]]
    policy = replace(
        policy,
        required_headers=headers,
        ignore_paths=ignored,
        tolerances=tolerances,
        mappings=mappings,
        allowed_diff_paths=paths,
        status_equivalences=status_pairs,
    )
    headers[0] = "authorization"
    ignored.append("/price")
    tolerances.clear()
    mappings.clear()
    paths.add("/private")
    status_pairs[0][1] = 500
    status_pairs.append([200, 202])
    assert policy.required_headers == ("x-version",)
    assert policy.ignore_paths == ("/id",)
    assert policy.tolerances == (Tolerance("/price", Decimal("1")),)
    assert policy.mappings == (FieldMapping("/name", "/label"),)
    assert policy.allowed_diff_paths == frozenset({"/price"})
    assert policy.status_equivalences == ((200, 201),)
    v1 = response(b'{"id":1,"price":1,"name":"item"}', headers=(("x-version", "1"),))
    v2 = replace(
        response(b'{"id":2,"price":2,"label":"item"}', backend="v2", headers=(("x-version", "1"),)),
        status_code=201,
    )
    assert compare(v1, v2, policy, CONTEXT).result == "matched"


def test_response_body_and_nested_headers_are_immutable_snapshots(policy):
    body = bytearray(b'{"a":1}')
    headers = [["x-version", "1"]]
    v1 = response(body, headers=headers)
    body[-2] = ord("2")
    headers[0][1] = "2"
    headers.append(["x-secret", "sensitive"])
    assert v1.body == b'{"a":1}'
    assert v1.headers == (("x-version", "1"),)
    assert (
        compare(
            v1,
            response(b'{"a":1}', backend="v2", headers=(("x-version", "1"),)),
            replace(policy, required_headers=("x-version",)),
            CONTEXT,
        ).result
        == "matched"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"body_complete": "false"},
        {"body_complete": 1},
        {"body": "{}"},
        {"headers": "invalid"},
        {"headers": [["name"]]},
        {"headers": [["name", 1]]},
    ],
)
def test_response_ambiguous_types_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(response(), **changes)
