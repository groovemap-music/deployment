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
    "ANALYTICS_ENGINE_IMAGE": "a50a9eb79f58f463f287de379d6a87b68c39c3df84b8aa6a7c80f93294210c69",  # gitleaks:allow
    "CATALOG_API_IMAGE": "3483fb912c94f79076b4010043fb074eda3cdbb1299d3080887d6709590501d7",  # gitleaks:allow
    "CATALOG_INGESTION_IMAGE": "6cdd1c14dd6c7d5a8d298d81f066913ba5d069980850d211b6a50bbae180fed4",  # gitleaks:allow
    "DATABASE_SCHEMA_IMAGE": "6831fa563e5a1b2dccb54fe2a86b64c084bb8d320d57fdd8ff65ace5b65eafa3",  # gitleaks:allow
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "441881a0862613bc7393bad553a3f4d15331f731f49e2c4cc008e222c770f601",  # gitleaks:allow
    "DISCOGS_SQL_LOADER_IMAGE": "bd1e045292322c2a9c3ce2a01f9c38365b56a549f7eb2f0cb7ceec97f95edc31",  # gitleaks:allow
    "GRAPH_EXPLORER_IMAGE": "546e3823b811eb9d912c175a28a56153a72c95107089714393b3c21551f6e33b",  # gitleaks:allow
    "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE": "c08a5987503a492e8f7ad500be1d6007c0ce273fb63956e84cadff4ff97c68a5",  # gitleaks:allow
    "MUSICBRAINZ_SQL_LOADER_IMAGE": "08a96799c2846707892277dc7fe1c7eba8b6100432a72e35bc97fe3671d0856e",  # gitleaks:allow
    "OPERATIONS_CONSOLE_IMAGE": "fa771bc34f5ed69a028587ae62095268301a774426ef90ee18c0b18f2e5f59b1",  # gitleaks:allow
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
