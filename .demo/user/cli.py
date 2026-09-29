from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def _origin(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("url must be an HTTP(S) origin without credentials")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or parsed.port > 0)
            and "\\" not in value
        )
        if not valid:
            raise ValueError
        normalized = httpx.URL(value)
        if not normalized.host:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise ValueError("url must be an HTTP(S) origin without credentials") from None
    return str(normalized).rstrip("/")


def _path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("path must be an origin-relative path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("path must be an origin-relative path")
    return value


def _positive_int(value: str) -> int:
    try:
        result = int(value)
        if result <= 0:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None


def _positive_float(value: str) -> float:
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("must be positive and finite") from None


def run(url: str, path: str = "/items/different", requests: int = 100, timeout: float = 5) -> dict:
    origin, path = _origin(url), _path(path)
    if type(requests) is not int or requests <= 0:
        raise ValueError("requests must be a positive integer")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be positive and finite")

    versions = {"v1": 0, "v2": 0, "unknown": 0}
    status_classes = {f"{code}xx": 0 for code in range(1, 6)}
    status_counts: dict[str, int] = {}
    latencies = []
    transport_errors = 0
    started = time.perf_counter()
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(requests):
            attempt_started = time.perf_counter()
            try:
                response = client.get(origin + path)
            except httpx.RequestError:
                transport_errors += 1
            else:
                version = response.headers.get("x-backend-version", "unknown")
                versions[version if version in {"v1", "v2"} else "unknown"] += 1
                status = str(response.status_code)
                status_counts[status] = status_counts.get(status, 0) + 1
                category = f"{response.status_code // 100}xx"
                status_classes[category] = status_classes.get(category, 0) + 1
            finally:
                latencies.append((time.perf_counter() - attempt_started) * 1000)
    responses = sum(versions.values())
    return {
        "attempts": requests,
        "responses": responses,
        "transport_errors": transport_errors,
        "status_counts": status_counts,
        "status_classes": status_classes,
        "backend_versions": {
            "denominator": "responses",
            "counts": versions,
            "ratios": {
                version: count / responses if responses else None
                for version, count in versions.items()
            },
        },
        "latency_ms": {
            "denominator": "attempts_including_transport_errors",
            "count": len(latencies),
            "min": min(latencies),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
        },
        "elapsed_seconds": time.perf_counter() - started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send sequential requests to the migration proxy.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--path", default="/items/different")
    parser.add_argument("--requests", type=_positive_int, default=100)
    parser.add_argument("--timeout", type=_positive_float, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run(args.url, args.path, args.requests, args.timeout)
    except ValueError as error:
        parser.error(str(error))
    serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    print(serialized)
    if args.output is not None:
        try:
            args.output.write_text(serialized + "\n", encoding="utf-8")
        except OSError:
            parser.exit(1, "Could not write output file.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
