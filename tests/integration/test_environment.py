from __future__ import annotations

import os
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from api_migration_proxy.environment import (
    load_proxy_settings,
    load_run_options,
    read_environment,
)
from api_migration_proxy.routing.configuration import ConfigurationError
from api_migration_proxy.routing.selection import choose_serving, match_route
from api_migration_proxy.settings import load_settings, metrics_from_settings


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    for key in os.environ:
        if key.startswith("API_PROXY_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_missing_environment_file_uses_safe_defaults_without_parent_discovery(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text("API_PROXY_PORT=1234\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    environment = read_environment()
    assert environment == {}
    run = load_run_options(environment)
    assert (run.host, run.port, run.event_store) == ("127.0.0.1", 8080, "events.sqlite")
    assert (run.control_enabled, run.control_host, run.control_port, run.control_token) == (
        True,
        "127.0.0.1",
        9090,
        "",
    )
    assert not hasattr(run, "expose_observability")
    assert run.event_store_backend == "sqlite"
    assert run.postgres_dsn == ""
    assert run.retention_interval_seconds == 60
    assert run.retention_batch_size == 1000
    settings = load_proxy_settings(environment)
    assert settings.snapshot.default_v1 == "http://127.0.0.1:8001"
    assert settings.snapshot.routes == ()
    assert match_route(settings.snapshot, "GET", "/anything") is None
    assert metrics_from_settings(settings).snapshot().counters == {}


def test_process_environment_overrides_dotenv_without_mutation_or_interpolation(monkeypatch):
    Path(".env").write_text(
        "\ufeff# settings\n"
        'export API_PROXY_HOST="0.0.0.0" # bind\n'
        "API_PROXY_PORT='8082'\n"
        'API_PROXY_EVENT_STORE="${HOME}/events.sqlite"\n'
        "API_PROXY_CONFIG=\n"
        "UNRELATED_SECRET=private\n"
    )
    monkeypatch.setenv("API_PROXY_PORT", "8083")
    before = dict(os.environ)
    environment = read_environment()
    assert environment == {
        "API_PROXY_HOST": "0.0.0.0",
        "API_PROXY_PORT": "8083",
        "API_PROXY_EVENT_STORE": "${HOME}/events.sqlite",
        "API_PROXY_CONFIG": "",
    }
    assert load_run_options(environment).port == 8083
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "content",
    [b'API_PROXY_CONTROL_TOKEN="sensitive-unterminated\n', b"API_PROXY_PORT\n", b"\xff"],
)
def test_invalid_dotenv_is_rejected_without_echoing_values(content, caplog):
    Path(".env").write_bytes(content)
    with pytest.raises(ConfigurationError) as error:
        read_environment()
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__
    assert caplog.text == ""


def test_environment_file_io_error_is_not_treated_as_missing(tmp_path):
    with pytest.raises(ConfigurationError):
        read_environment(tmp_path)


def test_example_matches_builtin_defaults():
    example = Path(__file__).parents[2] / ".env.example"
    assert example.is_file()
    environment = read_environment(example)
    assert len(environment) == 18
    assert "API_PROXY_EXPOSE_OBSERVABILITY" not in environment
    assert load_run_options(environment) == load_run_options({})
    assert load_proxy_settings(environment) == load_proxy_settings({})


@pytest.mark.parametrize("ratio,backend", [("0", "v1"), ("1", "v2")])
def test_simple_route_enables_get_request_cohort_and_serving_boundaries(ratio, backend):
    settings = load_proxy_settings(
        {"API_PROXY_ROUTE_PATH": "/items/{id}", "API_PROXY_V2_RATIO": ratio}
    )
    snapshot = settings.snapshot
    matched = match_route(snapshot, "GET", "/items/42")
    assert matched is not None
    route = matched.route
    assert choose_serving(route, request_key="independent-request").backend == backend
    assert route.cohort.mode == route.cohort.key_source == "request"
    assert not route.shadow.eligible
    assert route.shadow.sample_ratio == 0
    assert route.contract.classify(200) == route.contract.classify(299) == "success"
    assert route.contract.classify(404) == "unknown"
    assert match_route(snapshot, "POST", "/items/42") is None
    assert match_route(snapshot, "GET", "/other") is None


