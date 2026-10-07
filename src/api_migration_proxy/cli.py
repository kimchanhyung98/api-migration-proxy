"""설정 검사, 프록시 실행 및 만료 이벤트 삭제 명령."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from api_migration_proxy.collection.collector import BoundedCollector
from api_migration_proxy.collection.postgresql import PostgreSQLEventStore
from api_migration_proxy.collection.retention import RetentionWorker
from api_migration_proxy.collection.sqlite import SQLiteEventStore
from api_migration_proxy.environment import (
    RunOptions,
    load_proxy_settings,
    load_run_options,
    read_environment,
)
from api_migration_proxy.proxy.runtime import ProxyRuntime
from api_migration_proxy.routing.configuration import ConfigManager, ConfigurationError
from api_migration_proxy.server import run_server
from api_migration_proxy.settings import Settings, metrics_from_settings


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("batch size must be a positive 64-bit integer") from None
    if not 0 < size <= 2**63 - 1:
        raise argparse.ArgumentTypeError("batch size must be a positive 64-bit integer")
    return size


def _event_store(
    options: RunOptions, *, create: bool = True
) -> SQLiteEventStore | PostgreSQLEventStore:
    if options.event_store_backend == "postgresql":
        return PostgreSQLEventStore(options.postgres_dsn, create=create)
    return SQLiteEventStore(options.event_store, create=create)


async def _purge_events(options: RunOptions, batch_size: int) -> dict[str, int]:
    store = _event_store(options, create=False)
    try:
        return await store.purge_expired(batch_size=batch_size)
    finally:
        await store.close()


def _explain(settings: Settings, options: RunOptions, *, json_mode: bool) -> dict:
    """백엔드 연결 검사 없이 설정상 동작과 런타임 제약 요약."""
    routes = []
    for route in settings.snapshot.routes:
        request_cohort = route.cohort.mode == "request"
        routes.append(
            {
                "route_id": route.route_id,
                "cohort_mode": route.cohort.mode,
                "rollout_enabled": route.rollout_enabled,
                "v2_serve_ratio": route.v2_serve_ratio,
                "effective_v2_serve_ratio": route.v2_serve_ratio
                if request_cohort and route.rollout_enabled
                else 0,
                "shadow_sample_ratio": route.shadow.sample_ratio,
                "shadow_enabled": bool(
                    request_cohort
                    and route.rollout_enabled
                    and route.shadow.eligible
                    and not route.shadow.stopped
                    and route.shadow.sample_ratio > 0
                ),
                "warnings": []
                if request_cohort
                else ["No trusted identity provider: v1 serving and no shadow for this route."],
            }
        )
    return {
        "scope": "configuration_only",
        "mode": "json" if json_mode else "environment",
        "runtime": {
            "event_store_backend": options.event_store_backend,
            "retention_interval_seconds": options.retention_interval_seconds,
            "retention_batch_size": options.retention_batch_size,
        },
        "routes": routes,
        "capabilities": {
            "identity_provider": False,
            "comparison_context_provider": False,
            "detail_provider": False,
        },
        "warnings": [
            "No comparison context provider: authorization and data equivalence are unverified.",
            "Configuration validity does not establish backend availability or rollout readiness.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 인자와 환경 설정에 따른 명령 실행.

    Args:
        argv: 명령 인자 목록. None이면 프로세스 인자 사용.

    Returns:
        정상 종료 시 0, 설정·실행·삭제 실패 시 2, 제어 리스너 모드의 서버 시작 실패 시 3.
    """
    parser = argparse.ArgumentParser(description="HTTP API migration proxy")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check-config", help="validate a configuration without network access"
    )
    check.add_argument("--config")
    check.add_argument("--env-file", default=".env")
    check.add_argument(
        "--explain", action="store_true", help="show safe CLI capability diagnostics"
    )
    serve = commands.add_parser("serve", help="run a single-process local proxy")
    serve.add_argument("--config")
    serve.add_argument("--env-file", default=".env")
    serve.add_argument("--host")
    serve.add_argument("--port", type=_port)
    serve.add_argument("--event-store", help="local SQLite event file")
    purge = commands.add_parser(
        "purge-events", help="remove one batch of expired data from an existing event store"
    )
    purge.add_argument("--env-file", default=".env")
    purge.add_argument("--event-store", help="existing local SQLite event file; selects SQLite")
    purge.add_argument(
        "--batch-size",
        type=_batch_size,
        required=True,
        help="maximum detail rows and summary rows to remove, each; not a time limit",
    )
    args = parser.parse_args(argv)
    if args.command == "purge-events":
        try:
            environment = read_environment(args.env_file)
            storage_environment = {
                name: value
                for name, value in environment.items()
                if name
                in {
                    "API_PROXY_EVENT_STORE_BACKEND",
                    "API_PROXY_EVENT_STORE",
                    "API_PROXY_POSTGRES_DSN",
                }
            }
            if args.event_store is not None:
                storage_environment.update(
                    API_PROXY_EVENT_STORE_BACKEND="sqlite", API_PROXY_EVENT_STORE=args.event_store
                )
            options = load_run_options(storage_environment)
            result = asyncio.run(_purge_events(options, args.batch_size))
        except Exception:
            print("Event cleanup failed; verify the store and retry.", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        environment = read_environment(args.env_file)
        for name in ("config", "host", "port", "event_store"):
            value = getattr(args, name, None)
            if value is not None:
                environment[f"API_PROXY_{name.upper()}"] = str(value)
        if getattr(args, "event_store", None) is not None:
            environment["API_PROXY_EVENT_STORE_BACKEND"] = "sqlite"
        options = load_run_options(environment)
        settings = load_proxy_settings(environment)
    except ConfigurationError:
        print("Configuration could not be loaded or validated.", file=sys.stderr)
        return 2
    if args.command == "check-config":
        print("Configuration valid.")
        if args.explain:
            print(
                json.dumps(
                    _explain(settings, options, json_mode=bool(environment.get("API_PROXY_CONFIG")))
                )
            )
        return 0
    try:
        store = _event_store(options)
        collector = BoundedCollector(store, settings.collection_limits)
        metrics = metrics_from_settings(settings)
        maintenance = RetentionWorker(
            store,
            interval_seconds=options.retention_interval_seconds,
            batch_size=options.retention_batch_size,
        )
        runtime = ProxyRuntime(
            ConfigManager(settings.snapshot),
            comparison_policies=settings.comparison_policies,
            work_limits=settings.work_limits,
            collector=collector,
            metrics=metrics,
            maintenance=maintenance,
        )
        return run_server(runtime, options)
    except Exception:
        print("Proxy execution failed; verify configuration and retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
