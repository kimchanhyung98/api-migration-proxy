import json
import threading

import pytest

from api_migration_proxy.observability import (
    CohortWindow,
    Metrics,
    PipelineObservation,
    WorkerMetrics,
    merge_workers,
    ratio,
)


def test_request_cohort_keeps_late_work_in_original_epoch_and_deduplicates_ack():
    metrics = Metrics({"catalog"})
    window = CohortWindow("catalog", "epoch-a", 100, 200)
    observation = window.begin(metrics, route_id="catalog", epoch="epoch-a", started_at=199)
    observation.eligible()
    observation.selected()
    observation.dispatched()
    with pytest.raises(ValueError, match="unsettled"):
        window.close(250)
    observation.terminal()
    observation.compared("different", "success", "json_value_mismatch")
    assert observation.stored()
    assert not observation.stored()
    assert not observation.terminal()
    assert not observation.compared("different", "success", "json_value_mismatch")
    observation.finish()
    observation.finish()
    window.close(300)
    report = window.report()
    assert report["counts"] == dict(E=1, S=1, D=1, T=1, C=1, M=0, X=1, W=1)
    assert report["state"] == "closed"
    assert report["epoch"] == "epoch-a"
    assert metrics.value("comparison_pipeline_total", route="catalog", step="stored") == 1
    assert report["ratios"]["difference"].value == 1
    with pytest.raises(ValueError):
        window.begin(metrics, route_id="catalog", epoch="epoch-a", started_at=199)


def test_concurrent_cohort_report_preserves_comparable_equals_matches_plus_differences(monkeypatch):
    metrics = Metrics({"catalog"})
    window = CohortWindow("catalog", "epoch-a", 0, 10)
    observation = window.begin(metrics, route_id="catalog", epoch="epoch-a", started_at=1)
    observation.eligible()
    observation.selected()
    observation.dispatched()
    observation.terminal()
    advanced, release, reader_started, reported = (threading.Event() for _ in range(4))
    original = observation._advance
    reports = []

    def paused_advance(symbol, prerequisite=None):
        value = original(symbol, prerequisite)
        if symbol == "C":
            advanced.set()
            assert release.wait(2)
        return value

    def read_report():
        reader_started.set()
        reports.append(window.report())
        reported.set()

    monkeypatch.setattr(observation, "_advance", paused_advance)
    writer = threading.Thread(target=observation.compared, args=("different", "success"))
    reader = threading.Thread(target=read_report)
    writer.start()
    try:
        assert advanced.wait(2)
        reader.start()
        assert reader_started.wait(2)
        assert not reported.wait(0.03)
    finally:
        release.set()
        writer.join(2)
        reader.join(2)
    assert reported.is_set()
    assert reports[0]["counts"]["C"] == reports[0]["counts"]["M"] + reports[0]["counts"]["X"] == 1
    assert reports[0]["ratios"]["difference"].value == 1


def test_selected_without_dispatch_can_store_but_does_not_become_comparable():
    metrics = Metrics({"catalog"})
    window = CohortWindow("catalog", "epoch-a", 0, 10)
    observation = window.begin(metrics, route_id="catalog", epoch="epoch-a", started_at=1)
    observation.eligible()
    observation.selected()
    observation.compared("not_executed", "unavailable", "slot_exhausted")
    observation.stored()
    observation.finish()
    window.close(10)
    assert window.report()["counts"] == dict(E=1, S=1, D=0, T=0, C=0, M=0, X=0, W=1)
    assert window.report()["ratios"]["difference"].state == "no_data"


def test_shadow_early_timeout_does_not_allow_terminal_comparison_before_serving_finishes():
    observation = PipelineObservation(Metrics({"catalog"}), "catalog")
    observation.eligible()
    observation.selected()
    observation.dispatched()
    with pytest.raises(ValueError, match="both backends"):
        observation.compared("execution_error", "unavailable", "timeout")
    with pytest.raises(ValueError, match="terminal"):
        observation.finish(dropped=True)
    observation.terminal()
    observation.compared("execution_error", "unavailable", "timeout")
    observation.finish(dropped=True)


