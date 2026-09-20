import pytest

from api_migration_proxy.rollout import (
    GATES,
    RETIREMENT_CHECKS,
    ROLLBACK_CHECKS,
    ChangeRecord,
    ControlRatios,
    GateEvidence,
    RetirementEvidence,
    RevisionConflict,
    RevisionTracker,
    RollbackReadiness,
    Stage,
    evaluate_promotion,
)


def passing_gates(epoch="epoch-a"):
    return [GateEvidence(gate, "pass", epoch, (f"measured-{gate}",)) for gate in GATES]


def readiness(*, status="pass", measured=3, target=5, evidence=("actual-rollback-exercise",)):
    return RollbackReadiness(dict.fromkeys(ROLLBACK_CHECKS, status), evidence, measured, target)


def test_promotion_requires_all_gates_in_current_epoch():
    evidence = passing_gates()
    assert evaluate_promotion(
        stage=Stage.SHADOW, epoch="epoch-a", evidence=evidence, comparison_active=True
    ).allowed
    assert not evaluate_promotion(
        stage=Stage.SHADOW, epoch="epoch-a", evidence=evidence[:-1], comparison_active=True
    ).allowed
    assert not evaluate_promotion(
        stage=Stage.SHADOW, epoch="epoch-b", evidence=evidence, comparison_active=True
    ).allowed
    for status in ("fail", "unknown"):
        changed = [GateEvidence("G-01", status, "epoch-a"), *evidence[1:]]
        result = evaluate_promotion(
            stage=Stage.CANARY, epoch="epoch-a", evidence=changed, comparison_active=True
        )
        assert not result.allowed
        assert f"G-01:{status}" in result.blockers


def test_empty_evidence_and_duplicate_gate_cannot_look_ready():
    with pytest.raises(ValueError, match="observed evidence"):
        GateEvidence("G-01", "pass", "epoch-a")
    with pytest.raises(ValueError):
        evaluate_promotion(
            stage=Stage.SHADOW,
            epoch="epoch-a",
            evidence=[*passing_gates(), passing_gates()[0]],
            comparison_active=True,
        )


def test_shadow_stage_cannot_pass_with_comparison_not_running():
    result = evaluate_promotion(
        stage=Stage.SHADOW, epoch="epoch-a", evidence=passing_gates(), comparison_active=False
    )
    assert not result.allowed
    assert "shadow_stage_requires_shadow" in result.blockers


@pytest.mark.parametrize("references", [("",), ("   ",), "a-reference"])
def test_blank_or_wrongly_typed_evidence_cannot_prove_readiness(references):
    with pytest.raises(ValueError):
        readiness(evidence=references)
    with pytest.raises(ValueError):
        RetirementEvidence(dict.fromkeys(RETIREMENT_CHECKS, "pass"), references)
    with pytest.raises(ValueError):
        evaluate_promotion(
            stage=Stage.SHADOW_OFF,
            epoch="epoch-a",
            evidence=passing_gates(),
            comparison_active=False,
            prior_comparison_evidence=references,
            current_serving_evidence=("current",),
        )


def test_planned_comparison_end_needs_exclusion_history_and_current_serving():
    evidence = passing_gates()
    evidence[2] = GateEvidence(
        "G-03",
        "unknown",
        "epoch-a",
        applicable=False,
        exclusion_reason="shadow deliberately stopped",
        alternative_evidence=("last-valid-epoch",),
    )
    for stage in (Stage.SHADOW_OFF, Stage.RETIRED, Stage.V2_PRIMARY):
        result = evaluate_promotion(
            stage=stage, epoch="epoch-a", evidence=evidence, comparison_active=False
        )
        assert not result.allowed
        result = evaluate_promotion(
            stage=stage,
            epoch="epoch-a",
            evidence=evidence,
            comparison_active=False,
            prior_comparison_evidence=("old-epoch-contract",),
            current_serving_evidence=("v2-current-slo",),
        )
        assert result.allowed
        assert result.excluded == ("G-03",)
    assert not evaluate_promotion(
        stage=Stage.SHADOW, epoch="epoch-a", evidence=evidence, comparison_active=True
    ).allowed
    assert not evaluate_promotion(
        stage=Stage.SHADOW_OFF,
        epoch="epoch-a",
        evidence=passing_gates(),
        comparison_active=False,
        prior_comparison_evidence=("old",),
        current_serving_evidence=("current",),
    ).allowed


def test_baseline_does_not_fabricate_comparison_success():
    evidence = passing_gates()
    assert not evaluate_promotion(
        stage=Stage.V1_ONLY, epoch="epoch-a", evidence=evidence, comparison_active=False
    ).allowed
    evidence[2] = GateEvidence(
        "G-03",
        "unknown",
        "epoch-a",
        applicable=False,
        exclusion_reason="comparison not started",
        alternative_evidence=("baseline-metrics-health",),
    )
    assert evaluate_promotion(
        stage=Stage.V1_ONLY, epoch="epoch-a", evidence=evidence, comparison_active=False
    ).allowed
    with pytest.raises(ValueError, match="excluded gate"):
        GateEvidence(
            "G-03",
            "pass",
            "epoch-a",
            ("not-run",),
            applicable=False,
            exclusion_reason="unused",
            alternative_evidence=("baseline",),
        )


