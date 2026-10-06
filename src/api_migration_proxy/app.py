from __future__ import annotations

import inspect
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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


def control_authorizer(token: str) -> Callable[[Request], bool]:
    def authorize(request: Request) -> bool:
        if request.client is None:
            return False
        try:
            if not ipaddress.ip_address(request.client.host).is_loopback:
                return False
        except ValueError:
            return False
        if token:
            headers = request.headers.getlist("authorization")
            if len(headers) != 1:
                return False
            scheme, _, credential = headers[0].partition(" ")
            return scheme.lower() == "bearer" and secrets.compare_digest(
                credential.lstrip(" ").encode("utf-8"), token.encode("utf-8")
            )
        return True

    return authorize


def create_listener_app(
    runtime: ProxyRuntime,
    *,
    control_port: int,
    authorize: Callable[[Request], bool | Awaitable[bool]],
) -> ASGIApp:
    data_app = create_app(runtime)
    control_app = create_control_app(runtime, authorize=authorize)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        server = scope.get("server")
        if scope["type"] != "lifespan" and server and server[1] == control_port:
            await control_app(scope, receive, send)
        else:
            await data_app(scope, receive, send)

    return app


def create_control_app(
    runtime: ProxyRuntime,
    *,
    authorize: Callable[[Request], bool | Awaitable[bool]],
) -> FastAPI:
    local_access = control_authorizer("")

    async def require_access(request: Request) -> None:
        if not local_access(request):
            raise HTTPException(status_code=403, detail="Access denied")
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

    @app.get("/healthcheck")
    async def healthcheck() -> JSONResponse:
        is_ready = runtime.ready
        return JSONResponse(
            {"ready": is_ready},
            status_code=200 if is_ready else 503,
        )

    return app
