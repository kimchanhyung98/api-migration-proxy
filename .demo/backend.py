import asyncio
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


def create_app(version: str) -> FastAPI:
    if version not in {"v1", "v2"}:
        raise ValueError("version must be v1 or v2")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    item_requests = 0

    def success_response(result: dict, headers: dict | None = None) -> JSONResponse:
        body = (
            result if version == "v1" else {"code": "0000", "message": "Success", "result": result}
        )
        return JSONResponse(body, headers=headers)

    def error_response(
        status_code: int, code: str, message: str, headers: dict | None = None
    ) -> JSONResponse:
        body = (
            {"result": []} if version == "v1" else {"code": code, "message": message, "result": []}
        )
        return JSONResponse(body, status_code=status_code, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(_request, error: HTTPException):
        status = HTTPStatus(error.status_code)
        return error_response(
            status.value,
            status.name,
            status.phrase.capitalize() + ".",
            {**(error.headers or {}), "x-backend-version": version},
        )

    @app.get("/health")
    async def health():
        return success_response({"status": "alive"})

    @app.get("/__demo/requests")
    async def requests():
        return {"version": version, "items": item_requests}

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        nonlocal item_requests
        item_requests += 1
        headers = {"x-backend-version": version}
        if item_id == "error":
            return error_response(500, "SYNTHETIC_FAILURE", "synthetic failure", headers)
        if item_id == "missing":
            return error_response(404, "NOT_FOUND", "Not found.", headers)
        if item_id == "slow" and version == "v2":
            await asyncio.sleep(0.5)
        name = "Changed item" if version == "v2" and item_id == "different" else "Synthetic item"
        return success_response({"id": item_id, "name": name}, headers=headers)

    return app
