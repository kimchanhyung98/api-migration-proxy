import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx


def check(url: str, event_store: str, serving: str = "v1") -> None:
    assert serving in {"v1", "v2"}, "serving must be v1 or v2"
    shadow = "v2" if serving == "v1" else "v1"
    started = time.time()
    with httpx.Client(base_url=url, timeout=5, trust_env=False) as client:
        for item, status, body in (
            (
                "smoke",
                200,
                {"id": "smoke", "name": "Synthetic item"}
                if serving == "v1"
                else {
                    "code": "0000",
                    "message": "Success",
                    "result": {"id": "smoke", "name": "Synthetic item"},
                },
            ),
            (
                "missing",
                404,
                {"result": []}
                if serving == "v1"
                else {"code": "NOT_FOUND", "message": "Not found.", "result": []},
            ),
            (
                "error",
                500,
                {"result": []}
                if serving == "v1"
                else {"code": "SYNTHETIC_FAILURE", "message": "synthetic failure", "result": []},
            ),
        ):
            response = client.get(f"/items/{item}")
            assert response.status_code == status, f"{item}: backend status was not preserved"
            assert response.json() == body, f"{item}: backend body was not preserved"
            assert response.headers["x-backend-version"] == serving, f"expected {serving} serving"
            assert response.headers["content-type"] == "application/json"

    deadline = time.monotonic() + 10
    while True:
        try:
            with closing(
                sqlite3.connect(Path(event_store).resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                rows = db.execute(
                    "SELECT summary FROM comparison_event WHERE created_at >= ? "
                    "AND route_id = 'synthetic_item' ORDER BY created_at LIMIT 100",
                    (started,),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        summaries = [json.loads(row[0]) for row in rows]
        outcomes = {
            (
                row["backends"]["v1"]["status_code"],
                row["backends"]["v2"]["status_code"],
                row["comparison"]["result"],
            )
            for row in summaries
        }
        if {
            (200, 200, "not_comparable"),
            (404, 404, "not_comparable"),
            (500, 500, "execution_error"),
        } <= outcomes:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("expected comparison events were not stored")
        time.sleep(0.05)

    for summary in summaries:
        assert summary["serving_backend"] == serving
        assert summary["shadow_dispatched"] is True
        assert summary["request_outcome"] == "completed"
        assert summary["backends"][serving]["role"] == "serving"
        assert summary["backends"][shadow]["role"] == "shadow"
        assert summary["detail_state"] == "disabled"
    print(
        f"PASS: {serving} serving, 200/404/500 responses, {shadow} shadow and SQLite event storage"
    )
    print("Comparison context is unconfigured; successful pairs remain not_comparable.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check synthetic serving with shadow enabled")
    parser.add_argument("--url", required=True)
    parser.add_argument("--event-store", required=True)
    parser.add_argument("--serving", choices=("v1", "v2"), default="v1")
    args = parser.parse_args()
    check(args.url, args.event_store, args.serving)
