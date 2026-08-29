"""Repository-boundary and immutable-image regression tests."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
REGISTRY = "ghcr.io/groovemap-music"
SERVICE_IMAGES = {
    "schema-init": ("DATABASE_SCHEMA_IMAGE", "database-schema"),
    "api": ("CATALOG_API_IMAGE", "catalog-api"),
    "extractor-discogs": ("CATALOG_INGESTION_IMAGE", "catalog-ingestion"),
    "extractor-musicbrainz": ("CATALOG_INGESTION_IMAGE", "catalog-ingestion"),
    "graphinator": ("DISCOGS_GRAPH_ENRICHER_IMAGE", "discogs-graph-enricher"),
    "brainzgraphinator": ("MUSICBRAINZ_GRAPH_ENRICHER_IMAGE", "musicbrainz-graph-enricher"),
    "tableinator": ("DISCOGS_SQL_LOADER_IMAGE", "discogs-sql-loader"),
    "brainztableinator": ("MUSICBRAINZ_SQL_LOADER_IMAGE", "musicbrainz-sql-loader"),
    "dashboard": ("OPERATIONS_CONSOLE_IMAGE", "operations-console"),
    "explore": ("GRAPH_EXPLORER_IMAGE", "graph-explorer"),
    "insights": ("ANALYTICS_ENGINE_IMAGE", "analytics-engine"),
}


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            values[name] = value
    return values


def test_infrastructure_images_are_digest_pinned() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("rabbitmq", "postgres", "neo4j", "redis"):
        assert DIGEST.search(services[name]["image"]), (name, services[name]["image"])


def test_internal_images_are_required_inputs() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, (variable, _) in SERVICE_IMAGES.items():
        spec = services[name]
        assert spec["image"].startswith(f"${{{variable}:?")
        assert "build" not in spec


def test_primary_images_use_owning_repository_names() -> None:
    validation = _env(ROOT / "config/validation.env")
    example = _env(ROOT / ".env.example")
    for variable, repository in set(SERVICE_IMAGES.values()):
        prefix = f"{REGISTRY}/{repository}@sha256:"
        assert validation[variable] == prefix + "1" * 64
        assert example[variable] == prefix + "REPLACE_WITH_64_HEX_CHARACTERS"


def test_compose_has_no_mutable_latest_tag() -> None:
    assert ":latest" not in (ROOT / "docker-compose.yml").read_text()
