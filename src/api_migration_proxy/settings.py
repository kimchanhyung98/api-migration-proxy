from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from api_migration_proxy.collection.collector import CollectionLimits
from api_migration_proxy.comparison.engine import (
    REASON_CODES,
    ComparisonPolicy,
    FieldMapping,
    Tolerance,
)
from api_migration_proxy.observability.metrics import DEFAULT_REASONS, Metrics
from api_migration_proxy.proxy.pipeline import WorkLimits
from api_migration_proxy.routing.configuration import ConfigurationError, Snapshot


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
        metrics_from_settings(settings)
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


def metrics_from_settings(settings: Settings) -> Metrics:
    return Metrics(
        {route.route_id for route in settings.snapshot.routes} | {"unregistered"},
        reason_codes=DEFAULT_REASONS | REASON_CODES,
    )
