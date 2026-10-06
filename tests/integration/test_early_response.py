import asyncio
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from api_migration_proxy.routing.configuration import ShadowPolicy


@pytest.fixture
async def raw_backend_factory():
    servers, tasks = [], set()

    async def make(handler):
        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                await handler(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        return SimpleNamespace(url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")

    yield make
    for server in servers:
        server.close()
    pending = tuple(tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for server in servers:
        await server.wait_closed()


@pytest.mark.parametrize("capture_limit", [4, 4096])
async def test_selected_upload_reaches_serving_before_eof_and_preserves_body(
    raw_backend_factory, backend_factory, runtime_factory, asgi_request, capture_limit
):
    first_received = asyncio.Event()
    received = []

    async def backend(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        first = await reader.readexactly(3)
        first_received.set()
        received.append(first + await reader.readexactly(9))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
        await writer.drain()

    v1, v2 = await raw_backend_factory(backend), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"method": "POST"},
        budget_values={"request_capture_limit_bytes": capture_limit},
    )
    exchange = asgi_request(
        harness.runtime,
        method="POST",
        headers=((b"content-length", b"12"),),
        chunks=(),
    )
    try:
        exchange.incoming.put_nowait({"type": "http.request", "body": b"abc", "more_body": True})
        await asyncio.wait_for(first_received.wait(), 0.3)
        assert not v2.requests
        exchange.incoming.put_nowait({"type": "http.request", "body": b"def", "more_body": True})
        exchange.incoming.put_nowait(
            {"type": "http.request", "body": b"ghijkl", "more_body": False}
        )
        await exchange.wait()
        await harness.runtime.flush()
        assert exchange.status == 200
        assert received == [b"abcdefghijkl"]
        record = (await harness.records())[0]
        if capture_limit == 4:
            assert not v2.requests
            assert record["backends"]["v2"]["reason"] == "request_oversized"
        else:
            assert [request.body for request in v2.requests] == received
    finally:
        exchange.disconnect()


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("known_length", [False, True])
@pytest.mark.parametrize("status", [200, 413])
async def test_final_response_before_upload_eof_is_forwarded_without_replacement(
    raw_backend_factory,
    backend_factory,
    runtime_factory,
    asgi_request,
    selected,
    known_length,
    status,
):
    body = b"early response\x00unchanged"
    calls = []

    async def backend(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        calls.append(headers)
        writer.write(
            f"HTTP/1.1 {status} Early\r\nContent-Length: {len(body)}\r\n".encode()
            + b"Set-Cookie: first=one\r\nSet-Cookie: second=two\r\n"
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()

    v1, v2 = await raw_backend_factory(backend), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"method": "POST", "shadow": ShadowPolicy(selected, int(selected), "safe")},
        budget_values={"serving_max_inflight": 1, "serving_timeout_seconds": 0.2},
    )
    exchange = asgi_request(
        harness.runtime,
        method="POST",
        headers=((b"content-length", b"200"),) if known_length else (),
        chunks=(),
    )
    exchange.incoming.put_nowait({"type": "http.request", "body": b"x", "more_body": True})
    await exchange.wait()
    await harness.runtime.flush()
    assert exchange.status == status
    assert exchange.body == body
    headers = next(message["headers"] for message in exchange.messages if "headers" in message)
    assert [value for key, value in headers if key.lower() == b"set-cookie"] == [
        b"first=one",
        b"second=two",
    ]
    assert len(calls) == 1
    assert not v2.requests
    assert harness.runtime.observation_status()["active_requests"] == 0
    assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    assert (
        harness.metrics.value(
            "proxy_requests_total",
            route="catalog",
            serving="v1",
            outcome="completed",
            source="backend",
        )
        == 1
    )
    if selected:
        record = (await harness.records())[0]
        assert record["backends"]["v2"]["reason"] == "capture_unavailable"
        assert record["shadow_dispatched"] is False
    following = await asgi_request(harness.runtime, method="POST").wait()
    assert following.status == status


async def test_final_headers_allow_remaining_upload_and_complete_shadow_capture(
    raw_backend_factory, backend_factory, runtime_factory, asgi_request
):
    received = []

    async def backend(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        first = await reader.readexactly(3)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":')
        await writer.drain()
        received.append(first + await reader.readexactly(9))
        writer.write(b"true}")
        await writer.drain()

    v1, v2 = await raw_backend_factory(backend), await backend_factory()
    harness = await runtime_factory(v1, v2, route_values={"method": "POST"})
    exchange = asgi_request(
        harness.runtime, method="POST", headers=((b"content-length", b"12"),), chunks=()
    )
    try:
        exchange.incoming.put_nowait({"type": "http.request", "body": b"abc", "more_body": True})
        async with asyncio.timeout(0.5):
            while not exchange.body:
                await asyncio.sleep(0)
        assert exchange.status == 200
        assert exchange.body == b'{"ok":'
        assert not v2.requests
        exchange.incoming.put_nowait(
            {"type": "http.request", "body": b"defghijkl", "more_body": False}
        )
        await exchange.wait()
        await harness.runtime.flush()
        assert exchange.body == b'{"ok":true}'
        assert received == [b"abcdefghijkl"]
        assert [request.body for request in v2.requests] == received
    finally:
        exchange.disconnect()


@pytest.mark.parametrize("selected", [False, True])
async def test_disconnect_during_upload_and_response_has_one_reader_and_releases_slots(
    raw_backend_factory, backend_factory, runtime_factory, asgi_request, selected
):
    async def backend(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 413 Early\r\nContent-Length: 20\r\n\r\npartial")
        await writer.drain()
        await asyncio.Event().wait()

    v1, v2 = await raw_backend_factory(backend), await backend_factory()
    harness = await runtime_factory(
        v1,
        v2,
        route_values={"method": "POST", "shadow": ShadowPolicy(selected, int(selected), "safe")},
    )
    reading = maximum_readers = 0

    async def tracked_runtime(scope, receive, send):
        async def tracked_receive():
            nonlocal reading, maximum_readers
            reading += 1
            maximum_readers = max(reading, maximum_readers)
            try:
                return await receive()
            finally:
                reading -= 1

        await harness.runtime(scope, tracked_receive, send)

    exchange = asgi_request(
        tracked_runtime, method="POST", headers=((b"content-length", b"200"),), chunks=()
    )
    try:
        exchange.incoming.put_nowait({"type": "http.request", "body": b"x", "more_body": True})
        async with asyncio.timeout(0.5):
            while not exchange.body:
                await asyncio.sleep(0)
        exchange.disconnect()
        done, _ = await asyncio.wait({exchange.task}, timeout=0.5)
        assert done
        await exchange.wait()
        await harness.runtime.flush()
        assert exchange.status == 413
        assert exchange.body == b"partial"
        assert not any(message.get("more_body") is False for message in exchange.messages)
        assert maximum_readers == 1
        assert reading == 0
        assert not v2.requests
        assert harness.runtime._shadow_slots == 0
        assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    finally:
        exchange.disconnect()


async def test_repeated_cancellation_during_response_cleanup_releases_shadow_reservation(
    backend_factory, runtime_factory, asgi_request, monkeypatch
):
    opened, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            yield b"partial"
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()
            await self.body.aclose()

    async def open_response(_method, _url, _headers, body):
        await anext(body)
        opened.set()
        return httpx.Response(200, stream=Stream(body))

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, route_values={"method": "POST"})
    monkeypatch.setattr(harness.runtime._transports["serving"], "open", open_response)
    exchange = asgi_request(harness.runtime, method="POST", chunks=())
    exchange.incoming.put_nowait({"type": "http.request", "body": b"prefix", "more_body": True})
    try:
        await asyncio.wait_for(opened.wait(), 0.5)
        exchange.task.cancel()
        await asyncio.wait_for(closing.wait(), 0.5)
        exchange.task.cancel()
        release.set()
        await asyncio.gather(exchange.task, return_exceptions=True)
        await harness.runtime.flush()
        assert harness.runtime._shadow_slots == 0
        assert harness.runtime.observation_status()["active_requests"] == 0
        assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    finally:
        release.set()
        exchange.disconnect()


async def test_unstarted_upload_hands_receive_to_disconnect_watcher_before_slow_send(
    backend_factory, runtime_factory, asgi_request, monkeypatch
):
    sending, release = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"reply"

    async def open_response(*_):
        return httpx.Response(413, stream=Stream())

    async def slow_send(message):
        if message["type"] == "http.response.start":
            sending.set()
            await release.wait()

    v1, v2 = await backend_factory(), await backend_factory()
    harness = await runtime_factory(v1, v2, route_values={"method": "POST"})
    monkeypatch.setattr(harness.runtime._transports["serving"], "open", open_response)
    exchange = asgi_request(harness.runtime, method="POST", chunks=(), on_send=slow_send)
    try:
        await asyncio.wait_for(sending.wait(), 0.5)
        exchange.disconnect()
        done, _ = await asyncio.wait({exchange.task}, timeout=0.3)
        assert done
        await exchange.wait()
        await harness.runtime.flush()
        assert harness.runtime._shadow_slots == 0
        assert not v2.requests
        assert harness.metrics.value("backend_inflight", backend="v1", role="serving") == 0
    finally:
        release.set()
        exchange.disconnect()