@pytest.mark.parametrize(
    "result,comparison_class",
    [
        ("matched", "mixed"),
        ("matched", "unavailable"),
        ("not_comparable", "success"),
        ("execution_error", "expected_rejection"),
    ],
)
def test_invalid_result_classes_never_increment_comparable(result, comparison_class):
    metrics = Metrics({"catalog"})
    observation = PipelineObservation(metrics, "catalog")
    observation.eligible()
    observation.selected()
    observation.dispatched()
    observation.terminal()
    with pytest.raises(ValueError, match="class"):
        observation.compared(result, comparison_class)
    assert metrics.value("comparison_pipeline_total", route="catalog", step="comparable") == 0


def test_pipeline_rejects_invalid_order_and_conflicting_final_result():
    observation = PipelineObservation(Metrics({"catalog"}), "catalog")
    with pytest.raises(ValueError):
        observation.selected()
    with pytest.raises(ValueError):
        observation.stored()
    observation.eligible()
    observation.selected()
    observation.dispatched()
    with pytest.raises(ValueError):
        observation.compared("not_executed")
    observation.terminal()
    observation.compared("matched", "expected_rejection")
    with pytest.raises(ValueError):
        observation.compared("different", "expected_rejection")
    observation.stored()
    observation.finish()


def test_zero_unknown_provisional_and_planned_end_are_distinct():
    assert ratio(0, 0).state == "no_data"
    assert ratio(0, 0, planned_end=True).state == "ended"
    assert ratio(0, 0, complete=False).state == "unknown"
    assert ratio(None, None).state == "unknown"
    assert ratio(0, 1).value == 0
    metrics = Metrics({"catalog"})
    window = CohortWindow("catalog", "epoch-stop", 0, 10, planned_end=True)
    observation = window.begin(metrics, route_id="catalog", epoch="epoch-stop", started_at=1)
    observation.eligible()
    observation.finish()
    assert window.report()["state"] == "provisional"
    window.close(10)
    report = window.report()
    assert report["counts"]["E"] == 1
    assert report["counts"]["S"] == 0
    assert report["ratios"]["selection"].value == 0
    assert report["ratios"]["storage"].state == "ended"
    assert report["comparison_state"] == "ended"
    window.mark_unknown()
    assert window.report()["state"] == "unknown"
    assert window.report()["ratios"]["selection"].state == "unknown"


def test_ack_unknown_is_not_confirmed_drop_or_zero_observation():
    metrics = Metrics({"catalog"})
    window = CohortWindow("catalog", "epoch-a", 0, 10)
    observation = window.begin(metrics, route_id="catalog", epoch="epoch-a", started_at=1)
    observation.eligible()
    observation.selected()
    observation.compared("not_executed", "unavailable", "shutdown")
    with pytest.raises(ValueError, match="ACK"):
        observation.finish()
    observation.finish(acknowledgement_unknown=True)
    window.close(10)
    report = window.report()
    assert report["counts"]["W"] == 0
    assert report["ratios"]["storage"].state == "unknown"


@pytest.mark.parametrize(
    "route,epoch,started",
    [("other", "epoch-a", 1), ("catalog", "epoch-b", 1), ("catalog", "epoch-a", 10)],
)
def test_mixed_epochs_routes_and_end_boundary_are_not_one_cohort(route, epoch, started):
    metrics = Metrics({"catalog", "other"})
    window = CohortWindow("catalog", "epoch-a", 0, 10)
    with pytest.raises(ValueError):
        window.begin(metrics, route_id=route, epoch=epoch, started_at=started)
    assert window.pending == 0


def test_user_completion_and_backend_completion_use_independent_counts_and_durations():
    metrics = Metrics({"catalog"})
    metrics.increment("proxy_assignments_total", route="catalog", serving="v2")
    metrics.increment("backend_started_total", route="catalog", backend="v2", role="serving")
    metrics.increment(
        "backend_completed_total",
        route="catalog",
        backend="v2",
        role="serving",
        outcome="http_response",
        contract_class="success",
    )
    metrics.observe("backend_duration_seconds", 0.01, route="catalog", backend="v2", role="serving")
    assert (
        metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="v2",
            outcome="completed",
            source="backend",
        )
        == 0
    )
    metrics.increment(
        "proxy_requests_total", route="catalog", serving="v2", outcome="cancelled", source="none"
    )
    metrics.observe("proxy_request_duration_seconds", 0.8, route="catalog", serving="v2")
    assert metrics.value("proxy_assignments_total", route="catalog", serving="v2") == 1
    assert (
        metrics.value(
            "backend_duration_seconds", route="catalog", backend="v2", role="serving"
        ).total
        == 0.01
    )
    assert (
        metrics.value("proxy_request_duration_seconds", route="catalog", serving="v2").total == 0.8
    )


