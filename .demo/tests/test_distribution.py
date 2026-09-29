import pytest

from tests.distribution import evaluate


def report(v1=500, v2=500, unknown=0):
    responses = v1 + v2 + unknown
    return {
        "attempts": responses,
        "responses": responses,
        "transport_errors": 0,
        "status_counts": {"200": responses},
        "backend_versions": {
            "denominator": "responses",
            "counts": {"v1": v1, "v2": v2, "unknown": unknown},
        },
    }


def test_observed_serving_share_within_tolerance_passes():
    result = evaluate(report(493, 507), requests=1000, v2_ratio=0.5, tolerance=0.08)

    assert result["passed"] is True
    assert result["failures"] == []
    assert result["observed"]["v2_ratio"] == 0.507
    assert result["expected"]["v2_count_min"] == 420
    assert result["expected"]["v2_count_max"] == 580


@pytest.mark.parametrize(
    ("ratio", "v2", "passed"),
    [
        (0.5, 419, False),
        (0.5, 420, True),
        (0.5, 580, True),
        (0.5, 581, False),
        (0, 0, True),
        (0, 1, False),
        (1, 1000, True),
        (1, 999, False),
    ],
)
def test_inclusive_count_boundaries_and_exact_full_switch(ratio, v2, passed):
    result = evaluate(report(1000 - v2, v2), requests=1000, v2_ratio=ratio, tolerance=0.08)
    assert result["passed"] is passed
    assert bool(result["failures"]) is not passed


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": 999},
        {"responses": 999},
        {"transport_errors": 1},
        {"status_counts": {"200": 999, "500": 1}},
        {"status_counts": {"200": 999, "302": 1}},
        {
            "backend_versions": {
                "denominator": "responses",
                "counts": {"v1": 499, "v2": 500, "unknown": 1},
            }
        },
    ],
)
def test_errors_or_incomplete_samples_fail_even_when_share_looks_correct(changes):
    result = evaluate(report() | changes, requests=1000, v2_ratio=0.5, tolerance=0.08)
    assert result["passed"] is False
    assert result["failures"]


def test_zero_responses_fail_without_division_by_zero():
    result = evaluate(
        report(0, 0) | {"attempts": 1000, "transport_errors": 1000},
        requests=1000,
        v2_ratio=0.5,
        tolerance=0.08,
    )
    assert result["passed"] is False
    assert result["observed"]["v2_ratio"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"requests": 0},
        {"requests": True},
        {"requests": 1.5},
        {"v2_ratio": -0.1},
        {"v2_ratio": 1.1},
        {"v2_ratio": float("nan")},
        {"v2_ratio": True},
        {"tolerance": -0.01},
        {"tolerance": 0.5},
        {"tolerance": float("inf")},
        {"tolerance": True},
    ],
)
def test_invalid_acceptance_parameters_are_rejected(changes):
    with pytest.raises(ValueError):
        evaluate(report(), **({"requests": 1000, "v2_ratio": 0.5, "tolerance": 0.08} | changes))


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        report() | {"responses": "1000"},
        report() | {"transport_errors": False},
        report() | {"backend_versions": None},
        report() | {"status_counts": {"200": 1000.0}},
        report()
        | {
            "backend_versions": {
                "denominator": "attempts",
                "counts": {"v1": 500, "v2": 500, "unknown": 0},
            }
        },
        report()
        | {
            "backend_versions": {
                "denominator": "responses",
                "counts": {"v1": 500.0, "v2": 500, "unknown": 0},
            }
        },
        report()
        | {
            "backend_versions": {
                "denominator": "responses",
                "counts": {"v1": -1, "v2": 1001, "unknown": 0},
            }
        },
    ],
)
def test_malformed_reports_cannot_be_accepted(invalid):
    with pytest.raises(ValueError, match="report"):
        evaluate(invalid, requests=1000, v2_ratio=0.5, tolerance=0.08)


def test_observed_ratio_is_calculated_from_counts_instead_of_reported_ratio():
    sample = report(700, 300)
    sample["backend_versions"]["ratios"] = {"v1": 0.5, "v2": 0.5, "unknown": 0}
    result = evaluate(sample, requests=1000, v2_ratio=0.5, tolerance=0.08)
    assert result["passed"] is False
    assert result["observed"]["v2_ratio"] == 0.3
