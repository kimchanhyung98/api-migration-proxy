import asyncio
import time

import httpx
import pytest

from backend import create_app
from v1.app import app as v1_app
from v2.app import app as v2_app


@pytest.mark.parametrize("version,app", [("v1", v1_app), ("v2", v2_app)])
async def test_version_entry_points_preserve_synthetic_responses(version, app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == (
            {"status": "alive"}
            if version == "v1"
            else {"code": "0000", "message": "Success", "result": {"status": "alive"}}
        )
        assert "x-backend-version" not in health.headers
        for item_id, status, body in (
            (
                "same",
                200,
                {"id": "same", "name": "Synthetic item"}
                if version == "v1"
                else {
                    "code": "0000",
                    "message": "Success",
                    "result": {"id": "same", "name": "Synthetic item"},
                },
            ),
            (
                "different",
                200,
                {"id": "different", "name": "Synthetic item"}
                if version == "v1"
                else {
                    "code": "0000",
                    "message": "Success",
                    "result": {"id": "different", "name": "Changed item"},
                },
            ),
            (
                "sample",
                200,
                {"id": "sample", "name": "Synthetic item"}
                if version == "v1"
                else {
                    "code": "0000",
                    "message": "Success",
                    "result": {"id": "sample", "name": "Synthetic item"},
                },
            ),
            (
                "missing",
                404,
                {"result": []}
                if version == "v1"
                else {"code": "NOT_FOUND", "message": "Not found.", "result": []},
            ),
            (
                "error",
                500,
                {"result": []}
                if version == "v1"
                else {"code": "SYNTHETIC_FAILURE", "message": "synthetic failure", "result": []},
            ),
        ):
            response = await client.get(f"/items/{item_id}")
            assert response.status_code == status
            assert response.json() == body
            assert response.headers["x-backend-version"] == version
            assert response.headers["content-type"] == "application/json"
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404


async def test_v2_slow_response_preserves_delay_and_body():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=v2_app), base_url="http://synthetic.test"
    ) as client:
        started = time.monotonic()
        response = await client.get("/items/slow")
        elapsed = time.monotonic() - started
    assert elapsed >= 0.5
    assert response.json() == {
        "code": "0000",
        "message": "Success",
        "result": {"id": "slow", "name": "Synthetic item"},
    }
    assert response.headers["x-backend-version"] == "v2"


def test_backend_factory_rejects_unknown_version():
    with pytest.raises(ValueError, match="version must be v1 or v2"):
        create_app("v3")


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_observation_counts_only_actual_item_requests(version):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(version)), base_url="http://synthetic.test"
    ) as client:
        before = await client.get("/__demo/requests")
        assert before.status_code == 200
        assert before.json() == {"version": version, "items": 0}
        for path in ("/health", "/__demo/requests", "/not-a-route"):
            await client.get(path)
        await client.post("/items/same")
        assert (await client.get("/__demo/requests")).json()["items"] == 0
        responses = await asyncio.gather(
            *(client.get(f"/items/{item}") for item in ("same", "different", "missing", "error"))
        )
        assert [response.status_code for response in responses] == [200, 200, 404, 500]
        for _ in range(2):
            assert (await client.get("/__demo/requests")).json() == {
                "version": version,
                "items": 4,
            }


@pytest.mark.parametrize("version,app", [("v1", v1_app), ("v2", v2_app)])
@pytest.mark.parametrize(
    ("method", "path", "status", "code", "message"),
    [
        ("GET", "/not-a-route", 404, "NOT_FOUND", "Not found."),
        ("POST", "/items/same", 405, "METHOD_NOT_ALLOWED", "Method not allowed."),
    ],
)
async def test_framework_http_errors_use_version_contract_and_keep_headers(
    version, app, method, path, status, code, message
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
    ) as client:
        response = await client.request(method, path)
    assert response.status_code == status
    assert response.json() == (
        {"result": []} if version == "v1" else {"code": code, "message": message, "result": []}
    )
    assert response.headers["x-backend-version"] == version
    if status == 405:
        assert response.headers["allow"] == "GET"
