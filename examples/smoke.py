import argparse
import json
import sqlite3
import time
from pathlib import Path

import httpx


def check(url: str, event_store: str) -> None:
    started = time.time()
    with httpx.Client(base_url=url, timeout=5, trust_env=False) as client:
        response = client.get("/items/smoke")
        assert response.status_code == 200, "serving request failed"
        assert response.json() == {"id": "smoke", "name": "Synthetic item"}
        assert response.headers["x-backend-version"] == "v1", "expected v1 serving"
        response = client.get("/items/error")
        assert response.status_code == 500, "backend error was not preserved"
        assert response.headers["x-backend-version"] == "v1"

    deadline = time.monotonic() + 10
    while True:
        try:
            with sqlite3.connect(Path(event_store).resolve().as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT summary FROM comparison_event WHERE created_at >= ? "
                    "AND route_id = 'synthetic_item' ORDER BY created_at LIMIT 100",
                    (started,),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        summaries = [json.loads(row[0]) for row in rows]
        outcomes = {row["comparison"]["result"] for row in summaries}
        if {"not_comparable", "execution_error"} <= outcomes:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("expected comparison events were not stored")
        time.sleep(0.05)

    for summary in summaries:
        assert summary["serving_backend"] == "v1"
        assert summary["shadow_dispatched"] is True
        assert summary["request_outcome"] == "completed"
        assert summary["backends"]["v1"]["role"] == "serving"
        assert summary["backends"]["v2"]["role"] == "shadow"
        assert summary["detail_state"] == "disabled"
    print("PASS: HTTP serving, backend errors, v1/v2 execution and SQLite event storage")
    print("Comparison context is unconfigured; successful pairs remain not_comparable.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the default synthetic Docker configuration")
    parser.add_argument("--url", required=True)
    parser.add_argument("--event-store", required=True)
    args = parser.parse_args()
    check(args.url, args.event_store)
