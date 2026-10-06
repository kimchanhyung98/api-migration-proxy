from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from api_migration_proxy.environment import RunOptions
from api_migration_proxy.observability.metrics import Metrics
from api_migration_proxy.server import run_server


class RuntimeDouble:
    def __init__(self, *, fail_start=False):
        self.config = SimpleNamespace(
            current=SimpleNamespace(
                revision="listener-test",
                routes=(),
                budgets=SimpleNamespace(shutdown_grace_seconds=1),
            )
        )
        self.ready = False
        self.started = 0
        self.closed = 0
        self.requests = []
        self.metrics = Metrics({"registered"})
        self.fail_start = fail_start

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("synthetic startup failure")
        self.ready = True

    async def close(self):
        self.closed += 1
        self.ready = False

    def observation_status(self):
        return {"scope": "current_process"}

    def metric_snapshot(self):
        return self.metrics.snapshot()

    async def __call__(self, scope, receive, send):
        self.requests.append(scope["path"])
        self.metrics.increment("proxy_assignments_total", route="registered", serving="v1")
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"data backend"})


@pytest.fixture
def options():
    with socket.socket() as data, socket.socket() as control:
        data.bind(("127.0.0.1", 0))
        control.bind(("127.0.0.1", 0))
        data_port, control_port = data.getsockname()[1], control.getsockname()[1]
    return RunOptions(
        host="127.0.0.1",
        port=data_port,
        event_store=":memory:",
        control_enabled=True,
        control_host="127.0.0.1",
        control_port=control_port,
        control_token="synthetic-control-token",
    )


@contextmanager
def running_server(runtime, options, monkeypatch):
    instances, results, errors = [], [], []
    server_class = uvicorn.Server

    def create_server(*args, **kwargs):
        server = server_class(*args, **kwargs)
        instances.append(server)
        return server

    def run():
        try:
            results.append(run_server(runtime, options))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr("api_migration_proxy.server.uvicorn.Server", create_server)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        for port in (options.port, options.control_port):
            while True:
                assert thread.is_alive(), f"server exited before startup: {errors!r}"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, "listener startup deadline exceeded"
                    time.sleep(0.01)
        yield
    finally:
        for server in instances:
            server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "server shutdown deadline exceeded"
        assert errors == []
        assert results == [0]


def test_real_listeners_separate_control_auth_and_ignore_host_spoofing(options, monkeypatch):
    runtime = RuntimeDouble()
    paths = ("/healthcheck", "/health/live", "/health/ready", "/metrics", "/state")
    with running_server(runtime, options, monkeypatch), httpx.Client(trust_env=False) as client:
        data_url = f"http://127.0.0.1:{options.port}"
        control_url = f"http://127.0.0.1:{options.control_port}"
        for path in paths:
            response = client.get(
                data_url + path,
                headers={
                    "Host": f"127.0.0.1:{options.control_port}",
                    "X-Forwarded-Host": f"127.0.0.1:{options.control_port}",
                    "Forwarded": f"host=127.0.0.1:{options.control_port};for=127.0.0.1",
                },
            )
            assert response.status_code == 201
            assert response.content == b"data backend"
            for authorization in (None, "Bearer wrong-token"):
                headers = {"Host": f"127.0.0.1:{options.port}"}
                if authorization is not None:
                    headers["Authorization"] = authorization
                assert client.get(control_url + path, headers=headers).status_code == (
                    403 if path == "/healthcheck" else 404
                )
        headers = {
            "Authorization": f"Bearer {options.control_token}",
            "Host": f"127.0.0.1:{options.port}",
        }
        assert client.get(control_url + "/healthcheck", headers=headers).json() == {
            "ready": True,
        }
        for path in paths[1:]:
            assert client.get(control_url + path, headers=headers).status_code == 404
        assert (
            runtime.metrics.value("proxy_assignments_total", route="registered", serving="v1") == 5
        )
        assert runtime.requests == list(paths)
        assert runtime.started == 1
        assert runtime.closed == 0
    assert runtime.closed == 1
    assert runtime.ready is False


