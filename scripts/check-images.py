"""Enforce immutable image inputs and repository ownership boundaries."""

import hashlib
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
REGISTRY = "ghcr.io/groovemap-music"
INTERNAL_IMAGES = {
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
RELEASED_IMAGE_DIGESTS = {
    "DATABASE_SCHEMA_IMAGE": "6831fa563e5a1b2dccb54fe2a86b64c084bb8d320d57fdd8ff65ace5b65eafa3",
}


def read_env(path: Path) -> dict[str, str]:
    """Read the simple NAME=value files used for Compose image inputs."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            values[name] = value
    return values


compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
services = compose["services"]
assert not any("build" in service for service in services.values()), "deployment must consume images, not sibling build contexts"

for service_name, (variable, _) in INTERNAL_IMAGES.items():
    image = services[service_name]["image"]
    assert image.startswith(f"${{{variable}:?"), f"{service_name} must require {variable}"

for service_name in sorted(set(services) - set(INTERNAL_IMAGES)):
    image = services[service_name]["image"]
    assert DIGEST.search(image), f"{service_name} image is not digest-pinned: {image}"

assert ":latest" not in (ROOT / "docker-compose.yml").read_text()

validation_env = read_env(ROOT / "config/validation.env")
example_env = read_env(ROOT / ".env.example")
for variable, repository in sorted(set(INTERNAL_IMAGES.values())):
    prefix = f"{REGISTRY}/{repository}@sha256:"
    if digest := RELEASED_IMAGE_DIGESTS.get(variable):
        assert validation_env[variable] == prefix + digest, f"{variable} validation image must pin its approved release"
        assert example_env[variable] == prefix + digest, f"{variable} example image must pin its approved release"
    else:
        assert validation_env[variable] == prefix + "1" * 64, f"{variable} validation image must be named {repository}"
        assert example_env[variable] == prefix + "REPLACE_WITH_64_HEX_CHARACTERS", f"{variable} example image must be named {repository}"

provenance = json.loads((ROOT / "config/provenance.json").read_text())["extraction-rules.yaml"]
promoted = ROOT / "config/extraction-rules.yaml"
assert hashlib.sha256(promoted.read_bytes()).hexdigest() == provenance["promoted_sha256"]
assert len(provenance["producer_commit"]) == 40
assert len(provenance["source_sha256"]) == 64