def test_shadow_requires_review_and_enables_full_sampling():
    environment = {"API_PROXY_ROUTE_PATH": "/items/{id}", "API_PROXY_SHADOW_ENABLED": "true"}
    for review_ref in ("", " "):
        with pytest.raises(ConfigurationError):
            load_proxy_settings({**environment, "API_PROXY_SHADOW_REVIEW_REF": review_ref})
    settings = load_proxy_settings({**environment, "API_PROXY_SHADOW_REVIEW_REF": "review-42"})
    route = settings.snapshot.routes[0]
    assert route.rollout_enabled and route.shadow.eligible
    assert route.shadow.sample_ratio == 1
    assert route.shadow.review_ref == "review-42"
    assert route.comparison_policy_revision in settings.comparison_policies
    assert choose_serving(route, request_key="request").backend == "v1"


@pytest.mark.parametrize(
    "environment",
    [
        {"API_PROXY_V2_RATIO": "0.1"},
        {"API_PROXY_SHADOW_ENABLED": "true", "API_PROXY_SHADOW_REVIEW_REF": "review-42"},
    ],
)
def test_route_required_before_enabling_v2_or_shadow(environment):
    with pytest.raises(ConfigurationError):
        load_proxy_settings(environment)


@pytest.mark.parametrize("ratio", ["NaN", "inf", "-inf", "-0.1", "1.1", "sensitive-invalid", ""])
def test_invalid_v2_ratios_are_rejected_without_values(ratio):
    with pytest.raises(ConfigurationError) as error:
        load_proxy_settings({"API_PROXY_ROUTE_PATH": "/items", "API_PROXY_V2_RATIO": ratio})
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "variable",
    ["API_PROXY_SHADOW_ENABLED", "API_PROXY_CONTROL_ENABLED"],
)
@pytest.mark.parametrize("value", ["1", "yes", "FALSE", ""])
def test_switches_require_explicit_lowercase_boolean(variable, value):
    loader = load_proxy_settings if variable == "API_PROXY_SHADOW_ENABLED" else load_run_options
    with pytest.raises(ConfigurationError):
        loader({variable: value})


@pytest.mark.parametrize("variable", ["API_PROXY_PORT", "API_PROXY_CONTROL_PORT"])
@pytest.mark.parametrize("value", ["0", "65536", "1.5", "8_080", "sensitive-invalid", ""])
def test_invalid_listener_ports_are_rejected_without_values(variable, value):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({variable: value})
    assert "sensitive" not in str(error.value)


def test_listener_port_boundaries_and_disabled_control():
    options = load_run_options({"API_PROXY_PORT": "1", "API_PROXY_CONTROL_PORT": "65535"})
    assert (options.port, options.control_port) == (1, 65535)
    with pytest.raises(ConfigurationError):
        load_run_options({"API_PROXY_CONTROL_PORT": "8080"})
    disabled = load_run_options(
        {
            "API_PROXY_CONTROL_ENABLED": "false",
            "API_PROXY_CONTROL_PORT": "8080",
            "API_PROXY_CONTROL_HOST": "0.0.0.0",
        }
    )
    assert not disabled.control_enabled


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "localhost", "sensitive-host"])
@pytest.mark.parametrize("token", ["", "sensitive-token"])
def test_control_nonloopback_is_rejected_even_with_explicit_token(host, token):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({"API_PROXY_CONTROL_HOST": host, "API_PROXY_CONTROL_TOKEN": token})
    assert "sensitive" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
