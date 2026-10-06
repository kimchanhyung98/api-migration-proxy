import copy

import pytest

from scenarios.observation import evaluate_observations


def observation(counts=None, shadow=True):
    counts = counts or {"v1": 2, "v2": 1}
    return {
        "health": {"ready": True},
        "configuration_revision": "scenario-step-1",
        "backends": {
            "before": {version: {"version": version, "items": 10} for version in counts},
            "after": {
                version: {"version": version, "items": 10 + (sum(counts.values()) if shadow else n)}
                for version, n in counts.items()
            },
        },
        "shutdown": {
            "status": "exited",
            "exit_code": 0,
            "oom_killed": False,
            "application_shutdown_complete": True,
        },
    }


def events(counts=None, shadow=True):
    counts = counts or {"v1": 2, "v2": 1}
    rows = []
    for backend, amount in counts.items():
        for _ in range(amount if shadow else 0):
            rows.append(
                {
                    "event_id": f"event-{len(rows)}",
                    "summary": {
                        "configuration_revision": "scenario-step-1",
                        "route_id": "synthetic_item",
                        "serving_backend": backend,
                        "response_source": "backend",
                        "request_outcome": "completed",
                        "shadow_selected": True,
                        "shadow_dispatched": True,
                        "detail_state": "disabled",
                        "detail_sampled": False,
                        "comparison": {
                            "result": "not_comparable",
                            "reason": "authorization_context_unknown",
                            "comparison_class": "unavailable",
                        },
                        "backends": {
                            target: {
                                "backend": target,
                                "role": "serving" if target == backend else "shadow",
                                "execution_outcome": "http_response",
                                "contract_class": "success",
                                "status_code": 200,
                                "response_complete": True,
                            }
                            for target in ("v1", "v2")
                        },
                    },
                }
            )
    return rows


def evaluate(current=None, stored=None, *, counts=None, shadow=True, previous=()):
    return evaluate_observations(
        current if current is not None else observation(counts, shadow),
        stored if stored is not None else events(counts, shadow),
        revision="scenario-step-1",
        backend_counts=counts or {"v1": 2, "v2": 1},
        shadow_enabled=shadow,
        previous_events=previous,
    )


@pytest.mark.parametrize("counts", [{"v1": 3, "v2": 0}, {"v1": 2, "v2": 1}, {"v1": 0, "v2": 3}])
@pytest.mark.parametrize("shadow", [True, False])
def test_actual_backend_calls_and_opposite_shadow_event_roles_pass(counts, shadow):
    result = evaluate(counts=counts, shadow=shadow)
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["observed"]["event_count"] == (3 if shadow else 0)
    assert result["observed"]["event_serving_counts"] == (counts if shadow else {"v1": 0, "v2": 0})
    assert result["observed"]["backend_requests"] == ({"v1": 3, "v2": 3} if shadow else counts)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("health", "ready"), False),
        (("health", "ready"), 1),
        (("configuration_revision",), "previous-revision"),
        (("shutdown", "status"), "running"),
        (("shutdown", "exit_code"), 137),
        (("shutdown", "exit_code"), False),
        (("shutdown", "oom_killed"), True),
        (("shutdown", "oom_killed"), 0),
        (("shutdown", "application_shutdown_complete"), False),
        (("backends", "before", "v1", "version"), "v2"),
        (("backends", "after", "v2", "version"), "v1"),
        (("backends", "before", "v1", "items"), -1),
        (("backends", "after", "v1", "items"), 12),
        (("backends", "after", "v2", "items"), 14),
        (("backends", "after", "v2", "items"), 2),
        (("backends", "after", "v2", "items"), "13"),
        (("backends", "after", "v2", "items"), True),
    ],
)
def test_incomplete_health_shutdown_or_backend_observations_fail(path, value):
    current = observation()
    target = current
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert evaluate(current)["passed"] is False


@pytest.mark.parametrize("field", ["health", "shutdown", "backends"])
@pytest.mark.parametrize("value", [None, [], "missing"])
def test_missing_or_malformed_observation_groups_fail(field, value):
    assert evaluate(observation() | {field: value})["passed"] is False


def test_sigterm_exit_requires_successful_application_shutdown():
    current = observation()
    current["shutdown"]["exit_code"] = 143
    assert evaluate(current)["passed"] is True
    current["shutdown"]["application_shutdown_complete"] = False
    assert evaluate(current)["passed"] is False


def test_shadow_calls_while_off_and_missing_shadow_calls_while_on_fail():
    assert evaluate(observation(shadow=True), [], shadow=False)["passed"] is False
    assert evaluate(observation(shadow=False))["passed"] is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("configuration_revision",), "previous-revision"),
        (("route_id",), "other_route"),
        (("serving_backend",), "v2"),
        (("shadow_selected",), False),
        (("shadow_dispatched",), 1),
        (("request_outcome",), "incomplete"),
        (("detail_state",), "stored"),
        (("detail_sampled",), True),
        (("comparison", "result"), "matched"),
        (("comparison", "reason"), "context_missing"),
        (("comparison", "comparison_class"), "success"),
        (("backends", "v1", "role"), "shadow"),
        (("backends", "v2", "status_code"), 500),
        (("backends", "v2", "execution_outcome"), "timeout"),
        (("backends", "v1", "contract_class"), "unknown"),
        (("backends", "v1", "response_complete"), False),
    ],
)
def test_events_must_match_revision_roles_completion_and_comparison(path, value):
    stored = events()
    target = stored[0]["summary"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert evaluate(stored=stored)["passed"] is False


def test_missing_duplicate_or_malformed_events_fail():
    stored = events()
    assert evaluate(stored=stored[:-1])["passed"] is False
    duplicate = copy.deepcopy(stored)
    duplicate[1]["event_id"] = duplicate[0]["event_id"]
    assert evaluate(stored=duplicate)["passed"] is False
    assert evaluate(stored=[None, {}, {"event_id": "x", "summary": None}])["passed"] is False


def previous_events():
    stored = events({"v1": 1, "v2": 0})
    stored[0]["event_id"] = "previous-event"
    stored[0]["summary"]["configuration_revision"] = "previous-revision"
    return stored


@pytest.mark.parametrize("shadow", [True, False])
def test_previous_events_remain_identical_across_proxy_restarts(shadow):
    before = previous_events()
    result = evaluate(stored=before + events(shadow=shadow), shadow=shadow, previous=before)
    assert result["passed"] is True
    assert result["observed"]["retained_event_count"] == 1


@pytest.mark.parametrize("mutation", ["missing", "changed", "unexpected"])
def test_lost_changed_or_unexpected_stored_events_fail(mutation):
    before = previous_events()
    after = copy.deepcopy(before)
    if mutation == "missing":
        after.clear()
    elif mutation == "changed":
        after[0]["summary"]["request_outcome"] = "changed"
    else:
        extra = copy.deepcopy(after[0])
        extra["event_id"] = "unexpected-event"
        after.append(extra)
    assert evaluate(stored=after + events(), previous=before)["passed"] is False
