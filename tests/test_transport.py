import asyncio
import gzip
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import httpx
import pytest

from api_migration_proxy.transport import BackendTransport, forwarding_headers


@dataclass
class ReceivedRequest:
    method: bytes
    target: bytes
    headers: list[tuple[bytes, bytes]]
    body: bytes
    connection: int


Handler = Callable[[ReceivedRequest, asyncio.StreamWriter], Awaitable[None]]


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def raw_body(response: httpx.Response) -> bytes:
    try:
        return b"".join([part async for part in response.aiter_raw()])
    finally:
        await response.aclose()


async def ok(request: ReceivedRequest, writer: asyncio.StreamWriter) -> None:
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    await writer.drain()


@asynccontextmanager
async def backend(handler: Handler = ok):
    requests: list[ReceivedRequest] = []
    connections: list[asyncio.StreamWriter] = []
    tasks: set[asyncio.Task] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connections.append(writer)
        connection = len(connections)
        task = asyncio.current_task()
        tasks.add(task)
        try:
            while not writer.is_closing():
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                method, target, _ = lines[0].split(b" ", 2)
                headers = [
                    (name.lower(), value.strip())
                    for line in lines[1:]
                    if line
                    for name, value in [line.split(b":", 1)]
                ]
                fields = dict(headers)
                body = b""
                if fields.get(b"transfer-encoding") == b"chunked":
                    while True:
                        size = int((await reader.readline()).strip(), 16)
                        if not size:
                            await reader.readexactly(2)
                            break
                        body += await reader.readexactly(size)
                        assert await reader.readexactly(2) == b"\r\n"
                else:
                    body = await reader.readexactly(int(fields.get(b"content-length", b"0")))
                request = ReceivedRequest(method, target, headers, body, connection)
                requests.append(request)
                await handler(request, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        for writer in connections:
            writer.close()
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


def test_connection_fields_and_duplicate_response_headers():
    headers = [
        (b"Connection", b"keep-alive, X-Internal"),
        (b"connection", b"X-Other"),
        (b"X-Internal", b"secret"),
        (b"x-other", b"secret"),
        (b"Keep-Alive", b"timeout=3"),
        (b"Transfer-Encoding", b"chunked"),
        (b"Set-Cookie", b"a=1"),
        (b"Set-Cookie", b"b=2"),
        (b"ETag", b'"revision-1"'),
    ]
    assert forwarding_headers(iter(headers)) == headers[-3:]
    assert len(headers) == 9


@pytest.mark.asyncio
async def test_preserves_raw_target_body_headers_and_target_host():
    original = [
        (b"Host", b"public.example"),
        (b"Content-Length", b"7"),
        (b"Content-Type", b"application/octet-stream"),
        (b"Authorization", b"Bearer request-token"),
        (b"Cookie", b"session=request-session"),
        (b"X-Tenant", b"current-tenant"),
        (b"X-Duplicate", b"first"),
        (b"X-Duplicate", b"second"),
        (b"Connection", b"X-Internal, keep-alive"),
        (b"X-Internal", b"remove-me"),
    ]
    async with backend() as (url, requests):
        transport = BackendTransport(1, 2)
        try:
            response = await transport.open(
                "POST",
                url + "/a%2Fb/%2e%2e?tag=one&tag=two&x=%2f&blank=",
                original,
                chunks(b"\x00ab", b"cd\xff\x01"),
            )
            assert await raw_body(response) == b"ok"
        finally:
            await transport.aclose()
    request = requests[0]
    assert request.target == b"/a%2Fb/%2e%2e?tag=one&tag=two&x=%2f&blank="
    assert request.body == b"\x00abcd\xff\x01"
    assert dict(request.headers)[b"host"] == url.removeprefix("http://").encode()
    assert dict(request.headers)[b"content-length"] == b"7"
    assert dict(request.headers)[b"authorization"] == b"Bearer request-token"
    assert dict(request.headers)[b"cookie"] == b"session=request-session"
    assert dict(request.headers)[b"x-tenant"] == b"current-tenant"
    assert [value for name, value in request.headers if name == b"x-duplicate"] == [
        b"first",
        b"second",
    ]
    assert b"x-internal" not in dict(request.headers)
    assert original[0] == (b"Host", b"public.example")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["/a/../b", "/a/./b", "/a%2Fb", "/x?x=1&x=2", "/a;b?", "//a/%2e%2e"]
)
async def test_raw_request_target_bypasses_url_normalization(target):
    async with backend() as (url, requests):
        transport = BackendTransport(1, 1)
        try:
            response = await transport.open("GET", url + target, [], chunks())
            assert await raw_body(response) == b"ok"
        finally:
            await transport.aclose()
    assert requests[0].target == target.encode()