@pytest.mark.parametrize("token", ["", "sensitive-token"])
def test_control_literal_loopback_permits_private_optional_token(host, token):
    options = load_run_options({"API_PROXY_CONTROL_HOST": host, "API_PROXY_CONTROL_TOKEN": token})
    assert options.control_token == token
    assert "sensitive-token" not in repr(options)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "localhost"])
def test_disabled_control_preserves_unused_nonloopback_host(host):
    options = load_run_options(
        {"API_PROXY_CONTROL_ENABLED": "false", "API_PROXY_CONTROL_HOST": host}
    )
    assert not options.control_enabled
    assert options.control_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "localhost"])
def test_public_listener_remains_independent_of_control_loopback_requirement(host):
    options = load_run_options({"API_PROXY_HOST": host})
    assert options.host == host
    assert options.control_host == "127.0.0.1"


@pytest.mark.parametrize("token", [" ", "sensitive token", "sensitive\ntoken", "sensitive-비밀"])
def test_unusable_bearer_tokens_are_rejected_without_values(token):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({"API_PROXY_CONTROL_TOKEN": token})
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("variable", ["API_PROXY_V1_URL", "API_PROXY_V2_URL"])
@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.invalid",
        "http://user:sensitive@example.invalid",
        "http://example.invalid/path",
        "",
    ],
)
def test_backend_origins_are_validated(variable, value):
    with pytest.raises(ConfigurationError) as error:
        load_proxy_settings({variable: value})
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"API_PROXY_V1_URL": "http://example.invalid:sensitive-marker"},
        {"API_PROXY_CONFIG": "/missing-sensitive-marker.json"},
    ],
)
def test_invalid_proxy_values_do_not_leak_through_exception_chains(environment):
    with pytest.raises(ConfigurationError) as error:
        load_proxy_settings(environment)
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


def test_json_configuration_has_authority_over_all_proxy_environment_values():
    path = Path(__file__).parents[1] / "fixtures/proxy.json"
    environment = {
        "API_PROXY_CONFIG": str(path),
        "API_PROXY_V1_URL": "invalid",
        "API_PROXY_V2_URL": "invalid",
        "API_PROXY_ROUTE_PATH": "invalid",
        "API_PROXY_V2_RATIO": "invalid",
        "API_PROXY_SHADOW_ENABLED": "invalid",
        "API_PROXY_SHADOW_REVIEW_REF": "invalid",
        "API_PROXY_PORT": "8083",
    }
    assert load_proxy_settings(environment) == load_settings(path)
    assert load_run_options(environment).port == 8083


def test_snapshot_revision_tracks_proxy_inputs_and_excludes_run_secrets():
    baseline = load_proxy_settings({}).snapshot.revision
    assert (
        load_proxy_settings({"API_PROXY_V1_URL": "http://127.0.0.1:8001/"}).snapshot.revision
        == baseline
    )
    assert (
        load_proxy_settings(
            {
                "API_PROXY_CONTROL_TOKEN": "sensitive-token",
                "API_PROXY_PORT": "8083",
                "API_PROXY_EVENT_STORE": "other.sqlite",
            }
        ).snapshot.revision
        == baseline
    )
    assert load_proxy_settings({"API_PROXY_ROUTE_PATH": "/items"}).snapshot.revision != baseline


@pytest.mark.parametrize("value", ["true", "false", "invalid"])
def test_removed_observability_environment_has_no_configuration_effect(monkeypatch, value):
    monkeypatch.setenv("API_PROXY_EXPOSE_OBSERVABILITY", value)
    environment = read_environment()
    assert environment["API_PROXY_EXPOSE_OBSERVABILITY"] == value
    options = load_run_options(environment)
    assert options == load_run_options({})
    assert not hasattr(options, "expose_observability")
    assert load_proxy_settings(environment) == load_proxy_settings({})


def test_removed_observability_field_cannot_be_set_directly():
    removed_setting: dict[str, Any] = {"expose_observability": True}
    with pytest.raises(TypeError, match="expose_observability"):
        replace(load_run_options({}), **removed_setting)


