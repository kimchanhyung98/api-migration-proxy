"""격리된 Docker Compose 환경에서 단계별 마이그레이션 검증."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scenarios.distribution import evaluate, validate_parameters
from scenarios.observation import evaluate_observations


@dataclass(frozen=True)
class Stage:
    """요청 수, v2 비율 및 shadow 상태로 정의한 검증 단계."""

    name: str
    requests: int
    v2_ratio: float
    shadow_enabled: bool = False
    tolerance: float = 0.08


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _health(execute) -> dict:
    query = (
        "import json, os, urllib.request; "
        "request = urllib.request.Request('http://127.0.0.1:9090/healthcheck', "
        "headers={'Authorization': 'Bearer ' + os.environ['API_PROXY_CONTROL_TOKEN']}); "
        "response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5); "
        "health = json.load(response); response.close(); "
        "config = json.load(open('/config/proxy.json')); "
        "print(json.dumps({'health': health, "
        "'configuration_revision': config['snapshot']['revision']}))"
    )
    return json.loads(
        execute(
            ["exec", "-T", "proxy", "python", "-c", query], stdout=subprocess.PIPE, timeout=30
        ).stdout
    )


def _backends(execute) -> dict:
    query = (
        "import urllib.request; "
        "response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open("
        "'http://127.0.0.1:8000/__demo/requests', timeout=5); "
        "print(response.read().decode()); response.close()"
    )
    return {
        backend: json.loads(
            execute(
                ["exec", "-T", backend, "python", "-c", query], stdout=subprocess.PIPE, timeout=30
            ).stdout
        )
        for backend in ("v1", "v2")
    }


def _stored_events(execute) -> list:
    query = (
        "import json, sqlite3; from pathlib import Path; "
        "db = sqlite3.connect('file:/data/events.sqlite?mode=ro', uri=True) "
        "if Path('/data/events.sqlite').exists() else None; "
        "rows = db.execute('SELECT event_id, summary FROM comparison_event ORDER BY event_id')"
        ".fetchall() if db else []; "
        "print(json.dumps([{'event_id': key, 'summary': json.loads(value)} for key, value in rows])); "
        "db.close() if db else None"
    )
    return json.loads(
        execute(
            ["run", "--rm", "--no-deps", "test", "python", "-c", query],
            stdout=subprocess.PIPE,
            timeout=30,
        ).stdout
    )


def _summarize(scenario: str, validations: list[dict], failures: list[str]) -> str:
    lines = [f"## Demo {scenario} scenario", "", f"- Result: {'FAIL' if failures else 'PASS'}"]
    for result in validations:
        expected, observed = result["expected"], result["observed"]
        lines.extend(
            [
                "",
                f"### {result['stage']}: {'PASS' if result['passed'] else 'FAIL'}",
                "",
                f"- Requests: {expected['requests']}",
                f"- Expected v2 share: {expected['v2_ratio']:.1%}",
                f"- Shadow: {'ON' if expected['shadow_enabled'] else 'OFF'}",
            ]
        )
        if observed is not None:
            lines.extend(
                [
                    f"- Backend counts: {observed['backend_counts']}",
                    f"- HTTP status counts: {observed['status_counts']}",
                    f"- Transport errors: {observed['transport_errors']}",
                    f"- Accepted v2 count: {expected['v2_count_min']}–{expected['v2_count_max']}",
                    f"- Backend request deltas: {result['runtime']['observed']['backend_requests']}",
                    f"- Stored events: {result['runtime']['observed']['event_count']}",
                    f"- Retained events: {result['runtime']['observed']['retained_event_count']}",
                ]
            )
        lines.extend(f"- Failure: {failure}" for failure in result["failures"])
    if failures:
        lines.extend(["", "### Run failures", ""])
        lines.extend(f"- {failure}" for failure in failures)
    lines.extend(
        ["", "Normal CLI comparison context is unconfigured; shadow pairs remain not_comparable."]
    )
    return "\n".join(lines) + "\n"


def run_stages(scenario: str, stages: tuple[Stage, ...], *, distribution: bool = False) -> int:
    """단계별 설정 적용·요청·종료·저장 검증과 결과 보관.

    Args:
        scenario: 실행 및 결과 식별에 사용할 시나리오 이름.
        stages: 순서대로 실행할 검증 단계.
        distribution: 단일 분배 검증용 결과 형식 사용 여부.

    Returns:
        전체 검증과 자체 Compose 자원 정리 성공 시 0, 실패 시 1.

    Raises:
        ValueError: 단계별 요청 수·비율·허용 오차 오류.
    """
    for stage in stages:
        validate_parameters(stage.requests, stage.v2_ratio, stage.tolerance)
    demo = Path(__file__).resolve().parents[1]
    results = demo / "results" / ("distribution" if distribution else "scenarios")
    results.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=results))
    project = f"proxy-{scenario}-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    environment = os.environ | {
        "V1_PORT": "0",
        "V2_PORT": "0",
        "PROXY_PORT": "0",
        "DEMO_CONTROL_TOKEN": token,
        "DEMO_PATH": "/items/different",
        "DEMO_TIMEOUT": "5",
    }
    compose = ["docker", "compose", "--project-name", project, "--file", str(demo / "compose.yml")]
    prepared = []
    previous_revision = None
    for stage in stages:
        stage_output = output if distribution else output / stage.name
        stage_output.mkdir(exist_ok=True)
        config = json.loads((demo / "config" / "docker.json").read_text())
        revision = f"{project}-{stage.name}"
        config["snapshot"].update(
            revision=revision,
            previous_revision=previous_revision,
            change_reason=f"Synthetic {scenario} scenario: {stage.name}",
        )
        route = next(
            route for route in config["snapshot"]["routes"] if route["route_id"] == "synthetic_item"
        )
        route.update(rollout_enabled=True, v2_serve_ratio=stage.v2_ratio)
        route["cohort"].update(mode="request", key_source="request")
        route["shadow"].update(sample_ratio=1, stopped=not stage.shadow_enabled)
        _write_json(stage_output / "config.json", config)
        prepared.append((stage, stage_output, revision))
        previous_revision = revision
    environment["PROXY_CONFIG"] = str(prepared[0][1] / "config.json")
    validations: list[dict] = []
    failures: list[str] = []
    previous_events: list = []
    print(f"Scenario artifacts: {output.relative_to(demo)}", flush=True)
    with (output / "execution.log").open("w") as execution:
        execution.write(f"Compose project: {project}\n")
        execution.flush()

        def execute(
            arguments: list[str],
            *,
            stdout: Any = execution,
            timeout: int = 900,
            docker: bool = False,
        ):
            displayed = arguments[:5] if arguments[0] == "exec" else arguments
            prefix = ["docker"] if docker else compose
            command = "+ " + ("docker " if docker else "docker compose ") + " ".join(displayed)
            print(command, flush=True)
            execution.write(command + "\n")
            execution.flush()
            return subprocess.run(
                prefix + arguments,
                cwd=demo,
                env=environment,
                stdout=stdout,
                stderr=execution,
                check=True,
                timeout=timeout,
                text=True,
            )

        try:
            execute(["build", "v1", "v2", "proxy", "user", "test"])
            for index, (stage, stage_output, revision) in enumerate(prepared):
                result: dict[str, Any] = {
                    "stage": stage.name,
                    "passed": False,
                    "failures": ["Stage did not complete"],
                    "expected": {
                        "requests": stage.requests,
                        "v2_ratio": stage.v2_ratio,
                        "shadow_enabled": stage.shadow_enabled,
                        "revision": revision,
                    },
                    "observed": None,
                }
                validations.append(result)
                try:
                    environment.update(
                        PROXY_CONFIG=str(stage_output / "config.json"),
                        DEMO_REQUESTS=str(stage.requests),
                    )
                    execute(
                        [
                            "run",
                            "--rm",
                            "--no-deps",
                            "proxy",
                            "check-config",
                            "--config",
                            "/config/proxy.json",
                        ]
                    )
                    arguments = ["up", "-d", "--wait", "--wait-timeout", "60"]
                    arguments += (
                        ["v1", "v2", "proxy"]
                        if index == 0
                        else ["--no-deps", "--force-recreate", "proxy"]
                    )
                    execute(arguments)
                    observation = _health(execute)
                    if observation != {
                        "health": {"ready": True},
                        "configuration_revision": revision,
                    }:
                        raise ValueError("Health or mounted configuration did not match this stage")
                    observation["backends"] = {"before": _backends(execute)}
                    _write_json(stage_output / "observation.json", observation)
                    with (stage_output / "report.json").open("w") as report_file:
                        execute(
                            ["run", "--rm", "--no-deps", "user"], stdout=report_file, timeout=600
                        )
                    report = json.loads((stage_output / "report.json").read_text())
                    # 프록시 종료로 비교·수집 작업을 정리한 뒤 저장 결과 확인.
                    execute(["stop", "--timeout", "15", "proxy"], timeout=30)
                    container_id = execute(
                        ["ps", "--all", "--quiet", "proxy"], stdout=subprocess.PIPE, timeout=30
                    ).stdout.strip()
                    state = json.loads(
                        execute(
                            ["inspect", "--format", "{{json .State}}", container_id],
                            stdout=subprocess.PIPE,
                            timeout=30,
                            docker=True,
                        ).stdout
                    )
                    logs = execute(
                        ["logs", "--no-color", "--timestamps", "proxy"],
                        stdout=subprocess.PIPE,
                        timeout=30,
                    ).stdout
                    (stage_output / "proxy.log").write_text(logs)
                    observation["shutdown"] = {
                        "status": state["Status"],
                        "exit_code": state["ExitCode"],
                        "oom_killed": state["OOMKilled"],
                        "application_shutdown_complete": "Application shutdown complete." in logs,
                    }
                    observation["backends"]["after"] = _backends(execute)
                    _write_json(stage_output / "observation.json", observation)
                    stored_events = _stored_events(execute)
                    _write_json(stage_output / "stored-events.json", stored_events)
                    events = [
                        event
                        for event in stored_events
                        if event["summary"]["configuration_revision"] == revision
                    ]
                    _write_json(stage_output / "events.json", events)
                    distribution_result = evaluate(
                        report,
                        requests=stage.requests,
                        v2_ratio=stage.v2_ratio,
                        tolerance=stage.tolerance,
                    )
                    runtime = evaluate_observations(
                        observation,
                        stored_events,
                        revision=revision,
                        backend_counts=report["backend_versions"]["counts"],
                        shadow_enabled=stage.shadow_enabled,
                        previous_events=previous_events,
                    )
                    result.update(distribution_result, runtime=runtime)
                    result["expected"].update(
                        shadow_enabled=stage.shadow_enabled, revision=revision
                    )
                    result["failures"].extend(runtime["failures"])
                    result["passed"] = not result["failures"]
                    if not runtime["passed"]:
                        break
                    previous_events = stored_events
                finally:
                    _write_json(stage_output / "validation.json", result)
        except (OSError, subprocess.SubprocessError, ValueError, KeyboardInterrupt) as error:
            failures.append(f"Scenario execution failed: {type(error).__name__}: {error}")
        finally:
            with (output / "containers.log").open("w") as container_logs:
                try:
                    execute(
                        ["logs", "--no-color", "--timestamps"], stdout=container_logs, timeout=30
                    )
                except (OSError, subprocess.SubprocessError):
                    failures.append("Container log collection failed")
            try:
                execute(["down", "--volumes", "--remove-orphans", "--timeout", "15"], timeout=60)
            except (OSError, subprocess.SubprocessError):
                failures.append("Owned Compose project cleanup failed")
            if len(validations) != len(stages):
                failures.append(f"Only {len(validations)} of {len(stages)} stages ran")
            failures.extend(
                f"{result['stage']}: {failure}"
                for result in validations
                for failure in result["failures"]
            )
            validation = {
                "scenario": scenario,
                "passed": not failures,
                "failures": failures,
                "stages": validations,
            }
            if distribution and validations:
                validation = dict(validations[0], passed=not failures, failures=failures)
            _write_json(output / "validation.json", validation)
            summary = _summarize(scenario, validations, failures)
            (output / "summary.md").write_text(summary)
            print(summary)
    return 0 if not failures else 1


def handle_termination() -> None:
    """SIGTERM을 KeyboardInterrupt로 변환하여 기존 종료 정리 경로 실행."""

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
