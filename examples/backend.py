import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
version = os.environ.get("BACKEND_VERSION", "v1")
if version not in {"v1", "v2"}:
    raise ValueError("BACKEND_VERSION must be v1 or v2")


@app.get("/health")
async def health():
    return {"status": "alive"}


@app.get("/items/{item_id}")
async def item(item_id: str):
    headers = {"x-backend-version": version}
    if item_id == "error":
        return JSONResponse({"detail": "synthetic failure"}, status_code=500, headers=headers)
    if item_id == "missing":
        return JSONResponse({"detail": "synthetic missing item"}, status_code=404, headers=headers)
    if item_id == "slow" and version == "v2":
        await asyncio.sleep(0.5)
    name = "Changed item" if item_id == "different" and version == "v2" else "Synthetic item"
    return JSONResponse({"id": item_id, "name": name}, headers=headers)
