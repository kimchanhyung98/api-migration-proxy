from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys

import uvicorn

from api_migration_proxy.app import create_app
from api_migration_proxy.collection.collector import BoundedCollector
from api_migration_proxy.collection.sqlite import SQLiteEventStore
from api_migration_proxy.proxy.runtime import ProxyRuntime
from api_migration_proxy.routing.configuration import ConfigManager, ConfigurationError
from api_migration_proxy.settings import load_settings, metrics_from_settings


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
    metrics = metrics_from_settings(settings)
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
