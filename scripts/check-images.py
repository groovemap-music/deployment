"""Enforce immutable image inputs and repository ownership boundaries."""

import hashlib
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
REGISTRY = "ghcr.io/groovemap-music"

# Compose service -> the required image variable. Each Discogs/MusicBrainz producer
# owns its own image after the ADR 0005 split; no variable is shared by two services.
INTERNAL_IMAGES = {
    "schema-init": "DATABASE_SCHEMA_IMAGE",
    "api": "CATALOG_API_IMAGE",
    "extractor-discogs": "DISCOGS_INGESTION_IMAGE",
    "extractor-musicbrainz": "MUSICBRAINZ_INGESTION_IMAGE",
    "graphinator": "DISCOGS_GRAPH_ENRICHER_IMAGE",
    "brainzgraphinator": "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE",
    "tableinator": "DISCOGS_SQL_LOADER_IMAGE",
    "brainztableinator": "MUSICBRAINZ_SQL_LOADER_IMAGE",
    "dashboard": "OPERATIONS_CONSOLE_IMAGE",
    "explore": "GRAPH_EXPLORER_IMAGE",
    "insights": "ANALYTICS_ENGINE_IMAGE",
}

# Image variable -> the repository that owns and publishes it. The repository name is
# also the GHCR image name, so a variable must never promote another repository's image.
IMAGE_OWNERS = {
    "DATABASE_SCHEMA_IMAGE": "database-schema",
    "CATALOG_API_IMAGE": "catalog-api",
    "DISCOGS_INGESTION_IMAGE": "discogs-ingestion",
    "MUSICBRAINZ_INGESTION_IMAGE": "musicbrainz-ingestion",
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "discogs-graph-enricher",
    "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE": "musicbrainz-graph-enricher",
    "DISCOGS_SQL_LOADER_IMAGE": "discogs-sql-loader",
    "MUSICBRAINZ_SQL_LOADER_IMAGE": "musicbrainz-sql-loader",
    "OPERATIONS_CONSOLE_IMAGE": "operations-console",
    "GRAPH_EXPLORER_IMAGE": "graph-explorer",
    "ANALYTICS_ENGINE_IMAGE": "analytics-engine",
}

assert set(INTERNAL_IMAGES.values()) == set(IMAGE_OWNERS), "every required image variable needs a declared owning repository"
assert len(set(INTERNAL_IMAGES.values())) == len(INTERNAL_IMAGES), "each service must promote its own source-owned image"

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

# Documented image references must name their owning repository. `.env.example` carries a
# placeholder digest and `config/validation.env` a non-published one; neither is a
# deployment input, but both teach the operator which repository each variable promotes.
for env_name in (".env.example", "config/validation.env"):
    assignments = dict(line.split("=", 1) for line in (ROOT / env_name).read_text().splitlines() if "=" in line and not line.startswith("#"))
    for variable, repository in IMAGE_OWNERS.items():
        assert variable in assignments, f"{env_name} is missing {variable}"
        reference = assignments[variable]
        assert reference.startswith(f"{REGISTRY}/{repository}@sha256:"), (
            f"{env_name}: {variable} must promote {REGISTRY}/{repository} by digest, got {reference}"
        )

# Every promoted upstream artifact under `config/` records where it came from, so drift
# against the owning repository is a check failure rather than a silent divergence. The
# promoted extraction rules and the producers' promoted contract fixtures share one record.
provenance = json.loads((ROOT / "config/provenance.json").read_text())
assert "extraction-rules.yaml" in provenance, "the promoted extraction rules must stay recorded"
for relative_path, record in sorted(provenance.items()):
    promoted = ROOT / "config" / relative_path
    assert promoted.is_file(), f"config/provenance.json records {relative_path}, which is not a promoted file"
    assert hashlib.sha256(promoted.read_bytes()).hexdigest() == record["promoted_sha256"], (
        f"config/{relative_path} drifted from its recorded promoted digest"
    )
    assert record["owner"].startswith("groovemap-music/"), f"config/{relative_path} must name its owning repository"
    assert len(record["producer_commit"]) == 40, f"config/{relative_path} must record a full producer commit"
    assert len(record["source_sha256"]) == 64, f"config/{relative_path} must record the upstream source digest"
    assert record["source_path"], f"config/{relative_path} must record its path in the owning repository"

# The promoted contract fixtures are the smoke stack's only release inputs, so they must be
# byte-identical to the producers' fixtures rather than a locally edited copy.
for relative_path in ("media-smoke/discogs-releases.data.json", "media-smoke/musicbrainz-releases.data.json"):
    record = provenance[relative_path]
    assert record["promoted_sha256"] == record["source_sha256"], f"config/{relative_path} must be promoted verbatim from {record['owner']}"
