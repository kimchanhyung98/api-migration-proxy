import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from api_migration_proxy.config import (
    Cohort,
    ConfigManager,
    ConfigurationError,
    ResponseContract,
    RevisionConflict,
    Route,
    RuntimeBudgets,
    ShadowPolicy,
    Snapshot,
    read_config,
    snapshot_from_dict,
)
from api_migration_proxy.routing import backend_url, choose_serving, cohort_bucket, match_route


@pytest.fixture
def route():
    return Route(
        route_id="catalog_detail",
        method="GET",
        path_template="/catalog/{id}",
        v1="http://localhost:8101",
        v2="http://localhost:8102",
        owner="catalog",
        rollout_enabled=True,
        v2_serve_ratio=0.25,
        cohort=Cohort("user", "catalog", "authenticated_user", "fixture-salt"),
        shadow=ShadowPolicy(True, 1.0, "fixture-effect-auth-data-contract"),
        contract=ResponseContract(frozenset({200, 204}), frozenset({400, 403})),
        comparison_policy_revision="comparison-fixture-1",
    )


@pytest.fixture
def snapshot(route):
    return Snapshot(
        schema_version=1,
        revision="fixture-1",
        previous_revision=None,
        change_reason="synthetic test",
        default_v1=route.v1,
        allowed_backends=frozenset({route.v1, route.v2}),
        routes=(route,),
        budgets=RuntimeBudgets(2.0, 0.5, 2.0, 3.0, 10, 2, 1024, 4096),
    )


def test_route_matching_and_unregistered_default(snapshot):
    match = match_route(snapshot, "GET", "/catalog/item-1")
    assert match.route.route_id == "catalog_detail"
    assert match.parameters == {"id": "item-1"}
    assert match_route(snapshot, "POST", "/catalog/item-1") is None
    assert match_route(snapshot, "GET", "/catalog/") is None
    assert match_route(snapshot, "GET", "/catalog/item-1/other") is None
    assert (
        backend_url(snapshot, None, "v1", b"/legacy/%2F", b"q=1&q=2")
        == "http://localhost:8101/legacy/%2F?q=1&q=2"
    )
    with pytest.raises(ValueError):
        backend_url(snapshot, None, "v2", b"/catalog/item-1")


def test_raw_path_and_query_preserved_and_explicit_mapping(snapshot, route):
    match = match_route(snapshot, "GET", "/catalog/one two")
    assert (
        backend_url(snapshot, match, "v1", b"/catalog/one%20two", b"a=1&a=2&x=%2B")
        == "http://localhost:8101/catalog/one%20two?a=1&a=2&x=%2B"
    )
    route = replace(route, v2_path_template="/api/v2/items/{id}")
    mapped_snapshot = replace(snapshot, routes=(route,))
    match = match_route(mapped_snapshot, "GET", "/catalog/one two")
    assert (
        backend_url(mapped_snapshot, match, "v2", b"/catalog/one%20two")
        == "http://localhost:8102/api/v2/items/one%20two"
    )
    encoded = match_route(mapped_snapshot, "GET", "/catalog/..")
    assert (
        backend_url(mapped_snapshot, encoded, "v2", b"/catalog/%2E%2E")
        == "http://localhost:8102/api/v2/items/%2E%2E"
    )


@pytest.mark.parametrize(
    "template",
    ["/catalog/*", "/catalog/{", "/catalog/{id}/{id}", "//evil/{id}", "/catalog/../{id}"],
)
def test_unsupported_templates_rejected(route, template):
    with pytest.raises(ConfigurationError):
        replace(route, path_template=template)


def test_mapping_cannot_drop_or_invent_parameters(route):
    with pytest.raises(ConfigurationError):
        replace(route, v2_path_template="/api/{other}")
    with pytest.raises(ConfigurationError):
        replace(route, v2_path_template="/api/all")


@pytest.mark.parametrize("template", ["/catalog/{item}", "/catalog/search"])
def test_ambiguous_templates_rejected(snapshot, route, template):
    other = replace(route, route_id="other", path_template=template)
    with pytest.raises(ConfigurationError, match="ambiguous"):
        replace(snapshot, routes=(route, other))


