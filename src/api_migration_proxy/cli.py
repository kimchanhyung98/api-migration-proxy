from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import uvicorn

from api_migration_proxy.app import create_app
from api_migration_proxy.collection import BoundedCollector, CollectionLimits, SQLiteEventStore
from api_migration_proxy.comparison import REASON_CODES, ComparisonPolicy, FieldMapping, Tolerance
from api_migration_proxy.config import ConfigManager, ConfigurationError, Snapshot
from api_migration_proxy.observability import DEFAULT_REASONS, Metrics
from api_migration_proxy.runtime import ProxyRuntime, WorkLimits


@dataclass(frozen=True)
class Settings:
    snapshot: Snapshot
    comparison_policies: Mapping[str, ComparisonPolicy]
    work_limits: WorkLimits
    collection_limits: CollectionLimits


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate configuration key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ConfigurationError("configuration numbers must be finite")


def _comparison_policy(value: object) -> ComparisonPolicy:
    if not isinstance(value, dict):
        raise ConfigurationError("comparison policy must be an object")
    data = dict(value)
    for key in ("required_headers", "ignore_paths"):
        if key in data:
            if not isinstance(data[key], list):
                raise ConfigurationError("comparison rules must be arrays")
            data[key] = tuple(data[key])
    if "allowed_diff_paths" in data:
        if not isinstance(data["allowed_diff_paths"], list):
            raise ConfigurationError("comparison paths must be an array")
        data["allowed_diff_paths"] = frozenset(data["allowed_diff_paths"])
    data["mappings"] = tuple(FieldMapping(**item) for item in data.get("mappings", []))
    tolerances = []
    for item in data.get("tolerances", []):
        rule = dict(item)
        rule["absolute"] = Decimal(str(rule["absolute"]))
        tolerances.append(Tolerance(**rule))
    data["tolerances"] = tuple(tolerances)
    data["status_equivalences"] = tuple(tuple(pair) for pair in data.get("status_equivalences", []))
    return ComparisonPolicy(**data)


def load_settings(path: str | Path) -> Settings:
    try:
        data = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(data, dict) or set(data) != {
            "snapshot",
            "comparison_policies",
            "work_limits",
            "collection_limits",
        }:
            raise ConfigurationError("configuration sections are incomplete or unknown")
        snapshot = Snapshot.from_dict(data["snapshot"])
        if not isinstance(data["comparison_policies"], list):
            raise ConfigurationError("comparison policies must be an array")
        policies: dict[str, ComparisonPolicy] = {}
        for value in data["comparison_policies"]:
            policy = _comparison_policy(value)
            if policy.revision in policies:
                raise ConfigurationError("duplicate comparison policy revision")
            policies[policy.revision] = policy
        for route in snapshot.routes:
            if (
                route.comparison_policy_revision
                and route.comparison_policy_revision not in policies
            ):
                raise ConfigurationError("route comparison policy is missing")
        settings = Settings(
            snapshot,
            MappingProxyType(policies),
            WorkLimits(**data["work_limits"]),
            CollectionLimits(**data["collection_limits"]),
        )
        _metrics(settings)
        return settings
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        InvalidOperation,
        OverflowError,
    ) as error:
        raise ConfigurationError("configuration could not be loaded or validated") from error


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _metrics(settings: Settings) -> Metrics:
    return Metrics(
        {route.route_id for route in settings.snapshot.routes} | {"unregistered"},
        reason_codes=DEFAULT_REASONS | REASON_CODES,
    )


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("batch size must be a positive SQLite integer") from None
    if not 0 < size <= 2**63 - 1:
        raise argparse.ArgumentTypeError("batch size must be a positive SQLite integer")
    return size


async def _purge_events(path: str, batch_size: int) -> dict[str, int]:
    store = SQLiteEventStore(path, create=False)
    try:
        return await store.purge_expired(batch_size=batch_size)
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTTP API migration proxy")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check-config", help="validate a configuration without network access"
    )
    check.add_argument("--config", required=True)
    serve = commands.add_parser("serve", help="run a single-process local proxy")
    serve.add_argument("--config", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port, required=True)
    serve.add_argument("--event-store", required=True, help="local SQLite event file")
    purge = commands.add_parser(
        "purge-events", help="remove one batch of expired data from an existing local event store"
    )
    purge.add_argument("--event-store", required=True, help="existing local SQLite event file")
    purge.add_argument(
        "--batch-size",
        type=_batch_size,
        required=True,
        help="maximum detail rows and summary rows to remove, each; not a time limit",
    )
    args = parser.parse_args(argv)
    if args.command == "purge-events":
        try:
            result = asyncio.run(_purge_events(args.event_store, args.batch_size))
        except (OSError, ValueError, sqlite3.Error):
            print("Event cleanup failed; verify the store and retry.", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        settings = load_settings(args.config)
    except ConfigurationError:
        print("Configuration could not be loaded or validated.", file=sys.stderr)
        return 2
    if args.command == "check-config":
        print("Configuration valid.")
        return 0
    store = SQLiteEventStore(args.event_store)
    collector = BoundedCollector(store, settings.collection_limits)
    metrics = _metrics(settings)
    runtime = ProxyRuntime(
        ConfigManager(settings.snapshot),
        comparison_policies=settings.comparison_policies,
        work_limits=settings.work_limits,
        collector=collector,
        metrics=metrics,
    )
    uvicorn.run(
        create_app(runtime),
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
