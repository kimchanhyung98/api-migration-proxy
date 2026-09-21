from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import httpx

from .collection import BoundedCollector, DetailPolicy
from .comparison import BackendResponse, ComparisonContext, ComparisonPolicy
from .config import ConfigManager, ConfigurationError
from .observability import Metrics, MetricSnapshot
from .processing import ComparisonPipeline
from .processing import WorkLimits as WorkLimits
from .routing import backend_url, choose_serving, match_route
from .transport import BackendTransport, forwarding_headers


def _utc() -> str:
    return datetime.now(UTC).isoformat()


class ClientDisconnected(Exception):
    pass


class IncompleteResponse(Exception):
    """The response has started and must be terminated without a replacement body."""


class _Input:
    def __init__(self, receive: Callable):
        self.receive = receive
        self.done = asyncio.Event()
        self.disconnected = False
        self.prefix: deque[bytes] = deque()

    async def chunk(self) -> bytes:
        message = await self.receive()
        if message["type"] == "http.disconnect":
            self.disconnected = True
            self.done.set()
            raise ClientDisconnected()
        if not message.get("more_body", False):
            self.done.set()
        return message.get("body", b"")

    async def capture(self, limit: int) -> bytes | None:
        size = 0
        while not self.done.is_set():
            part = await self.chunk()
            if part:
                self.prefix.append(part)
            size += len(part)
            if size > limit:
                return None
        return b"".join(self.prefix)

    async def stream(self):
        while self.prefix:
            yield self.prefix.popleft()
        while not self.done.is_set():
            yield await self.chunk()


async def _body(data: bytes):
    if data:
        yield data


@dataclass
class _Attempt:
    backend: str
    role: str
    outcome: str = "not_dispatched"
    contract_class: str = "unknown"
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    capture: bytearray = field(default_factory=bytearray, repr=False)
    capture_state: str = "unavailable"
    received: int = 0
    complete: bool = False
    duration: float | None = None
    ended_at: str | None = None
    reason: str = "none"
    started: bool = False

    def response(self) -> BackendResponse:
        return BackendResponse(
            self.backend,
            self.role,
            self.outcome,
            self.contract_class,
            self.status,
            self.headers,
            bytes(self.capture) if self.capture_state == "complete" else None,
            self.capture_state,
            self.complete,
        )

    def summary(self) -> dict:
        return {
            "backend": self.backend,
            "role": self.role,
            "deployment_revision": "unknown",
            "execution_outcome": self.outcome,
            "contract_class": self.contract_class,
            "status_code": self.status,
            "duration_ms": None if self.duration is None else self.duration * 1000,
            "response_bytes": self.received,
            "response_complete": self.complete,
            "capture_state": self.capture_state,
            "reason": self.reason,
        }


