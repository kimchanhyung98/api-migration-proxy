"""원본 요청 경로를 보존하는 스트리밍 백엔드 전송."""

import logging
import math
from collections.abc import AsyncIterable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

from api_migration_proxy.proxy._duplex import DuplexHTTPTransport

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
    """연결 단위 헤더와 Connection 헤더가 지정한 추가 헤더 제거."""
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
    """HTTP 연결 한도와 민감한 라이브러리 로그 억제를 적용하는 전송기."""

    def __init__(self, timeout_seconds: float, max_connections: int) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            not isinstance(max_connections, int)
            or isinstance(max_connections, bool)
            or max_connections < 1
        ):
            raise ValueError("max_connections must be positive")
        # 원본 응답 헤더와 예외 데이터가 포함된 라이브러리 로그 억제.
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
        self._transport = DuplexHTTPTransport(max_connections)

    async def open(
        self,
        method: str,
        url: str | httpx.URL,
        headers: Iterable[tuple[bytes, bytes]],
        body: AsyncIterable[bytes],
    ) -> httpx.Response:
        """본문을 스트리밍 전송하고 응답 스트림 반환.

        Args:
            method: 대소문자를 보존할 원본 HTTP 메서드.
            url: 원본 경로와 쿼리를 포함한 백엔드 URL.
            headers: 전달할 원본 헤더 목록.
            body: 요청 본문의 비동기 바이트 스트림.

        Returns:
            아직 소비하지 않은 응답. 호출자가 읽은 뒤 aclose() 호출 필요.
        """
        raw_url = str(url).partition("#")[0]
        parsed = urlsplit(raw_url)
        # 원본 경로 보존을 위해 HTTPX의 점 세그먼트 정규화 우회.
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
        request.method = method
        with _private_http_logs():
            response = await self._transport.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = _PrivateResponseStream(response.stream)
        response.request = request
        return response

    async def aclose(self) -> None:
        """전송기가 보유한 연결 풀 종료."""
        with _private_http_logs():
            await self._transport.aclose()