@pytest.mark.asyncio
async def test_unknown_length_body_is_sent_once_without_content_decoding():
    wire = gzip.compress(b'{"payload":"value"}')
    async with backend() as (url, requests):
        transport = BackendTransport(1, 1)
        try:
            response = await transport.open(
                "POST",
                url,
                [(b"Content-Encoding", b"gzip")],
                chunks(wire[:3], wire[3:11], wire[11:]),
            )
            assert await raw_body(response) == b"ok"
        finally:
            await transport.aclose()
    assert len(requests) == 1
    assert requests[0].body == wire
    assert dict(requests[0].headers)[b"content-encoding"] == b"gzip"


@pytest.mark.asyncio
async def test_redirect_compression_and_set_cookie_are_not_transformed():
    wire = gzip.compress(b'{"result":true}')

    async def redirect(request, writer):
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\n"
            b"Set-Cookie: first=1; Path=/\r\nSet-Cookie: second=2; Path=/\r\n"
            b"Content-Encoding: gzip\r\nContent-Length: "
            + str(len(wire)).encode()
            + b"\r\n\r\n"
            + wire
        )
        await writer.drain()

    async with backend(redirect) as (url, requests):
        transport = BackendTransport(1, 2)
        try:
            response = await transport.open("GET", url, [], chunks())
            assert response.status_code == 302
            assert response.headers.get_list("set-cookie") == [
                "first=1; Path=/",
                "second=2; Path=/",
            ]
            assert response.headers["content-encoding"] == "gzip"
            assert int(response.headers["content-length"]) == len(wire)
            assert await raw_body(response) == wire
        finally:
            await transport.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,status,length", [("HEAD", 200, 123), ("GET", 204, None), ("GET", 304, 123)]
)
async def test_bodyless_response_semantics(method, status, length):
    async def respond(request, writer):
        raw = f"HTTP/1.1 {status} Response\r\nETag: revision\r\n".encode()
        if length is not None:
            raw += f"Content-Length: {length}\r\n".encode()
        writer.write(raw + b"\r\n")
        await writer.drain()

    async with backend(respond) as (url, _):
        transport = BackendTransport(1, 1)
        try:
            response = await transport.open(method, url, [], chunks())
            assert response.status_code == status
            assert response.headers["etag"] == "revision"
            assert await raw_body(response) == b""
        finally:
            await transport.aclose()


@pytest.mark.asyncio
async def test_cookie_auth_and_tenant_isolation_sequential_and_concurrent():
    async def cookie_response(request, writer):
        writer.write(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: backend_state=do-not-reuse; Path=/\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await writer.drain()

    async with backend(cookie_response) as (url, requests):
        transport = BackendTransport(1, 4)

        async def run(identity):
            headers = (
                []
                if identity == "anonymous"
                else [
                    (b"Cookie", f"session={identity}".encode()),
                    (b"Authorization", f"Bearer {identity}".encode()),
                    (b"X-Tenant", identity.encode()),
                ]
            )
            response = await transport.open("GET", url + "/" + identity, headers, chunks())
            assert response.headers["set-cookie"].startswith("backend_state=")
            assert await raw_body(response) == b""

        try:
            for identity in ("A", "B", "anonymous"):
                await run(identity)
            await asyncio.gather(*(run(identity) for identity in ("A", "B", "anonymous")))
        finally:
            await transport.aclose()
    assert len(requests) == 6
    assert requests[0].connection == requests[1].connection == requests[2].connection
    for request in requests:
        identity = request.target.removeprefix(b"/")
        fields = dict(request.headers)
        if identity == b"anonymous":
            assert not {b"cookie", b"authorization", b"x-tenant"} & fields.keys()
        else:
            assert fields[b"cookie"] == b"session=" + identity
            assert fields[b"authorization"] == b"Bearer " + identity
            assert fields[b"x-tenant"] == identity


@pytest.mark.asyncio
async def test_environment_proxy_and_certificate_overrides_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/certificate-file")
    async with backend() as (url, requests):
        transport = BackendTransport(1, 1)
        try:
            assert await raw_body(await transport.open("GET", url, [], chunks())) == b"ok"
        finally:
            await transport.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_incomplete_response_raises_without_retry():
    async def truncated(request, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\npartial")
        await writer.drain()
        writer.close()

    async with backend(truncated) as (url, requests):
        transport = BackendTransport(1, 1)
        try:
            response = await transport.open("GET", url, [], chunks())
            with pytest.raises(httpx.RemoteProtocolError):
                await raw_body(response)
            assert response.is_closed
        finally:
            await transport.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_failure_before_response_headers_is_not_retried():
    async def close(request, writer):
        writer.close()

    async with backend(close) as (url, requests):
        transport = BackendTransport(1, 1)
        try:
            with pytest.raises(httpx.RemoteProtocolError):
                await transport.open("GET", url, [], chunks())
        finally:
            await transport.aclose()
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_header_wait_timeout_or_cancellation_releases_connection(cancel):
    async def slow(request, writer):
        if request.target == b"/slow":
            await asyncio.Event().wait()
        else:
            await ok(request, writer)

    async with backend(slow) as (url, requests):
        transport = BackendTransport(1 if cancel else 0.04, 1)
        try:
            if cancel:
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.04):
                        await transport.open("GET", url + "/slow", [], chunks())
            else:
                with pytest.raises(httpx.ReadTimeout):
                    await transport.open("GET", url + "/slow", [], chunks())
            assert await raw_body(await transport.open("GET", url, [], chunks())) == b"ok"
        finally:
            await transport.aclose()
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_pool_wait_is_bounded_and_cancelled_stream_releases_slot():
    async def slow(request, writer):
        if request.target == b"/slow":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\na")
            await writer.drain()
            await asyncio.Event().wait()
        else:
            await ok(request, writer)

    async with backend(slow) as (url, requests):
        transport = BackendTransport(0.05, 1)
        try:
            first = await transport.open("GET", url + "/slow", [], chunks())
            try:
                with pytest.raises(httpx.PoolTimeout):
                    await transport.open("GET", url, [], chunks())
                assert len(requests) == 1
            finally:
                await first.aclose()
            response = await transport.open("GET", url, [], chunks())
            assert await raw_body(response) == b"ok"
        finally:
            await transport.aclose()
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_external_total_deadline_closes_dripping_response():
    async def drip(request, writer):
        if request.target == b"/drip":
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            while True:
                writer.write(b"1\r\na\r\n")
                await writer.drain()
                await asyncio.sleep(0.01)
        else:
            await ok(request, writer)

    async with backend(drip) as (url, _):
        transport = BackendTransport(1, 1)
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.06):
                    response = await transport.open("GET", url + "/drip", [], chunks())
                    await raw_body(response)
            assert response.is_closed
            assert await raw_body(await transport.open("GET", url, [], chunks())) == b"ok"
        finally:
            await transport.aclose()


