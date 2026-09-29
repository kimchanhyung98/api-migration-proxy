import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.distribution import evaluate
from user.cli import main, run


@pytest.fixture
def server_factory():
    servers = []

    def make(replies):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                index = len(seen)
                seen.append((self.path, dict(self.headers)))
                reply = replies[index % len(replies)]
                if reply is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, version, *extra = reply
                self.send_response(status)
                if version is not None:
                    self.send_header("x-backend-version", version)
                for name, value in extra[0] if extra else ():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", seen

    yield make
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mixed_versions_and_http_errors_use_response_denominator(server_factory):
    origin, seen = server_factory([(200, "v1"), (404, "v2"), (500, None), (200, "other")])
    result = run(origin, "/items/different?key=one&key=two", requests=4, timeout=1)
    assert result["attempts"] == result["responses"] == 4
    assert result["transport_errors"] == 0
    assert result["backend_versions"] == {
        "denominator": "responses",
        "counts": {"v1": 1, "v2": 1, "unknown": 2},
        "ratios": {"v1": 0.25, "v2": 0.25, "unknown": 0.5},
    }
    assert result["status_counts"] == {"200": 2, "404": 1, "500": 1}
    assert result["status_classes"] == {"1xx": 0, "2xx": 2, "3xx": 0, "4xx": 1, "5xx": 1}
    assert [path for path, _ in seen] == ["/items/different?key=one&key=two"] * 4
    assert all("authorization" not in {key.lower() for key in headers} for _, headers in seen)
    latency = result["latency_ms"]
    assert latency["count"] == 4
    assert 0 <= latency["min"] <= latency["avg"] <= latency["max"]
    assert result["elapsed_seconds"] > 0


def test_transport_failure_does_not_dilute_observed_backend_ratios(server_factory):
    origin, seen = server_factory([(200, "v1"), None, (503, "v2")])
    result = run(origin, requests=3, timeout=1)
    assert len(seen) == result["attempts"] == 3
    assert result["responses"] == 2
    assert result["transport_errors"] == 1
    assert result["backend_versions"]["ratios"] == {"v1": 0.5, "v2": 0.5, "unknown": 0}
    assert result["status_counts"] == {"200": 1, "503": 1}
    assert result["latency_ms"]["count"] == 3
    assert result["latency_ms"]["denominator"] == "attempts_including_transport_errors"


def test_redirect_is_an_observed_response_and_never_follows_another_origin(server_factory):
    target, target_seen = server_factory([(200, "v2")])
    proxy, proxy_seen = server_factory([(302, "v1", (("Location", target + "/items/other"),))])
    result = run(proxy, requests=1)
    assert len(proxy_seen) == 1
    assert target_seen == []
    assert result["responses"] == 1
    assert result["status_counts"] == {"302": 1}
    assert result["status_classes"]["3xx"] == 1
    assert result["backend_versions"]["counts"] == {"v1": 1, "v2": 0, "unknown": 0}


def test_refused_connections_report_no_observed_distribution():
    with contextlib.closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        result = run(origin, requests=2, timeout=0.5)
    assert result["attempts"] == result["transport_errors"] == 2
    assert result["responses"] == 0
    assert result["backend_versions"]["counts"] == {"v1": 0, "v2": 0, "unknown": 0}
    assert result["backend_versions"]["ratios"] == {"v1": None, "v2": None, "unknown": None}
    assert result["status_counts"] == {}


def test_cli_always_prints_report_and_optional_output_matches(server_factory, capsys, tmp_path):
    origin, _ = server_factory([(200, "v1")])
    output = tmp_path / "report.json"
    assert main(["--url", origin, "--requests", "2", "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads(output.read_text())
    assert report["responses"] == 2


def test_output_write_failure_keeps_json_stdout(server_factory, capsys, tmp_path):
    origin, _ = server_factory([(200, "v1")])
    with pytest.raises(SystemExit) as raised:
        main(["--url", origin, "--requests", "1", "--output", str(tmp_path / "absent" / "report")])
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["responses"] == 1
    assert "Could not write output file" in captured.err


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@127.0.0.1",
        "http://@127.0.0.1",
        "ftp://127.0.0.1",
        "http://127.0.0.1/base",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#section",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:0",
        "http://",
        " http://127.0.0.1",
        "http://127.0.0.1\n",
        "http://127.0.0.1\\@other",
    ],
)
def test_invalid_origin_is_rejected_before_any_request(url):
    with pytest.raises(ValueError, match="origin"):
        run(url, requests=1)


@pytest.mark.parametrize(
    "path",
    [
        "//other/items",
        "https://other/items",
        "items",
        "/\\other",
        "/items\n",
        "/items#fragment",
        "",
        "/items with spaces",
    ],
)
def test_only_origin_relative_paths_are_accepted(path):
    with pytest.raises(ValueError, match="origin-relative"):
        run("http://127.0.0.1", path, requests=1)


@pytest.mark.parametrize(
    "values",
    [
        {"requests": 0},
        {"requests": -1},
        {"requests": True},
        {"requests": 1.5},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
    ],
)
def test_invalid_run_limits_fail_before_network_access(values):
    with pytest.raises(ValueError):
        run("http://127.0.0.1", **values)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--url", "http://127.0.0.1", "--requests", "0"],
        ["--url", "http://127.0.0.1", "--requests", "1.5"],
        ["--url", "http://127.0.0.1", "--timeout", "nan"],
        ["--url", "http://127.0.0.1", "--timeout", "inf"],
    ],
)
def test_cli_rejects_invalid_arguments(arguments):
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("replies", "passed"),
    [
        ([(200, "v1"), (200, "v2")], True),
        ([(200, "v1")], False),
        ([(200, "v1"), (500, "v2")], False),
        ([(200, "v1"), None], False),
    ],
)
def test_distribution_gate_checks_actual_user_http_report(server_factory, replies, passed):
    origin, seen = server_factory(replies)
    result = evaluate(run(origin, requests=20), requests=20, v2_ratio=0.5, tolerance=0.08)
    assert len(seen) == 20
    assert result["passed"] is passed
