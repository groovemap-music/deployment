"""Repository-boundary and immutable-image regression tests."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")

# Third-party services consumed from public registries. They carry a literal
# digest-pinned image reference; every OTHER service is an internally released
# GrooveMap image supplied through a required environment variable.
THIRD_PARTY_SERVICES = (
    "rabbitmq",
    "postgres",
    "neo4j",
    "redis",
    "otel-collector",
    "victoria-metrics",
    "victoria-traces",
    "grafana",
    "postgres-exporter",
    "redis-exporter",
)


def test_infrastructure_images_are_digest_pinned() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in THIRD_PARTY_SERVICES:
        assert DIGEST.search(services[name]["image"]), (name, services[name]["image"])


def test_internal_images_are_required_inputs() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, spec in services.items():
        if name in set(THIRD_PARTY_SERVICES):
            continue
        assert spec["image"].startswith("${") and ":?" in spec["image"]
        assert "build" not in spec


def test_compose_has_no_mutable_latest_tag() -> None:
    assert ":latest" not in (ROOT / "docker-compose.yml").read_text()
