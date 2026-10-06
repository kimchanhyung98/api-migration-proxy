from __future__ import annotations


def evaluate_observations(
    observation: dict,
    events: list,
    *,
    revision: str,
    backend_counts: dict[str, int],
    shadow_enabled: bool,
    previous_events: list | tuple = (),
) -> dict:
    counts = {backend: backend_counts.get(backend, -1) for backend in ("v1", "v2")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("backend counts must be nonnegative integers")
    if not isinstance(revision, str) or not revision or type(shadow_enabled) is not bool:
        raise ValueError("a revision and boolean shadow state are required")
    requests = sum(counts.values())
    shadow_requests = requests if shadow_enabled else 0
    failures: list[str] = []

    def check(name, actual, wanted):
        matches = actual is wanted if type(wanted) is bool else actual == wanted
        if not matches:
            failures.append(f"{name}: expected {wanted!r}, observed {actual!r}")

    def fields(value):
        return value if isinstance(value, dict) else {}

    check("health.ready", fields(observation.get("health")).get("ready"), True)
    check("configuration_revision", observation.get("configuration_revision"), revision)
    shutdown = fields(observation.get("shutdown"))
    check("shutdown.status", shutdown.get("status"), "exited")
    exit_code = shutdown.get("exit_code")
    check("shutdown.exit_code", type(exit_code) is int and exit_code in (0, 143), True)
    check("shutdown.oom_killed", shutdown.get("oom_killed"), False)
    check(
        "shutdown.application_shutdown_complete",
        shutdown.get("application_shutdown_complete"),
        True,
    )

    backends = fields(observation.get("backends"))
    observed_requests = {}
    expected_requests = {
        backend: requests if shadow_enabled else n for backend, n in counts.items()
    }
    for backend in ("v1", "v2"):
        values = []
        for boundary in ("before", "after"):
            sample = fields(fields(backends.get(boundary)).get(backend))
            check(f"backends.{boundary}.{backend}.version", sample.get("version"), backend)
            value = sample.get("items")
            valid = type(value) is int and value >= 0
            check(f"backends.{boundary}.{backend}.items_valid", valid, True)
            values.append(value if valid else None)
        before, after = values
        delta = after - before if before is not None and after is not None else None
        observed_requests[backend] = delta
        check(f"backends.{backend}.requests", delta, expected_requests[backend])

    previous = {event["event_id"]: event for event in previous_events}
    stored = {}
    current_events = []
    for event in events:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in stored:
            failures.append("stored event IDs must be unique nonempty strings")
            current_events.append(event)
            continue
        stored[event_id] = event
        if event_id not in previous:
            current_events.append(event)
    retained = sum(stored.get(event_id) == event for event_id, event in previous.items())
    check("events.retained", retained, len(previous))
    check("events.count", len(current_events), shadow_requests)
    route = "synthetic_item"
    event_ids: set[str] = set()
    serving_counts = {"v1": 0, "v2": 0}
    for index, event in enumerate(current_events):
        prefix = f"events[{index}]"
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in event_ids:
            failures.append(f"{prefix}.event_id must be a unique nonempty string")
        else:
            event_ids.add(event_id)
        summary = event.get("summary") if isinstance(event, dict) else None
        if not isinstance(summary, dict):
            failures.append(f"{prefix}.summary must be an object")
            continue
        serving = summary.get("serving_backend")
        if not isinstance(serving, str) or serving not in serving_counts:
            failures.append(f"{prefix}.serving_backend must be v1 or v2")
        else:
            serving_counts[serving] += 1
        for field, wanted in {
            "configuration_revision": revision,
            "route_id": route,
            "response_source": "backend",
            "request_outcome": "completed",
            "shadow_selected": True,
            "shadow_dispatched": True,
            "detail_state": "disabled",
            "detail_sampled": False,
        }.items():
            check(f"{prefix}.{field}", summary.get(field), wanted)
        comparison = summary.get("comparison")
        comparison = comparison if isinstance(comparison, dict) else {}
        for field, wanted in {
            "result": "not_comparable",
            "reason": "authorization_context_unknown",
            "comparison_class": "unavailable",
        }.items():
            check(f"{prefix}.comparison.{field}", comparison.get(field), wanted)
        backends = summary.get("backends")
        backends = backends if isinstance(backends, dict) else {}
        for backend in ("v1", "v2"):
            attempt = backends.get(backend)
            attempt = attempt if isinstance(attempt, dict) else {}
            for field, wanted in {
                "backend": backend,
                "role": "serving" if backend == serving else "shadow",
                "execution_outcome": "http_response",
                "contract_class": "success",
                "status_code": 200,
                "response_complete": True,
            }.items():
                check(f"{prefix}.backends.{backend}.{field}", attempt.get(field), wanted)
    expected_serving = counts if shadow_enabled else {"v1": 0, "v2": 0}
    check("events.serving_counts", serving_counts, expected_serving)
    return {
        "passed": not failures,
        "failures": failures,
        "expected": {
            "revision": revision,
            "requests": requests,
            "backend_counts": counts,
            "backend_requests": expected_requests,
            "shadow_enabled": shadow_enabled,
            "event_count": shadow_requests,
            "retained_event_count": len(previous),
            "event_serving_counts": expected_serving,
        },
        "observed": {
            "backend_requests": observed_requests,
            "event_count": len(current_events),
            "unique_event_ids": len(event_ids),
            "retained_event_count": retained,
            "event_serving_counts": serving_counts,
            "shutdown": shutdown,
        },
    }