@pytest.mark.parametrize(
    "labels",
    [
        {"route": "/catalog/secret?token=value", "serving": "v1"},
        {"route": "catalog", "serving": "v1", "revision": "build-123"},
        {"route": "catalog", "serving": "v1", "user_id": "person"},
        {"route": "catalog", "serving": "unknown"},
    ],
)
def test_export_labels_reject_raw_identifiers_and_unbounded_dimensions(labels):
    metrics = Metrics({"catalog"})
    with pytest.raises(ValueError):
        metrics.increment("proxy_assignments_total", **labels)
    with pytest.raises(ValueError):
        metrics.increment("collection_dropped_total", reason="exception: credential")
    metrics.increment("proxy_assignments_total", route="catalog", serving="v1")
    exported = json.dumps(metrics.export())
    assert "token" not in exported
    assert "user_id" not in exported


def test_registered_config_route_identifier_with_colon_is_supported():
    metrics = Metrics({"catalog:detail"})
    metrics.increment("proxy_assignments_total", route="catalog:detail", serving="v1")
    assert metrics.value("proxy_assignments_total", route="catalog:detail", serving="v1") == 1


def test_gauges_and_histograms_merge_by_worker_with_completeness():
    left, right = Metrics({"catalog"}), Metrics({"catalog"})
    for metrics, depth, age, duration in ((left, 2, 3, 0.1), (right, 5, 7, 0.5)):
        metrics.set_gauge("collection_queue_depth", depth)
        metrics.set_gauge("collection_oldest_age_seconds", age)
        metrics.adjust_gauge("backend_inflight", 1, backend="v2", role="shadow")
        metrics.increment("proxy_assignments_total", route="catalog", serving="v1")
        metrics.observe("proxy_request_duration_seconds", duration, route="catalog", serving="v1")
    first = WorkerMetrics("instance-a/worker-1", "run-1", left.snapshot())
    second = WorkerMetrics("instance-a/worker-2", "run-1", right.snapshot())
    expected = {first.worker_id, second.worker_id}
    result, complete = merge_workers([first, second], expected_workers=expected)
    assert complete
    assert result.gauges[("collection_queue_depth", ())] == 7
    assert result.gauges[("collection_oldest_age_seconds", ())] == 7
    assert sum(result.counters.values()) == 2
    histogram = next(iter(result.histograms.values()))
    assert histogram.count == 2
    assert histogram.total == 0.6
    assert histogram.buckets[4] == 1
    dead = WorkerMetrics(second.worker_id, second.generation, second.snapshot, alive=False)
    after_exit, complete = merge_workers([first, dead], expected_workers=expected)
    assert not complete
    assert after_exit.gauges[("collection_queue_depth", ())] == 2
    assert not merge_workers([first], expected_workers=expected)[1]
    assert not merge_workers(
        [first, second],
        expected_workers=expected,
        previous_generations={first.worker_id: "old-run"},
    )[1]
    with pytest.raises(ValueError, match="duplicate"):
        merge_workers([first, first], expected_workers=expected)


def test_histogram_incompatible_buckets_and_gauge_underflow_are_rejected():
    left, right = Metrics({"catalog"}), Metrics({"catalog"}, histogram_bounds=(1, 2))
    for metrics in (left, right):
        metrics.observe("proxy_request_duration_seconds", 0.1, route="catalog", serving="v1")
    with pytest.raises(ValueError, match="incompatible"):
        merge_workers(
            [WorkerMetrics("w1", "a", left.snapshot()), WorkerMetrics("w2", "a", right.snapshot())],
            expected_workers={"w1", "w2"},
        )
    with pytest.raises(ValueError):
        left.adjust_gauge("backend_inflight", -1, backend="v1", role="serving")
    assert left.value("collection_queue_depth") is None
