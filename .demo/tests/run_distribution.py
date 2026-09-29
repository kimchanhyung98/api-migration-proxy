import argparse
import json
import os
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from tests.distribution import evaluate, validate_parameters


def run(*, requests: int, v2_ratio: float, tolerance: float) -> int:
    validate_parameters(requests, v2_ratio, tolerance)
    demo = Path(__file__).resolve().parents[1]
    results = demo / "results" / "distribution"
    results.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=results))
    project = f"proxy-distribution-{uuid.uuid4().hex}"
    config = json.loads((demo / "config" / "docker.json").read_text())
    snapshot = config["snapshot"]
    snapshot.update(
        revision=project,
        previous_revision=None,
        change_reason="Synthetic serving distribution validation",
    )
    route = next(route for route in snapshot["routes"] if route["route_id"] == "synthetic_item")
    route["rollout_enabled"] = True
    route["v2_serve_ratio"] = v2_ratio
    route["cohort"]["mode"] = "request"
    route["cohort"]["key_source"] = "request"
    route["shadow"]["stopped"] = True
    config_path = output / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    environment = os.environ | {
        "PROXY_CONFIG": str(config_path),
        "V1_PORT": "0",
        "V2_PORT": "0",
        "PROXY_PORT": "0",
        "DEMO_PATH": "/items/different",
        "DEMO_REQUESTS": str(requests),
        "DEMO_TIMEOUT": "5",
    }
    compose = ["docker", "compose", "--project-name", project, "--file", str(demo / "compose.yml")]
    validation: dict[str, Any] = {
        "passed": False,
        "failures": ["Distribution scenario did not complete"],
        "expected": {"requests": requests, "v2_ratio": v2_ratio, "tolerance": tolerance},
        "observed": None,
    }
    print(f"Distribution artifacts: {output.relative_to(demo)}", flush=True)
    with (output / "execution.log").open("w") as execution:
        execution.write(f"Compose project: {project}\n")
        execution.flush()

        def execute(arguments: list[str], *, stdout: Any = execution, timeout: int = 900) -> None:
            print("+ docker compose " + " ".join(arguments), flush=True)
            subprocess.run(
                compose + arguments,
                cwd=demo,
                env=environment,
                stdout=stdout,
                stderr=execution,
                check=True,
                timeout=timeout,
            )

        try:
            execute(["build", "v1", "v2", "proxy", "user"])
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
            execute(["up", "-d", "--wait", "--wait-timeout", "60", "v1", "v2", "proxy"])
            with (output / "report.json").open("w") as report_file:
                execute(["run", "--rm", "--no-deps", "user"], stdout=report_file, timeout=600)
            report = json.loads((output / "report.json").read_text())
            validation = evaluate(report, requests=requests, v2_ratio=v2_ratio, tolerance=tolerance)
        except (OSError, subprocess.SubprocessError, ValueError, KeyboardInterrupt) as error:
            validation["failures"] = [f"Scenario execution failed: {type(error).__name__}"]
        finally:
            with (output / "containers.log").open("w") as container_logs:
                try:
                    execute(
                        ["logs", "--no-color", "--timestamps"], stdout=container_logs, timeout=30
                    )
                except (OSError, subprocess.SubprocessError):
                    validation["passed"] = False
                    validation["failures"].append("Container log collection failed")
            try:
                execute(["down", "--volumes", "--remove-orphans", "--timeout", "15"], timeout=60)
            except (OSError, subprocess.SubprocessError):
                validation["passed"] = False
                validation["failures"].append("Owned Compose project cleanup failed")
            (output / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
            summary = summarize(validation)
            (output / "summary.md").write_text(summary)
            print(summary)
    return 0 if validation["passed"] else 1


def summarize(validation: dict[str, Any]) -> str:
    expected = validation["expected"]
    lines = [
        "## Demo serving distribution",
        "",
        f"- Result: {'PASS' if validation['passed'] else 'FAIL'}",
        f"- Requests: {expected['requests']}",
        f"- Expected v2 share: {expected['v2_ratio']:.1%}",
        f"- Tolerance: ±{expected['tolerance'] * 100:g} percentage points",
        "- Shadow: stopped; counts describe User-visible responses",
    ]
    observed = validation["observed"]
    if observed is not None:
        lines.extend(
            [
                f"- Backend counts: {observed['backend_counts']}",
                f"- HTTP status counts: {observed['status_counts']}",
                f"- Transport errors: {observed['transport_errors']}",
            ]
        )
        if observed["v2_ratio"] is not None:
            lines.append(f"- Observed v2 share: {observed['v2_ratio']:.1%}")
        lines.append(
            f"- Accepted v2 count: {expected['v2_count_min']}–{expected['v2_count_max']} (inclusive)"
        )
    lines.extend(f"- Failure: {failure}" for failure in validation["failures"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an isolated Docker serving distribution check"
    )
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--v2-ratio", type=float, default=0.5)
    parser.add_argument("--tolerance", type=float, default=0.08)
    args = parser.parse_args()
    try:
        validate_parameters(args.requests, args.v2_ratio, args.tolerance)
    except ValueError as error:
        parser.error(str(error))

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    return run(requests=args.requests, v2_ratio=args.v2_ratio, tolerance=args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
