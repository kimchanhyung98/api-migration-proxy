"""환경 변수 로딩과 기본 실행·라우팅 설정 구성."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from dotenv.parser import parse_stream

from api_migration_proxy.collection.collector import CollectionLimits
from api_migration_proxy.comparison.engine import ComparisonPolicy
from api_migration_proxy.proxy.pipeline import WorkLimits
from api_migration_proxy.routing.configuration import (
    Cohort,
    ConfigurationError,
    ResponseContract,
    Route,
    RuntimeBudgets,
    ShadowPolicy,
    Snapshot,
    backend_origin,
)
from api_migration_proxy.settings import Settings, load_settings


def read_environment(path: str | Path = ".env") -> dict[str, str]:
    """파일과 프로세스에서 API_PROXY_ 설정 병합.

    Args:
        path: 환경 파일 경로. 파일이 없으면 프로세스 환경만 사용.

    Returns:
        프로세스 환경 변수를 우선 적용한 설정 사전.

    Raises:
        ConfigurationError: 환경 파일의 구문·명시적 값·인코딩 오류 또는 읽기 실패.
    """
    values: dict[str, str] = {}
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise ConfigurationError("environment file contains invalid syntax")
                if binding.key and binding.key.startswith("API_PROXY_"):
                    if binding.value is None:
                        raise ConfigurationError("environment settings require explicit values")
                    values[binding.key] = binding.value
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError("environment file could not be loaded or validated") from None
    values.update({key: value for key, value in os.environ.items() if key.startswith("API_PROXY_")})
    return values


def _boolean(value: str) -> bool:
    if value not in {"true", "false"}:
        raise ConfigurationError("environment switches must be true or false")
    return value == "true"


def _port(value: str) -> int:
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except ValueError:
        raise ConfigurationError("listener ports must be integers between 1 and 65535") from None


def _ratio(value: str) -> float:
    try:
        ratio = float(value)
    except ValueError:
        raise ConfigurationError("v2 ratio must be a finite number between 0 and 1") from None
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ConfigurationError("v2 ratio must be a finite number between 0 and 1")
    return ratio


def _retention_interval(value: str) -> float:
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        raise ConfigurationError("retention interval must be positive and finite") from None
    return seconds


def _retention_batch_size(value: str) -> int:
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        return int(value)
    except ValueError:
        raise ConfigurationError("retention batch size must be a positive 64-bit integer") from None


@dataclass(frozen=True)
class RunOptions:
    """리스너, 이벤트 저장소 및 보존 작업 실행 옵션."""

    host: str
    port: int
    event_store: str
    control_enabled: bool
    control_host: str
    control_port: int
    control_token: str = field(repr=False)
    event_store_backend: str = "sqlite"
    postgres_dsn: str = field(default="", repr=False)
    retention_interval_seconds: float = 60
    retention_batch_size: int = 1000

    def __post_init__(self) -> None:
        for host in (self.host, self.control_host):
            if not host or any(
                ord(char) <= 32 or ord(char) > 126 or char in "/?#\\@" for char in host
            ):
                raise ConfigurationError("listener hosts must be nonempty addresses")
        if any(
            type(port) is not int or not 1 <= port <= 65535
            for port in (self.port, self.control_port)
        ):
            raise ConfigurationError("listener ports must be integers between 1 and 65535")
        if self.event_store_backend not in ("sqlite", "postgresql"):
            raise ConfigurationError("event store backend must be sqlite or postgresql")
        if self.event_store_backend == "sqlite":
            if not self.event_store.strip() or "\x00" in self.event_store:
                raise ConfigurationError("event store must be a nonempty file path")
        elif (
            not isinstance(self.postgres_dsn, str)
            or not self.postgres_dsn.strip()
            or "\x00" in self.postgres_dsn
        ):
            raise ConfigurationError("PostgreSQL requires a nonempty DSN without NUL characters")
        if type(self.control_enabled) is not bool:
            raise ConfigurationError("control switch must be boolean")
        try:
            valid_interval = (
                not isinstance(self.retention_interval_seconds, bool)
                and isinstance(self.retention_interval_seconds, (int, float))
                and math.isfinite(self.retention_interval_seconds)
                and self.retention_interval_seconds > 0
            )
        except OverflowError:
            valid_interval = False
        if not valid_interval:
            raise ConfigurationError("retention interval must be positive and finite")
        if (
            type(self.retention_batch_size) is not int
            or not 1 <= self.retention_batch_size <= 2**63 - 1
        ):
            raise ConfigurationError("retention batch size must be a positive 64-bit integer")
        if any(ord(char) <= 32 or ord(char) > 126 for char in self.control_token):
            raise ConfigurationError("control token must contain only non-whitespace ASCII")
        if self.control_enabled:
            if self.port == self.control_port:
                raise ConfigurationError("public and control listener ports must differ")
            try:
                loopback = ipaddress.ip_address(self.control_host).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ConfigurationError("control listener requires a literal loopback address")


def load_run_options(environment: Mapping[str, str]) -> RunOptions:
    """환경 설정에서 검증된 실행 옵션 생성.

    Args:
        environment: API_PROXY_ 접두사의 환경 설정.

    Returns:
        리스너·저장소·보존 작업 실행 옵션.

    Raises:
        ConfigurationError: 옵션 형식 또는 허용 범위 위반.
    """
    return RunOptions(
        host=environment.get("API_PROXY_HOST", "127.0.0.1"),
        port=_port(environment.get("API_PROXY_PORT", "8080")),
        event_store=environment.get("API_PROXY_EVENT_STORE", "events.sqlite"),
        control_enabled=_boolean(environment.get("API_PROXY_CONTROL_ENABLED", "true")),
        control_host=environment.get("API_PROXY_CONTROL_HOST", "127.0.0.1"),
        control_port=_port(environment.get("API_PROXY_CONTROL_PORT", "9090")),
        control_token=environment.get("API_PROXY_CONTROL_TOKEN", ""),
        event_store_backend=environment.get("API_PROXY_EVENT_STORE_BACKEND", "sqlite"),
        postgres_dsn=environment.get("API_PROXY_POSTGRES_DSN", ""),
        retention_interval_seconds=_retention_interval(
            environment.get("API_PROXY_RETENTION_INTERVAL_SECONDS", "60")
        ),
        retention_batch_size=_retention_batch_size(
            environment.get("API_PROXY_RETENTION_BATCH_SIZE", "1000")
        ),
    )


def load_proxy_settings(environment: Mapping[str, str]) -> Settings:
    """지정 JSON 또는 환경 변수로 프록시 설정 생성.

    Args:
        environment: API_PROXY_CONFIG와 기본 프록시 환경 설정.

    Returns:
        라우팅·비교·수집 정책을 포함한 설정.

    Raises:
        ConfigurationError: 설정 로딩 실패 또는 필수 라우트·shadow 검토 근거 누락.
    """
    if config := environment.get("API_PROXY_CONFIG", ""):
        try:
            return load_settings(config)
        except ConfigurationError:
            raise ConfigurationError("configuration could not be loaded or validated") from None

    try:
        v1 = backend_origin(environment.get("API_PROXY_V1_URL", "http://127.0.0.1:8001"))
        v2 = backend_origin(environment.get("API_PROXY_V2_URL", "http://127.0.0.1:8002"))
    except ConfigurationError:
        raise ConfigurationError("backend origins could not be validated") from None
    path = environment.get("API_PROXY_ROUTE_PATH", "")
    ratio = _ratio(environment.get("API_PROXY_V2_RATIO", "0"))
    shadow = _boolean(environment.get("API_PROXY_SHADOW_ENABLED", "false"))
    review_ref = environment.get("API_PROXY_SHADOW_REVIEW_REF", "") or None
    if not path and (ratio > 0 or shadow):
        raise ConfigurationError("v2 serving and shadow require an explicit route path")
    if shadow and (review_ref is None or not review_ref.strip()):
        raise ConfigurationError("shadow requires a nonempty review reference")

    revision_values = ["builtin-1", v1, v2, path, ratio, shadow, review_ref]
    revision = "env-" + hashlib.sha256(json.dumps(revision_values).encode()).hexdigest()[:16]
    policy = ComparisonPolicy(
        revision="builtin-json-v1",
        max_body_bytes=65536,
        max_decoded_body_bytes=131072,
        max_json_depth=32,
        max_json_nodes=10000,
        max_number_digits=64,
        max_number_exponent=64,
        max_differences=16,
        max_diff_paths=8,
    )
    routes: tuple[Route, ...] = ()
    if path:
        routes = (
            Route(
                route_id="default-route",
                method="GET",
                path_template=path,
                v1=v1,
                v2=v2,
                owner="local-operator",
                rollout_enabled=True,
                v2_serve_ratio=ratio,
                cohort=Cohort("request", "default-route", "request", "builtin-request-v1"),
                shadow=ShadowPolicy(shadow, 1 if shadow else 0, review_ref),
                contract=ResponseContract(frozenset(range(200, 300))),
                comparison_policy_revision=policy.revision,
            ),
        )
    snapshot = Snapshot(
        schema_version=1,
        revision=revision,
        previous_revision=None,
        change_reason="Environment configuration",
        default_v1=v1,
        allowed_backends=frozenset({v1, v2}),
        routes=routes,
        budgets=RuntimeBudgets(
            serving_timeout_seconds=5,
            shadow_timeout_seconds=2,
            client_send_timeout_seconds=5,
            shutdown_grace_seconds=5,
            serving_max_inflight=16,
            shadow_max_inflight=8,
            request_capture_limit_bytes=65536,
            response_capture_limit_bytes=65536,
        ),
    )
    return Settings(
        snapshot=snapshot,
        comparison_policies=MappingProxyType({policy.revision: policy}),
        work_limits=WorkLimits(
            compare_max_jobs=16,
            compare_max_bytes=2097152,
            compare_max_age_seconds=3,
            compare_workers=1,
            compare_timeout_seconds=1,
            event_retention_seconds=3600,
            environment="local",
            migration_id="default",
            epoch_id=revision,
        ),
        collection_limits=CollectionLimits(
            max_events=64,
            max_bytes=2097152,
            max_age_seconds=5,
            batch_size=16,
            write_timeout_seconds=1,
            max_attempts=2,
            retry_delay_seconds=0.01,
        ),
    )