def test_disjoint_methods_and_templates_allowed(snapshot, route):
    post = replace(route, route_id="post-search", method="POST", path_template="/catalog/search")
    listing = replace(route, route_id="catalog_list", path_template="/catalog")
    assert len(replace(snapshot, routes=(route, post, listing)).routes) == 3


@pytest.mark.parametrize(
    "backend",
    [
        "https://person:secret@example.invalid",
        "ftp://example.invalid",
        "https://example.invalid/path",
        "https://example.invalid?backend=other",
        "https://example.invalid#fragment",
        "https://example.invalid\\@other.invalid",
        "https://example.invalid\n",
        "https://example.invalid:99999",
        "https://example.invalid%2f.other",
    ],
)
def test_invalid_backend_rejected(route, backend):
    with pytest.raises(ConfigurationError):
        replace(route, v2=backend)


def test_fixed_operator_allowlist_required(snapshot, route):
    with pytest.raises(ConfigurationError, match="allowlist"):
        replace(snapshot, routes=(replace(route, v2="https://unregistered.example.invalid"),))


@pytest.mark.parametrize("ratio", [float("nan"), float("inf"), -0.01, 1.01, True, "0.2", None])
def test_invalid_ratios_rejected(route, ratio):
    with pytest.raises(ConfigurationError):
        replace(route, v2_serve_ratio=ratio)
    with pytest.raises(ConfigurationError):
        ShadowPolicy(False, ratio)


def test_limits_and_eligibility_review_are_required(snapshot, route):
    for value in (None, 0, -1, float("nan"), True):
        with pytest.raises(ConfigurationError):
            replace(snapshot.budgets, serving_timeout_seconds=value)
    with pytest.raises(ConfigurationError):
        replace(snapshot.budgets, serving_max_inflight=1.5)
    with pytest.raises(ConfigurationError):
        ShadowPolicy(True, 0)
    with pytest.raises(ConfigurationError):
        replace(route, comparison_policy_revision=None)


def test_group_policy_mismatch_rejected(snapshot, route):
    sibling = replace(route, route_id="catalog_list", path_template="/catalog")
    for changed in (
        replace(sibling, rollout_enabled=False),
        replace(sibling, v2_serve_ratio=0.5),
        replace(sibling, cohort=replace(sibling.cohort, salt="other-salt")),
        replace(sibling, cohort=replace(sibling.cohort, mode="tenant")),
        replace(sibling, cohort=replace(sibling.cohort, key_source="other-user")),
    ):
        with pytest.raises(ConfigurationError, match="cohort group"):
            replace(snapshot, routes=(route, changed))


def test_cohorts_repeat_across_routes_and_ratio_increases(route):
    sibling = replace(route, route_id="catalog_list", path_template="/catalog")
    expanded = replace(route, v2_serve_ratio=0.8)
    original_v2 = 0
    for index in range(1000):
        identity = {"authenticated_user": f"user-{index}"}
        first = choose_serving(route, trusted_identity=identity)
        assert first == choose_serving(sibling, trusted_identity=identity)
        assert 0 <= first.bucket < 1
        if first.backend == "v2":
            original_v2 += 1
            assert choose_serving(expanded, trusted_identity=identity).backend == "v2"
    assert 180 < original_v2 < 320


def test_hash_serialization_has_unambiguous_boundaries(route):
    first = cohort_bucket(replace(route.cohort, group="a"), "bc")
    second = cohort_bucket(replace(route.cohort, group="ab"), "c")
    assert first != second
    assert first == cohort_bucket(replace(route.cohort, group="a"), "bc")
    with pytest.raises(ConfigurationError):
        replace(route.cohort, algorithm="python-hash")


def test_serving_boundaries_and_missing_trusted_keys(route):
    identity = {"authenticated_user": "user-1"}
    assert (
        choose_serving(replace(route, v2_serve_ratio=0), trusted_identity=identity).backend == "v1"
    )
    assert (
        choose_serving(replace(route, v2_serve_ratio=1), trusted_identity=identity).backend == "v2"
    )
    missing = choose_serving(
        replace(route, v2_serve_ratio=1), trusted_identity={"X-User": "untrusted"}
    )
    assert (missing.backend, missing.reason) == ("v1", "missing_cohort_key")
    assert (
        choose_serving(
            replace(route, rollout_enabled=False, v2_serve_ratio=1), trusted_identity=identity
        ).backend
        == "v1"
    )
    request_route = replace(
        route, cohort=Cohort("request", "stateless", "request", "fixture-salt"), v2_serve_ratio=1
    )
    assert choose_serving(request_route, request_key="independent-request").backend == "v2"
    assert choose_serving(request_route).reason == "missing_cohort_key"


