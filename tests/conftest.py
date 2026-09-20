import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from api_migration_proxy.collection import (
    BoundedCollector,
    CollectionLimits,
    EventQuery,
    QueryAccess,
    SQLiteEventStore,
)
from api_migration_proxy.comparison import REASON_CODES, ComparisonContext, ComparisonPolicy
from api_migration_proxy.config import (
    Cohort,
    ConfigManager,
    ResponseContract,
    Route,
    RuntimeBudgets,
    ShadowPolicy,
    Snapshot,
)
from api_migration_proxy.observability import DEFAULT_REASONS, Metrics
from api_migration_proxy.runtime import ProxyRuntime, WorkLimits


@pytest.fixture
def respond():
    async def write(writer, body=b'{"ok":true}', *, status=200, headers=()):
        fields = [(b"content-type", b"application/json"), *headers]
        if not any(key.lower() == b"content-length" for key, _ in fields):
            fields.append((b"content-length", str(len(body)).encode()))
        writer.write(f"HTTP/1.1 {status} Response\r\n".encode())
        for key, value in fields:
            writer.write(key + b": " + value + b"\r\n")
        writer.write(b"connection: close\r\n\r\n" + body)
        await writer.drain()

    return write


@pytest.fixture
async def backend_factory(respond):
    servers, tasks = [], set()

    async def make(handler=None):
        backend = SimpleNamespace(requests=[], received=asyncio.Event())

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                start = await reader.readline()
                if not start:
                    return
                method, target, _ = start.rstrip().split(b" ", 2)
                headers = []
                while (line := await reader.readline()) != b"\r\n":
                    if not line:
                        raise EOFError("incomplete request headers")
                    key, value = line.rstrip(b"\r\n").split(b":", 1)
                    headers.append((key.lower(), value.strip()))
                values = dict(headers)
                if values.get(b"transfer-encoding") == b"chunked":
                    parts = []
                    while True:
                        length = int((await reader.readline()).split(b";", 1)[0], 16)
                        if length == 0:
                            assert await reader.readline() == b"\r\n"
                            break
                        parts.append(await reader.readexactly(length))
                        assert await reader.readexactly(2) == b"\r\n"
                    body = b"".join(parts)
                else:
                    body = await reader.readexactly(int(values.get(b"content-length", b"0")))
                request = SimpleNamespace(method=method, target=target, headers=headers, body=body)
                backend.requests.append(request)
                backend.received.set()
                if handler:
                    await handler(request, writer)
                else:
                    await respond(writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                tasks.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        backend.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        return backend

    yield make
    for server in servers:
        server.close()
    pending = tuple(tasks)
    for task in pending:
        task.cancel()
    async with asyncio.timeout(3):
        await asyncio.gather(*pending, return_exceptions=True)
        for server in servers:
            await server.wait_closed()


@pytest.fixture
async def runtime_factory():
    instances = []

    async def make(
        v1,
        v2,
        *,
        route_values=None,
        budget_values=None,
        work_values=None,
        collection_values=None,
        store_wrapper=None,
        runtime_values=None,
        context=True,
    ):
        budgets = dict(
            serving_timeout_seconds=1,
            shadow_timeout_seconds=1,
            client_send_timeout_seconds=2,
            shutdown_grace_seconds=1,
            serving_max_inflight=8,
            shadow_max_inflight=8,
            request_capture_limit_bytes=4096,
            response_capture_limit_bytes=4096,
        )
        budgets.update(budget_values or {})
        route = dict(
            route_id="catalog",
            method="GET",
            path_template="/catalog/{item}",
            v1=v1.url,
            v2=v2.url,
            owner="test-owner",
            rollout_enabled=True,
            v2_serve_ratio=0,
            cohort=Cohort("request", "catalog", "request", "test-salt"),
            shadow=ShadowPolicy(True, 1, "synthetic-safe-fixture"),
            contract=ResponseContract(frozenset({200}), frozenset({404})),
            comparison_policy_revision="compare-1",
        )
        route.update(route_values or {})
        snapshot = Snapshot(
            1,
            "config-1",
            None,
            "synthetic fixture",
            v1.url,
            frozenset({v1.url, v2.url}),
            (Route(**route),),
            RuntimeBudgets(**budgets),
        )
        config = ConfigManager(snapshot)
        store = SQLiteEventStore(":memory:")
        collection = dict(
            max_events=32,
            max_bytes=1_000_000,
            max_age_seconds=2,
            batch_size=8,
            write_timeout_seconds=1,
            max_attempts=2,
            retry_delay_seconds=0,
        )
        collection.update(collection_values or {})
        collector = BoundedCollector(
            store_wrapper(store) if store_wrapper else store,
            CollectionLimits(**collection),
        )
        metrics = Metrics({"catalog", "unregistered"}, reason_codes=DEFAULT_REASONS | REASON_CODES)
        policy = ComparisonPolicy("compare-1", 4096, 8192, 16, 1024, 64, 128, 32, 16)
        work = dict(
            compare_max_jobs=32,
            compare_max_bytes=1_000_000,
            compare_max_age_seconds=2,
            compare_workers=1,
            compare_timeout_seconds=1,
            event_retention_seconds=60,
            environment="test",
            migration_id="test",
            epoch_id="epoch-1",
        )
        work.update(work_values or {})
        runtime_options = {
            "context_provider": (lambda *_: ComparisonContext(True, True, True))
            if context
            else None,
        }
        runtime_options.update(runtime_values or {})
        runtime = ProxyRuntime(
            config,
            comparison_policies={policy.revision: policy},
            work_limits=WorkLimits(**work),
            collector=collector,
            metrics=metrics,
            **runtime_options,
        )
        state = SimpleNamespace(runtime=runtime, closed=False)
        instances.append(state)
        await runtime.start()

        async def close():
            await runtime.close()
            state.closed = True

        async def records():
            now = time.time()
            rows = await store.query(
                EventQuery(now - 100, now + 1, frozenset({"catalog"}), 100),
                QueryAccess(frozenset({"catalog"}), 100, 200),
            )
            return [row["summary"] for row in rows]

        return SimpleNamespace(
            runtime=runtime,
            store=store,
            metrics=metrics,
            config=config,
            collector=collector,
            records=records,
            close=close,
        )

    yield make
    for state in reversed(instances):
        if not state.closed:
            async with asyncio.timeout(3):
                await state.runtime.close()


@dataclass
class Exchange:
    task: asyncio.Task
    incoming: asyncio.Queue
    messages: list[dict] = field(default_factory=list)

    @property
    def body(self):
        return b"".join(message.get("body", b"") for message in self.messages)

    @property
    def status(self):
        return next(message["status"] for message in self.messages if "status" in message)

    async def wait(self):
        await asyncio.wait_for(self.task, 3)
        return self

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})


@pytest.fixture
async def asgi_request():
    exchanges = []

    def make(
        runtime,
        *,
        path=b"/catalog/1",
        query=b"",
        method="GET",
        headers=(),
        chunks=(b"",),
        on_send=None,
    ):
        incoming, messages = asyncio.Queue(), []
        for index, chunk in enumerate(chunks):
            incoming.put_nowait(
                {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            )

        async def send(message):
            if on_send:
                await on_send(message)
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(path.decode("ascii")),
            "raw_path": path,
            "query_string": query,
            "headers": [(b"host", b"proxy.local"), *headers],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 8000),
        }
        exchange = Exchange(
            asyncio.create_task(runtime(scope, incoming.get, send)), incoming, messages
        )
        exchanges.append(exchange)
        return exchange

    yield make
    for exchange in exchanges:
        if not exchange.task.done():
            exchange.disconnect()
    await asyncio.gather(*(exchange.task for exchange in exchanges), return_exceptions=True)