def test_worker_partial_apply_and_stale_revision_never_become_full_application():
    tracker = RevisionTracker(workers={"instance-a/worker-1", "instance-a/worker-2"}, revision="r1")
    assert not tracker.status().stable
    tracker.report("instance-a/worker-1", "r1")
    assert tracker.status().missing == ("instance-a/worker-2",)
    tracker.report("instance-a/worker-2", "r1")
    assert tracker.status().stable
    tracker.request_change(previous_revision="r1", next_revision="r2")
    tracker.report("instance-a/worker-1", "r2")
    assert not tracker.status().stable
    assert tracker.status().mismatched == ("instance-a/worker-2",)
    with pytest.raises(RevisionConflict):
        tracker.request_change(previous_revision="r1", next_revision="r3")
    assert tracker.status().desired_revision == "r2"
    tracker.report("instance-a/worker-2", "r2", failed=True)
    assert not tracker.status().stable
    tracker.report("instance-a/worker-2", "r2")
    assert tracker.status().stable
    with pytest.raises(ValueError):
        tracker.report("instance-a", "r2")


def test_shadow_stop_and_serving_rollback_have_distinct_future_request_effects():
    original = ControlRatios(0.75, 0.25)
    stopped = original.stop_shadow()
    assert stopped == ControlRatios(0.75, 0)
    serving_rollback = original.rollback_serving(readiness())
    assert serving_rollback == ControlRatios(0, 0.25)
    assert serving_rollback.stop_shadow() == ControlRatios(0, 0)
    assert original == ControlRatios(0.75, 0.25)


@pytest.mark.parametrize(
    "ready,expected",
    [
        (readiness(status="unknown"), "unknown"),
        (readiness(status="fail"), "fail"),
        (readiness(measured=None), "unknown"),
        (readiness(target=None), "unknown"),
        (readiness(measured=8, target=5), "fail"),
        (readiness(evidence=()), "unknown"),
    ],
)
def test_v1_process_liveness_alone_does_not_prove_rollback_readiness(ready, expected):
    assert ready.status == expected
    with pytest.raises(ValueError, match="readiness"):
        ControlRatios(1, 0).rollback_serving(ready)


def test_incompatible_schema_prevents_ready_even_with_successful_path_check():
    checks = dict.fromkeys(ROLLBACK_CHECKS, "pass")
    checks["schema"] = "fail"
    result = RollbackReadiness(checks, ("actual-observation",), 1, 5)
    assert result.status == "fail"
    checks["schema"] = "pass"
    assert result.status == "fail"


def test_retirement_requires_all_residual_dependencies_and_retention_ownership():
    checks = dict.fromkeys(RETIREMENT_CHECKS, "pass")
    assert RetirementEvidence(checks, ("direct-v2-and-residual-caller-audit",)).status == "pass"
    for name in (
        "unregistered_routes",
        "batch_clients",
        "sessions",
        "direct_v2_contract",
        "retention_ownership",
    ):
        unfinished = {**checks, name: "unknown"}
        assert RetirementEvidence(unfinished, ("audit",)).status == "unknown"
        failed = {**checks, name: "fail"}
        assert RetirementEvidence(failed, ("audit",)).status == "fail"
    assert RetirementEvidence(checks, ()).status == "unknown"
    with pytest.raises(ValueError):
        RetirementEvidence({"v2_business_cycle": "pass"}, ("audit",))


def test_change_record_is_application_evidence_not_just_an_intended_revision():
    fields = dict(
        change_id="change-1",
        route_or_group="catalog",
        owner="operator",
        reason="measured-canary",
        previous_revision="r1",
        next_revision="r2",
        previous_stage=Stage.SHADOW,
        next_stage=Stage.CANARY,
        ratios=ControlRatios(0.1, 0.5),
        epoch="epoch-b",
        requested_at="2026-09-19T01:00:00Z",
        rollback_revision="r1",
        gate_evidence=tuple(passing_gates()),
    )
    record = ChangeRecord(**fields)
    assert record.result == "requested"
    assert record.applied_workers == ()
    with pytest.raises(ValueError, match="actual time"):
        ChangeRecord(**fields, result="applied")
    completed = ChangeRecord(
        **fields, result="applied", applied_at="2026-09-19T01:00:05Z", applied_workers=("i1/w1",)
    )
    assert completed.result == "applied"
    assert completed.gate_evidence[0].epoch == "epoch-a"


@pytest.mark.parametrize(
    "v2,shadow", [(float("nan"), 0), (0, float("inf")), (-0.1, 0), (0, 1.1), (True, 0)]
)
def test_ratio_controls_reject_invalid_configuration(v2, shadow):
    with pytest.raises(ValueError):
        ControlRatios(v2, shadow)