def test_healthcheck_tracks_readiness_and_keeps_internal_counters_private(options, monkeypatch):
    runtime = RuntimeDouble()
    with running_server(runtime, options, monkeypatch), httpx.Client(trust_env=False) as client:
        data_url = f"http://127.0.0.1:{options.port}"
        control_url = f"http://127.0.0.1:{options.control_port}"
        headers = {"Authorization": f"Bearer {options.control_token}"}
        for path in ("/metrics", "/state"):
            response = client.get(data_url + path)
            assert response.status_code == 201
            assert response.content == b"data backend"
            assert client.get(control_url + path).status_code == 404
            assert client.get(control_url + path, headers=headers).status_code == 404
        healthy = client.get(control_url + "/healthcheck", headers=headers)
        assert healthy.status_code == 200
        assert healthy.json() == {"ready": True}
        runtime.ready = False
        unavailable = client.get(control_url + "/healthcheck", headers=headers)
        assert unavailable.status_code == 503
        assert unavailable.json() == {"ready": False}
        assert (
            runtime.metrics.value("proxy_assignments_total", route="registered", serving="v1") == 2
        )
    assert runtime.closed == 1


def test_control_bind_failure_releases_data_port_without_starting_runtime(options):
    runtime = RuntimeDouble()
    with socket.create_server(("127.0.0.1", options.control_port)):
        try:
            result = run_server(runtime, options)
        except SystemExit as error:
            result = error.code
        assert result != 0
        with socket.socket() as released:
            released.bind(("127.0.0.1", options.port))
    assert runtime.started == 0
    assert runtime.closed == 0


def test_runtime_start_failure_closes_once_and_releases_both_ports(options):
    runtime = RuntimeDouble(fail_start=True)
    try:
        result = run_server(runtime, options)
    except SystemExit as error:
        result = error.code
    assert result == 3
    assert runtime.started == 1
    assert runtime.closed == 1
    for port in (options.port, options.control_port):
        with socket.socket() as released:
            released.bind(("127.0.0.1", port))


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_cli_signals_stop_both_listeners(options, tmp_path, shutdown_signal):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("API_PROXY_")
    }
    environment.update(
        API_PROXY_EXPOSE_OBSERVABILITY="true",
        API_PROXY_CONTROL_ENABLED="true",
        API_PROXY_CONTROL_HOST=options.control_host,
        API_PROXY_CONTROL_PORT=str(options.control_port),
        API_PROXY_CONTROL_TOKEN=options.control_token,
    )
    config = Path(__file__).parents[1] / "fixtures/proxy.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "api_migration_proxy.cli",
            "serve",
            "--config",
            str(config),
            "--host",
            options.host,
            "--port",
            str(options.port),
            "--event-store",
            str(tmp_path / "events.sqlite"),
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False, timeout=0.2) as client:
            while True:
                assert process.poll() is None, "proxy exited before control listener became ready"
                try:
                    response = client.get(
                        f"http://127.0.0.1:{options.control_port}/healthcheck",
                        headers={"Authorization": f"Bearer {options.control_token}"},
                    )
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "control readiness deadline exceeded"
                time.sleep(0.02)
            for path in ("/health/live", "/health/ready", "/metrics", "/state"):
                removed = client.get(
                    f"http://127.0.0.1:{options.control_port}{path}",
                    headers={"Authorization": f"Bearer {options.control_token}"},
                )
                assert removed.status_code == 404
        assert response.json()["ready"] is True
        with socket.create_connection(("127.0.0.1", options.port), timeout=1):
            pass
        process.send_signal(shutdown_signal)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode in (0, -shutdown_signal)
        assert "Application shutdown complete." in stderr
        assert "Traceback" not in stderr
        assert options.control_token not in stdout + stderr
        for port in (options.port, options.control_port):
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