def test_postgresql_selection_keeps_dsn_private_and_does_not_require_sqlite_path():
    secret = "postgresql://example:sensitive-marker@db.invalid/events"
    for path in ("", "\x00"):
        options = load_run_options(
            {
                "API_PROXY_EVENT_STORE_BACKEND": "postgresql",
                "API_PROXY_POSTGRES_DSN": secret,
                "API_PROXY_EVENT_STORE": path,
            }
        )
        assert options.event_store_backend == "postgresql"
        assert options.postgres_dsn == secret
        assert "sensitive-marker" not in repr(options)


@pytest.mark.parametrize("dsn", ["", " ", "sensitive-marker\x00"])
def test_postgresql_requires_usable_dsn_without_exposing_values(dsn):
    with pytest.raises(ConfigurationError) as error:
        load_run_options(
            {"API_PROXY_EVENT_STORE_BACKEND": "postgresql", "API_PROXY_POSTGRES_DSN": dsn}
        )
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


def test_sqlite_ignores_unused_postgres_dsn_and_still_requires_file_path():
    options = load_run_options({"API_PROXY_POSTGRES_DSN": "sensitive-marker\x00"})
    assert options.event_store_backend == "sqlite"
    assert "sensitive-marker" not in repr(options)
    for path in ("", " ", "sensitive-marker\x00"):
        with pytest.raises(ConfigurationError) as error:
            load_run_options({"API_PROXY_EVENT_STORE": path})
        assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("backend", ["", "postgres", "POSTGRESQL", "sensitive-marker"])
def test_unsupported_event_store_backends_are_rejected_without_values(backend):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({"API_PROXY_EVENT_STORE_BACKEND": backend})
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("seconds", ["0.001", "60", "1e3"])
@pytest.mark.parametrize("batch", ["1", "1000", str(2**63 - 1)])
def test_retention_budgets_accept_positive_seconds_and_integer_boundaries(seconds, batch):
    options = load_run_options(
        {
            "API_PROXY_RETENTION_INTERVAL_SECONDS": seconds,
            "API_PROXY_RETENTION_BATCH_SIZE": batch,
        }
    )
    assert options.retention_interval_seconds == float(seconds)
    assert options.retention_batch_size == int(batch)


@pytest.mark.parametrize(
    "seconds", ["", "0", "-1", "nan", "inf", "-inf", "1e999", "sensitive-marker"]
)
def test_retention_interval_rejects_invalid_values_without_exception_leak(seconds):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({"API_PROXY_RETENTION_INTERVAL_SECONDS": seconds})
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "batch",
    [
        "",
        "0",
        "-1",
        "+1",
        " 1",
        "1.0",
        "1e3",
        "1_000",
        "١",
        "１",
        str(2**63),
        pytest.param("9" * 5000, id="oversized-decimal"),
        "sensitive-marker",
    ],
)
def test_retention_batch_requires_bounded_positive_ascii_decimal_without_exception_leak(batch):
    with pytest.raises(ConfigurationError) as error:
        load_run_options({"API_PROXY_RETENTION_BATCH_SIZE": batch})
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "changes",
    [
        {"control_host": "0.0.0.0", "control_token": "sensitive-token"},
        {"event_store_backend": "postgres"},
        {"event_store_backend": "postgresql", "postgres_dsn": None},
        {"retention_interval_seconds": True},
        {"retention_interval_seconds": "1"},
        {"retention_interval_seconds": float("inf")},
        {"retention_interval_seconds": 10**1000},
        {"retention_batch_size": True},
        {"retention_batch_size": 1.0},
        {"retention_batch_size": 0},
        {"retention_batch_size": 2**63},
    ],
)
def test_run_options_validate_direct_construction(changes):
    with pytest.raises(ConfigurationError):
        replace(load_run_options({}), **changes)
