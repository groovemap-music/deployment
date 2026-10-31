"""Host ports and bind addresses are configurable and default to the stock mapping."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = ["docker-compose.yml"]
PROD = ["docker-compose.yml", "docker-compose.prod.yml"]

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose is required")

Mapping = tuple[str | None, str, int]

# (host_ip, published, target) per service, as rendered before the variables existed.
DEFAULT_BASE: dict[str, list[Mapping]] = {
    "api": [(None, "8004", 8004), (None, "8005", 8005)],
    "dashboard": [(None, "8003", 8003)],
    "explore": [(None, "8006", 8006), (None, "8007", 8007)],
    "grafana": [(None, "3000", 3000)],
    "neo4j": [(None, "7474", 7474), (None, "7687", 7687)],
    "postgres": [(None, "5433", 5432)],
    "rabbitmq": [(None, "5672", 5672), (None, "15672", 15672), ("127.0.0.1", "15692", 15692)],
    "valkey": [(None, "6379", 6379)],
    "victoria-metrics": [(None, "8428", 8428)],
    "victoria-traces": [(None, "10428", 10428)],
}
DEFAULT_PROD: dict[str, list[Mapping]] = {
    **DEFAULT_BASE,
    "valkey": [("127.0.0.1", "6379", 6379)],
    "victoria-metrics": [("127.0.0.1", "8428", 8428)],
    "victoria-traces": [("127.0.0.1", "10428", 10428)],
}


def _ports(files: list[str], overrides: dict[str, str] | None = None) -> dict[str, list[Mapping]]:
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_HOST_PORT", "_BIND_ADDRESS"))}
    env.update(overrides or {})
    args = ["docker", "compose", "--env-file", "config/validation.env"]
    for name in files:
        args += ["-f", name]
    out = subprocess.run([*args, "config", "--format", "json"], cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True).stdout
    services = json.loads(out)["services"]
    return {name: [(p.get("host_ip"), str(p["published"]), p["target"]) for p in svc["ports"]] for name, svc in services.items() if svc.get("ports")}


def test_base_defaults_are_unchanged() -> None:
    assert _ports(BASE) == DEFAULT_BASE


def test_prod_defaults_are_unchanged() -> None:
    assert _ports(PROD) == DEFAULT_PROD


@pytest.mark.parametrize("files, defaults", [(BASE, DEFAULT_BASE), (PROD, DEFAULT_PROD)])
def test_overriding_one_port_changes_only_that_mapping(files: list[str], defaults: dict[str, list[Mapping]]) -> None:
    rendered = _ports(files, {"GRAFANA_HOST_PORT": "3300"})
    assert rendered["grafana"] == [(None, "3300", 3000)]
    assert {k: v for k, v in rendered.items() if k != "grafana"} == {k: v for k, v in defaults.items() if k != "grafana"}


def test_prod_metrics_port_override_does_not_duplicate_the_base_mapping() -> None:
    rendered = _ports(PROD, {"RABBITMQ_METRICS_HOST_PORT": "25692"})
    assert rendered["rabbitmq"] == [(None, "5672", 5672), (None, "15672", 15672), ("127.0.0.1", "25692", 15692)]


def test_data_bind_address_applies_only_to_the_data_tier() -> None:
    rendered = _ports(PROD, {"DATA_BIND_ADDRESS": "192.168.1.2"})
    for service in ("postgres", "neo4j"):
        assert {m[0] for m in rendered[service]} == {"192.168.1.2"}
    assert [m[0] for m in rendered["rabbitmq"]] == ["192.168.1.2", "192.168.1.2", "127.0.0.1"]
    assert rendered["grafana"] == DEFAULT_PROD["grafana"]
    assert rendered["valkey"] == DEFAULT_PROD["valkey"]


def test_web_bind_address_applies_only_to_the_web_tier() -> None:
    rendered = _ports(PROD, {"WEB_BIND_ADDRESS": "10.0.0.9"})
    for service in ("grafana", "api", "dashboard", "explore"):
        assert {m[0] for m in rendered[service]} == {"10.0.0.9"}
    assert rendered["postgres"] == DEFAULT_PROD["postgres"]


def test_internal_bind_address_moves_the_loopback_only_ports() -> None:
    rendered = _ports(PROD, {"INTERNAL_BIND_ADDRESS": "10.0.0.5"})
    assert rendered["valkey"] == [("10.0.0.5", "6379", 6379)]
    assert rendered["rabbitmq"][-1] == ("10.0.0.5", "15692", 15692)
