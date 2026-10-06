import asyncio

import httpcore
import httpx


class _HTTP11Connection(httpcore.AsyncHTTP11Connection):
    # These overrides depend on the pinned HTTPcore 1.0.9 lifecycle.
    _upload: asyncio.Task[None] | None = None

    async def _send_request_body(self, request: httpcore.Request) -> None:
        self._upload = asyncio.create_task(
            super()._send_request_body(request), name="proxy_backend_upload"
        )

    async def _receive_response_headers(self, request: httpcore.Request):
        if self._upload is None:
            return await super()._receive_response_headers(request)
        receive = asyncio.create_task(
            super()._receive_response_headers(request), name="proxy_backend_headers"
        )
        try:
            await asyncio.wait((self._upload, receive), return_when=asyncio.FIRST_COMPLETED)
            if receive.done():
                return receive.result()
            try:
                self._upload.result()
            except httpcore.WriteError:
                # A backend may close its input and still return a valid response.
                pass
            return await receive
        finally:
            if not receive.done():
                receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)

    async def _stop_upload(self) -> bool:
        upload, self._upload = self._upload, None
        if upload is None:
            return True
        completed = upload.done() and not upload.cancelled() and upload.exception() is None
        if not upload.done():
            upload.cancel()
        while not upload.done():
            try:
                await asyncio.gather(upload, return_exceptions=True)
            except asyncio.CancelledError:
                # Finish socket/pool cleanup even when its caller is cancelled again.
                pass
        if not upload.cancelled():
            upload.exception()
        return completed

    async def _response_closed(self) -> None:
        if await self._stop_upload():
            await super()._response_closed()
        else:
            # h11 can mark DONE before the final socket write has completed.
            await self.aclose()

    async def aclose(self) -> None:
        await self._stop_upload()
        closing = asyncio.create_task(super().aclose())
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                # HTTPcore still has to remove this request from its pool.
                pass
        closing.result()


class _HTTPConnection(httpcore.AsyncHTTPConnection):
    async def handle_async_request(self, request: httpcore.Request) -> httpcore.Response:
        if not self.can_handle_request(request.url.origin):
            raise RuntimeError("request origin does not match the backend connection")
        try:
            async with self._request_lock:
                if self._connection is None:
                    stream = await self._connect(request)
                    self._connection = _HTTP11Connection(
                        origin=self._origin,
                        stream=stream,
                        keepalive_expiry=self._keepalive_expiry,
                    )
        except BaseException:
            self._connect_failed = True
            raise
        return await self._connection.handle_async_request(request)


class _ConnectionPool(httpcore.AsyncConnectionPool):
    def create_connection(self, origin: httpcore.Origin) -> httpcore.AsyncHTTPConnection:
        return _HTTPConnection(
            origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=self._network_backend,
        )


class DuplexHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, max_connections: int) -> None:
        # HTTPX 0.28.1 delegates transport operations to this pool without buffering.
        self._pool = _ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
        )
