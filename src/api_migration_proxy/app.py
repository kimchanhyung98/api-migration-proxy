from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from api_migration_proxy.observability.prometheus import prometheus_text

if TYPE_CHECKING:
    from api_migration_proxy.proxy.runtime import ProxyRuntime


def create_app(runtime: ProxyRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.mount("/", runtime)
    return app


def create_control_app(
    runtime: ProxyRuntime,
    *,
    authorize: Callable[[Request], bool | Awaitable[bool]],
) -> FastAPI:
    async def require_access(request: Request) -> None:
        try:
            result = authorize(request)
            allowed = await result if inspect.isawaitable(result) else result
        except Exception:
            raise HTTPException(status_code=503, detail="Access verification unavailable") from None
        if allowed is not True:
            raise HTTPException(status_code=403, detail="Access denied")

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_access)],
        redirect_slashes=False,
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        is_ready = runtime.ready
        snapshot = runtime.config.current
        return JSONResponse(
            {"ready": is_ready, "revision": snapshot.revision if snapshot else None},
            status_code=200 if is_ready else 503,
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            prometheus_text(runtime.metric_snapshot()),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/state")
    async def state() -> dict:
        return runtime.observation_status()

    return app