@pytest.mark.asyncio
async def test_http_debug_logs_do_not_collect_proxy_payloads(caplog):
    secrets = (
        "SYNTHETIC_PRIVATE_QUERY",
        "SYNTHETIC_PRIVATE_AUTH",
        "SYNTHETIC_PRIVATE_COOKIE",
        "SYNTHETIC_PRIVATE_RESPONSE_COOKIE",
        "SYNTHETIC_PRIVATE_RESPONSE_BODY",
    )
    proxy_started = asyncio.Event()
    unrelated_done = asyncio.Event()

    async def respond(request, writer):
        if request.target.startswith(b"/private"):
            proxy_started.set()
            await unrelated_done.wait()
            payload = secrets[-1].encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nSet-Cookie: session="
                + secrets[-2].encode()
                + b"\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
            await writer.drain()
        else:
            await ok(request, writer)

    with caplog.at_level(logging.DEBUG):
        async with backend(respond) as (url, requests):
            transport = BackendTransport(1, 1)

            async def private_request():
                response = await transport.open(
                    "GET",
                    url + "/private?q=" + secrets[0],
                    [
                        (b"Authorization", ("Bearer " + secrets[1]).encode()),
                        (b"Cookie", ("session=" + secrets[2]).encode()),
                    ],
                    chunks(),
                )
                assert secrets[-2] in response.headers["set-cookie"]
                try:
                    received = bytearray()
                    async for part in response.aiter_raw():
                        received.extend(part)
                        logging.getLogger("httpcore.http11").debug("outside_proxy_iteration")
                    assert bytes(received) == secrets[-1].encode()
                finally:
                    await response.aclose()

            task = asyncio.create_task(private_request())
            try:
                await proxy_started.wait()
                async with httpx.AsyncClient(trust_env=False) as unrelated:
                    response = await unrelated.get(url + "/outside_transport")
                    assert response.content == b"ok"
                unrelated_done.set()
                await task
            finally:
                unrelated_done.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await transport.aclose()
    assert len(requests) == 2
    assert all(secret not in caplog.text for secret in secrets)
    assert "outside_transport" in caplog.text
    assert "outside_proxy_iteration" in caplog.text
    assert any(record.name == "httpcore.http11" for record in caplog.records)


@pytest.mark.asyncio
async def test_http_debug_exception_logs_do_not_collect_request_body_failures(caplog):
    async def failed_body():
        yield b"body-prefix"
        raise ValueError("SYNTHETIC_PRIVATE_BODY_FAILURE")

    with caplog.at_level(logging.DEBUG):
        async with backend() as (url, _):
            transport = BackendTransport(1, 1)
            try:
                with pytest.raises(ValueError, match="SYNTHETIC_PRIVATE_BODY_FAILURE"):
                    await transport.open("POST", url, [], failed_body())
                assert await raw_body(await transport.open("GET", url, [], chunks())) == b"ok"
            finally:
                await transport.aclose()
    assert "SYNTHETIC_PRIVATE_BODY_FAILURE" not in caplog.text
    assert not any(record.name.startswith(("httpx", "httpcore")) for record in caplog.records)