def test_snapshot_and_nested_values_are_immutable(snapshot, route):
    routes = [route]
    backends = [route.v1, route.v2]
    frozen = replace(snapshot, routes=routes, allowed_backends=backends)
    routes.clear()
    backends.clear()
    assert len(frozen.routes) == 1
    assert len(frozen.allowed_backends) == 2
    with pytest.raises(FrozenInstanceError):
        frozen.routes[0].owner = "other"
    match = match_route(snapshot, "GET", "/catalog/first")
    with pytest.raises(TypeError):
        match.parameters["id"] = "second"
    with pytest.raises(ConfigurationError):
        replace(route, rollout_enabled=False, comparison_policy_revision=[])
    with pytest.raises(ConfigurationError):
        ShadowPolicy(False, 0, review_ref=[])


def test_compare_and_swap_preserves_inflight_snapshot_and_last_valid(snapshot):
    manager = ConfigManager()
    assert not manager.readiness
    manager.apply(snapshot, actor="operator", expected_revision=None)
    inflight = manager.current
    updated = replace(snapshot, revision="fixture-2", previous_revision="fixture-1")
    manager.apply(updated, actor="operator", expected_revision="fixture-1")
    assert inflight.revision == "fixture-1"
    assert manager.current.revision == "fixture-2"
    with pytest.raises(RevisionConflict):
        manager.apply(
            replace(snapshot, revision="fixture-3"), actor="other", expected_revision="fixture-1"
        )
    assert manager.current is updated
    assert manager.worker_status == {"local": "fixture-2"}
    assert manager.history[-1].actor == "operator"
    assert manager.history[-1].previous_revision == "fixture-1"
    with pytest.raises(RevisionConflict):
        manager.apply(
            replace(snapshot, previous_revision="fixture-2"),
            actor="operator",
            expected_revision="fixture-2",
        )
    assert manager.current is updated


def test_response_contract_does_not_treat_every_4xx_as_expected(route):
    contract = route.contract
    assert contract.classify(200) == "success"
    assert contract.classify(403) == "expected_rejection"
    assert contract.classify(401) == "unknown"
    assert contract.classify(500) == "unexpected_error"
    assert contract.classify(200, body_complete=False) == "unknown"
    assert contract.classify(None) == "unknown"
    with pytest.raises(ConfigurationError):
        ResponseContract({200}, {200})
    with pytest.raises(ConfigurationError):
        ResponseContract({500})


def test_full_change_history_rejects_new_apply_without_losing_current_configuration(snapshot):
    manager = ConfigManager(snapshot, history_limit=1)
    new = replace(snapshot, revision="fixture-2", previous_revision=snapshot.revision)
    with pytest.raises(ConfigurationError, match="history capacity"):
        manager.apply(new, actor="operator", expected_revision=snapshot.revision)
    assert manager.current is snapshot
    assert len(manager.history) == 1


def test_json_config_loads_nested_validation_and_hides_input_on_failure(snapshot, tmp_path):
    value = asdict(snapshot)
    value["allowed_backends"] = list(value["allowed_backends"])
    for raw_route in value["routes"]:
        for key in raw_route["contract"]:
            raw_route["contract"][key] = list(raw_route["contract"][key])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    assert read_config(path) == snapshot
    value["routes"][0]["shadow"]["eligible"] = "yes"
    with pytest.raises(ConfigurationError):
        snapshot_from_dict(value)
    path.write_text('{"credential":"sensitive-invalid-json"')
    with pytest.raises(ConfigurationError) as error:
        read_config(path)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "document", ['{"revision":"first","revision":"second"}', '{"ratio":NaN}', "[]"]
)
def test_json_config_rejects_ambiguous_or_nonstandard_json(tmp_path, document):
    path = tmp_path / "config.json"
    path.write_text(document)
    with pytest.raises(ConfigurationError):
        read_config(path)
