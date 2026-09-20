import logging
import math
from collections.abc import AsyncIterable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_PRIVATE_HTTP_OPERATION: ContextVar[bool] = ContextVar(
    "private_proxy_http_operation", default=False
)


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_HTTP_OPERATION.get()


_TRANSPORT_LOG_FILTER = _TransportLogFilter()


@contextmanager
def _private_http_logs():
    token = _PRIVATE_HTTP_OPERATION.set(True)
    try:
        yield
    finally:
        _PRIVATE_HTTP_OPERATION.reset(token)


class _PrivateResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self):
        iterator = self._stream.__aiter__()
        while True:
            try:
                with _private_http_logs():
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            yield chunk

    async def aclose(self) -> None:
        with _private_http_logs():
            await self._stream.aclose()


def forwarding_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    headers = list(headers)
    connection_fields = {
        field.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for field in value.split(b",")
    }
    excluded = _HOP_BY_HOP | connection_fields
    return [(name, value) for name, value in headers if name.lower() not in excluded]


class BackendTransport:
    def __init__(self, timeout_seconds: float, max_connections: int) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            not isinstance(max_connections, int)
            or isinstance(max_connections, bool)
            or max_connections < 1
        ):
            raise ValueError("max_connections must be positive")
        # Library debug traces include raw response headers and exception payloads.
        for name in (
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
            "httpcore.socks",
        ):
            logging.getLogger(name).addFilter(_TRANSPORT_LOG_FILTER)
        self._timeout = httpx.Timeout(timeout_seconds).as_dict()
        self._transport = httpx.AsyncHTTPTransport(
            verify=True,
            trust_env=False,
            retries=0,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )

    async def open(
        self,
        method: str,
        url: str | httpx.URL,
        headers: Iterable[tuple[bytes, bytes]],
        body: AsyncIterable[bytes],
    ) -> httpx.Response:
        raw_url = str(url).partition("#")[0]
        parsed = urlsplit(raw_url)
        # The transport target must bypass HTTPX's URL dot-segment normalization.
        target = (parsed.path or "/") + ("?" + parsed.query if "?" in raw_url else "")
        request = httpx.Request(
            method,
            url,
            headers=[
                (name, value)
                for name, value in forwarding_headers(headers)
                if name.lower() != b"host"
            ],
            content=body,
            extensions={"timeout": dict(self._timeout), "target": target.encode("ascii")},
        )
        with _private_http_logs():
            response = await self._transport.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = _PrivateResponseStream(response.stream)
        response.request = request
        return response

    async def aclose(self) -> None:
        with _private_http_logs():
            await self._transport.aclose()