class ProxyRuntime:
    def __init__(
        self,
        config: ConfigManager,
        *,
        comparison_policies: Mapping[str, ComparisonPolicy],
        work_limits: WorkLimits,
        collector: BoundedCollector,
        metrics: Metrics,
        context_provider: Callable | None = None,
        response_classifier: Callable | None = None,
        identity_provider: Callable | None = None,
        detail_policy: DetailPolicy | None = None,
        detail_provider: Callable | None = None,
    ):
        self.config, self.policies = config, MappingProxyType(dict(comparison_policies))
        self.work_limits, self.collector, self.metrics = work_limits, collector, metrics
        self.context_provider = context_provider
        self.response_classifier = response_classifier
        self.identity_provider = identity_provider
        self._comparison = ComparisonPipeline(
            work_limits,
            collector,
            metrics,
            detail_policy=detail_policy,
            detail_provider=detail_provider,
        )
        self._active: set[asyncio.Task] = set()
        self._pairs: set[asyncio.Task] = set()
        self._shadow_slots = 0
        self._accepting = False
        self._close_task: asyncio.Task | None = None
        self._transports: dict[str, BackendTransport] = {}
        self._initial_budgets: Any = None
        config.add_validator(self._validate_snapshot)
        previous_outcome = getattr(collector, "on_outcome", None)

        def on_outcome(event, outcome):
            if outcome == "stored":
                self.metrics.increment(
                    "comparison_pipeline_total", route=event.summary["route_id"], step="stored"
                )
            elif outcome.startswith("dropped_"):
                self._drop(
                    {
                        "dropped_expired": "queue_expired",
                        "dropped_retry_exhausted": "retry_exhausted",
                        "dropped_shutdown": "shutdown",
                    }.get(outcome, "storage_failure")
                )
            if previous_outcome:
                previous_outcome(event, outcome)

        collector.on_outcome = on_outcome

    def _validate_snapshot(self, snapshot):
        if self._initial_budgets is not None and snapshot.budgets != self._initial_budgets:
            raise ConfigurationError("changing process resource budgets requires a restart")
        for route in snapshot.routes:
            try:
                self.metrics.value("proxy_assignments_total", route=route.route_id, serving="v1")
            except ValueError:
                raise ConfigurationError(
                    "register new route metrics during process startup"
                ) from None
            if route.rollout_enabled and route.shadow.eligible:
                if route.comparison_policy_revision not in self.policies:
                    raise ConfigurationError("active comparison policy is missing")

    @property
    def ready(self) -> bool:
        return self._accepting and self.config.current is not None

    def observation_status(self) -> dict:
        return {
            "worker_revisions": dict(self.config.worker_status),
            "collection": self.collector.metrics(),
            "storage_maintenance": dict(getattr(self.collector.store, "counters", {})),
            "active_requests": len(self._active),
            "pending_pairs": len(self._pairs),
            **self._comparison.status(),
            "ready": self.ready,
            "scope": "current_process",
        }

    def metric_snapshot(self) -> MetricSnapshot:
        snapshot = self.metrics.snapshot()
        collection = self.collector.metrics()
        counters, gauges = dict(snapshot.counters), dict(snapshot.gauges)
        counters[("collection_write_failures_total", ())] = int(collection.get("write_failures", 0))
        gauges[("collection_queue_depth", ())] = float(collection["queue_depth"])
        gauges[("collection_oldest_age_seconds", ())] = float(collection["oldest_age_seconds"])
        maintenance = getattr(self.collector.store, "counters", {})
        if "expired_pending" in maintenance:
            gauges[("collection_expired_pending", ())] = float(maintenance["expired_pending"])
        return MetricSnapshot(counters, gauges, snapshot.histograms)

    async def start(self) -> None:
        if self._close_task is not None:
            raise RuntimeError("runtime is closed")
        if self._accepting:
            return
        snapshot = self.config.current
        if snapshot is None:
            raise ConfigurationError("a validated initial snapshot is required")
        self._validate_snapshot(snapshot)
        budgets = snapshot.budgets
        self._initial_budgets = budgets
        self._transports = {
            "serving": BackendTransport(
                budgets.serving_timeout_seconds, budgets.serving_max_inflight
            ),
            "shadow": BackendTransport(budgets.shadow_timeout_seconds, budgets.shadow_max_inflight),
        }
        self._comparison.start()
        self._accepting = True

    def _drop(self, reason: str) -> None:
        self.metrics.increment("collection_dropped_total", reason=reason)

    async def flush(self):
        if self._pairs:
            await asyncio.gather(*tuple(self._pairs))
        await self._comparison.flush()
        await self.collector.flush()

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        self._accepting = False
        if self._initial_budgets is None:
            return
        grace = self._initial_budgets.shutdown_grace_seconds
        deadline = time.monotonic() + grace
        for group in (self._active, self._pairs):
            pending = set(group)
            if pending:
                _, pending = await asyncio.wait(
                    pending, timeout=max(0, deadline - time.monotonic())
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        await self._comparison.close(deadline)
        await self.collector.close(max(0.001, deadline - time.monotonic()))
        for transport in self._transports.values():
            await transport.aclose()
        close_store = getattr(self.collector.store, "close", None)
        if close_store:
            try:
                await asyncio.wait_for(close_store(), max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                self.collector.completeness_known = False

    async def _attempt(self, attempt, snapshot, match, scope, body, selected, messages=None):
        route_id = match.route.route_id if match else "unregistered"
        role = attempt.role
        budgets = snapshot.budgets
        timeout = (
            budgets.serving_timeout_seconds if role == "serving" else budgets.shadow_timeout_seconds
        )
        started = time.monotonic()
        response = None
        cancelled = False
        labels = {"route": route_id, "backend": attempt.backend, "role": role}
        self.metrics.increment("backend_started_total", **labels)
        attempt.started = True
        if role == "shadow":
            self.metrics.increment("comparison_pipeline_total", route=route_id, step="dispatched")
        self.metrics.adjust_gauge("backend_inflight", 1, backend=attempt.backend, role=role)
        attempt.capture_state = "complete" if selected else "not_needed"
        try:
            async with asyncio.timeout(timeout):
                url = backend_url(
                    snapshot,
                    match,
                    attempt.backend,
                    scope.get("raw_path", scope["path"].encode()),
                    scope.get("query_string", b""),
                )
                response = await self._transports[role].open(
                    scope["method"], url, scope["headers"], body
                )
                attempt.status = response.status_code
                attempt.headers = tuple(
                    (key.decode("latin1"), value.decode("latin1"))
                    for key, value in response.headers.raw
                )
                if messages is not None:
                    await messages.put(
                        (
                            "headers",
                            (response.status_code, forwarding_headers(response.headers.raw)),
                        )
                    )
                async for chunk in response.aiter_raw():
                    attempt.received += len(chunk)
                    if selected and attempt.capture_state == "complete":
                        if (
                            len(attempt.capture) + len(chunk)
                            <= budgets.response_capture_limit_bytes
                        ):
                            attempt.capture.extend(chunk)
                        else:
                            attempt.capture.clear()
                            attempt.capture_state = "oversized"
                    if (
                        messages is not None
                        and scope["method"] != "HEAD"
                        and response.status_code not in {204, 304}
                    ):
                        await messages.put(("body", chunk))
                attempt.complete = True
                attempt.outcome = "http_response"
                if match:
                    attempt.contract_class = match.route.contract.classify(
                        attempt.status, body_complete=True
                    )
                    if self.response_classifier and attempt.contract_class != "unexpected_error":
                        try:
                            classification = self.response_classifier(
                                match.route, attempt.response()
                            )
                            attempt.contract_class = (
                                classification
                                if classification
                                in {"success", "expected_rejection", "unexpected_error", "unknown"}
                                else "unknown"
                            )
                        except Exception:
                            attempt.contract_class = "unknown"
        except (TimeoutError, httpx.TimeoutException):
            attempt.outcome = attempt.reason = "timeout"
        except (asyncio.CancelledError, ClientDisconnected):
            attempt.outcome = attempt.reason = "cancelled"
            cancelled = True
        except Exception:
            attempt.outcome = attempt.reason = "transport_error"
        finally:
            try:
                if response is not None:
                    async with asyncio.timeout(max(0, timeout - (time.monotonic() - started))):
                        await response.aclose()
            except asyncio.CancelledError:
                attempt.outcome = attempt.reason = "cancelled"
                cancelled = True
            except Exception:
                if attempt.outcome == "http_response":
                    attempt.outcome = attempt.reason = "transport_error"
            attempt.duration = time.monotonic() - started
            attempt.ended_at = _utc()
            if not attempt.complete and attempt.capture_state == "complete":
                attempt.capture_state = "unavailable"
                attempt.capture.clear()
            self.metrics.adjust_gauge("backend_inflight", -1, backend=attempt.backend, role=role)
            self.metrics.increment(
                "backend_completed_total",
                **labels,
                outcome=attempt.outcome,
                contract_class=attempt.contract_class,
            )
            self.metrics.observe("backend_duration_seconds", attempt.duration, **labels)
        if messages is not None and not cancelled:
            await messages.put(("end", attempt.outcome))

    async def _error(self, send, status, timeout=None):
        if timeout is None:
            snapshot = self.config.current
            if snapshot is None:
                raise IncompleteResponse("no validated response time budget")
            timeout = snapshot.budgets.client_send_timeout_seconds
        body = b'{"detail":"proxy request failed"}'
        async with asyncio.timeout(timeout):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _reject(self, snapshot, scope, send):
        started = time.monotonic()
        match = match_route(snapshot, scope["method"], scope["path"]) if snapshot else None
        route_id = match.route.route_id if match else "unregistered"
        outcome = "rejected"
        try:
            await self._error(send, 503)
        except TimeoutError:
            outcome = "timeout"
            raise IncompleteResponse("proxy error response deadline exceeded") from None
        except (OSError, asyncio.CancelledError):
            outcome = "cancelled"
        finally:
            self.metrics.increment(
                "proxy_requests_total",
                route=route_id,
                serving="unknown",
                outcome=outcome,
                source="proxy",
            )
            self.metrics.observe(
                "proxy_request_duration_seconds",
                time.monotonic() - started,
                route=route_id,
                serving="unknown",
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        snapshot = self.config.current
        if not self.ready or snapshot is None:
            await self._reject(snapshot, scope, send)
            return
        if len(self._active) >= snapshot.budgets.serving_max_inflight:
            await self._reject(snapshot, scope, send)
            return
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        try:
            await self._request(snapshot, scope, receive, send)
        finally:
            self._active.discard(task)

    async def _request(self, snapshot, scope, receive, send):
        started, started_at, request_id = time.monotonic(), _utc(), str(uuid.uuid4())
        match = match_route(snapshot, scope["method"], scope["path"])
        route = match.route if match else None
        route_id = route.route_id if route else "unregistered"
        identity = self.identity_provider(scope) if self.identity_provider else {}
        assignment = (
            choose_serving(route, trusted_identity=identity, request_key=request_id)
            if route
            else None
        )
        serving = _Attempt(assignment.backend if assignment else "v1", "serving")
        shadow = _Attempt("v2" if serving.backend == "v1" else "v1", "shadow")
        self.metrics.increment("proxy_assignments_total", route=route_id, serving=serving.backend)
        eligible = bool(
            route
            and route.rollout_enabled
            and route.shadow.eligible
            and assignment
            and assignment.reason != "missing_cohort_key"
        )
        if eligible:
            self.metrics.increment("comparison_pipeline_total", route=route_id, step="eligible")
        selected = bool(
            route
            and eligible
            and not route.shadow.stopped
            and secrets.randbelow(2**53) / 2**53 < route.shadow.sample_ratio
        )
        self.metrics.increment(
            "shadow_decisions_total",
            route=route_id,
            decision="selected" if selected else "sampled_out" if eligible else "ineligible",
        )
        if selected:
            self.metrics.increment("comparison_pipeline_total", route=route_id, step="selected")
        source = _Input(receive)
        shadow_task = None
        producer = None
        watcher = None
        sender = None
        reserved = False
        sent_headers = sent_complete = False
        user_outcome, response_source = "cancelled", "none"
        messages: asyncio.Queue = asyncio.Queue(1)

        async def watch_disconnect():
            await source.done.wait()
            if source.disconnected:
                return
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return

        async def deliver():
            nonlocal sent_headers, sent_complete, user_outcome, response_source
            async with asyncio.timeout(snapshot.budgets.client_send_timeout_seconds):
                while True:
                    kind, value = await messages.get()
                    if kind == "headers":
                        await send(
                            {"type": "http.response.start", "status": value[0], "headers": value[1]}
                        )
                        sent_headers, response_source = True, "backend"
                    elif kind == "body":
                        await send({"type": "http.response.body", "body": value, "more_body": True})
                    elif value == "http_response":
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                        sent_complete, user_outcome = True, "completed"
                        return
                    elif not sent_headers:
                        response_source = "proxy"
                        user_outcome = "timeout" if value == "timeout" else "transport_error"
                        await self._error(send, 504 if value == "timeout" else 502)
                        sent_complete = True
                        return
                    else:
                        user_outcome = "incomplete"
                        raise IncompleteResponse("upstream response was incomplete")

        try:
            if selected:
                if self._shadow_slots >= snapshot.budgets.shadow_max_inflight:
                    shadow.reason = "slot_exhausted"
                else:
                    self._shadow_slots += 1
                    reserved = True
                    async with asyncio.timeout(snapshot.budgets.serving_timeout_seconds):
                        captured = await source.capture(
                            snapshot.budgets.request_capture_limit_bytes
                        )
                    if captured is None:
                        shadow.reason = "request_oversized"
                    else:
                        shadow_task = asyncio.create_task(
                            self._attempt(shadow, snapshot, match, scope, _body(captured), True)
                        )
                if shadow_task is None:
                    self.metrics.increment(
                        "shadow_not_dispatched_total", route=route_id, reason=shadow.reason
                    )
            producer = asyncio.create_task(
                self._attempt(serving, snapshot, match, scope, source.stream(), selected, messages)
            )
            watcher = asyncio.create_task(watch_disconnect())
            sender = asyncio.create_task(deliver())
            done, _ = await asyncio.wait({sender, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender
            elif not sent_complete:
                raise ClientDisconnected()
        except (ClientDisconnected, OSError, asyncio.CancelledError):
            user_outcome = "cancelled"
            if shadow_task:
                shadow_task.cancel()
        except TimeoutError:
            user_outcome = "timeout"
            if not sent_headers:
                response_source = "proxy"
                await self._error(send, 504)
            else:
                raise IncompleteResponse("client response deadline exceeded") from None
        finally:
            for child in (watcher, sender, producer):
                if child and not child.done():
                    child.cancel()
            await asyncio.gather(
                *(child for child in (watcher, sender, producer) if child), return_exceptions=True
            )
            self.metrics.increment(
                "proxy_requests_total",
                route=route_id,
                serving=serving.backend,
                outcome=user_outcome,
                source=response_source,
            )
            self.metrics.observe(
                "proxy_request_duration_seconds",
                time.monotonic() - started,
                route=route_id,
                serving=serving.backend,
            )
            if selected:
                assert route is not None and route.comparison_policy_revision is not None
                policy = self.policies[route.comparison_policy_revision]
                epoch = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        json.dumps(
                            [
                                self.work_limits.epoch_id,
                                snapshot.revision,
                                policy.revision,
                            ]
                        ),
                    )
                )
                summary = {
                    "route_id": route_id,
                    "request_id": request_id,
                    "environment": self.work_limits.environment,
                    "migration_id": self.work_limits.migration_id,
                    "epoch_id": epoch,
                    "configuration_revision": snapshot.revision,
                    "comparison_policy_revision": route.comparison_policy_revision,
                    "request_started_at": started_at,
                    "request_ended_at": _utc(),
                    "serving_backend": serving.backend,
                    "response_source": response_source,
                    "request_outcome": user_outcome,
                    "shadow_selected": True,
                    "shadow_dispatched": shadow.started,
                    "assignment_method": route.cohort.mode,
                }

                async def finalize():
                    try:
                        if shadow_task:
                            try:
                                await shadow_task
                            except asyncio.CancelledError:
                                shadow.reason = "cancelled"
                        summary["shadow_dispatched"] = shadow.started
                        if shadow.started:
                            self.metrics.increment(
                                "comparison_pipeline_total", route=route_id, step="terminal"
                            )
                        attempts = {serving.backend: serving, shadow.backend: shadow}
                        summary["backends"] = {
                            key: value.summary() for key, value in attempts.items()
                        }
                        summary["backend_completed_at"] = max(
                            (value.ended_at for value in attempts.values() if value.ended_at),
                            default=_utc(),
                        )
                        context = (
                            self.context_provider(
                                route, attempts["v1"].response(), attempts["v2"].response()
                            )
                            if self.context_provider
                            else ComparisonContext(logical_request_equal=True)
                        )
                        self._comparison.submit(
                            summary,
                            attempts["v1"].response(),
                            attempts["v2"].response(),
                            policy,
                            context,
                        )
                    except Exception:
                        self._drop("comparison_dropped")
                    finally:
                        if reserved:
                            self._shadow_slots -= 1

                pair = asyncio.create_task(finalize())
                self._pairs.add(pair)
                pair.add_done_callback(self._pairs.discard)
