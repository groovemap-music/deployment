"""Enforce immutable image inputs and repository ownership boundaries."""

import hashlib
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
INTERNAL_IMAGES = {
    "schema-init": "DATABASE_SCHEMA_IMAGE",
    "api": "CATALOG_API_IMAGE",
    "extractor-discogs": "CATALOG_INGESTION_IMAGE",
    "extractor-musicbrainz": "CATALOG_INGESTION_IMAGE",
    "graphinator": "DISCOGS_GRAPH_ENRICHER_IMAGE",
    "brainzgraphinator": "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE",
    "tableinator": "DISCOGS_SQL_LOADER_IMAGE",
    "brainztableinator": "MUSICBRAINZ_SQL_LOADER_IMAGE",
    "dashboard": "OPERATIONS_CONSOLE_IMAGE",
    "explore": "GRAPH_EXPLORER_IMAGE",
    "insights": "ANALYTICS_ENGINE_IMAGE",
}

compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
services = compose["services"]
assert not any("build" in service for service in services.values()), "deployment must consume images, not sibling build contexts"

for service_name, variable in INTERNAL_IMAGES.items():
    image = services[service_name]["image"]
    assert image.startswith(f"${{{variable}:?"), f"{service_name} must require {variable}"

for service_name in sorted(set(services) - set(INTERNAL_IMAGES)):
    image = services[service_name]["image"]
    assert DIGEST.search(image), f"{service_name} image is not digest-pinned: {image}"

assert ":latest" not in (ROOT / "docker-compose.yml").read_text()

provenance = json.loads((ROOT / "config/provenance.json").read_text())["extraction-rules.yaml"]
promoted = ROOT / "config/extraction-rules.yaml"
assert hashlib.sha256(promoted.read_bytes()).hexdigest() == provenance["promoted_sha256"]
assert len(provenance["producer_commit"]) == 40
assert len(provenance["source_sha256"]) == 64
