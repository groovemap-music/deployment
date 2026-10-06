"""Enforce immutable image inputs and repository ownership boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
REGISTRY = "ghcr.io/groovemap-music"

# Compose service -> the required image variable. Each Discogs/MusicBrainz producer
# owns its own image after the ADR 0005 split; no variable is shared by two services,
# except ANALYTICS_ENGINE_IMAGE: `embeddings` is the one-shot analytics-engine-embeddings
# entry point (ADR 0013), invoked with an entrypoint override against the same released
# image the always-on `insights` service runs, never a second published artifact.
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
    "embeddings": "ANALYTICS_ENGINE_IMAGE",
}

# Image variables allowed to be shared by more than one service in INTERNAL_IMAGES —
# the ANALYTICS_ENGINE_IMAGE exception above. Every other variable must promote exactly
# one service, so a variable colliding by accident (a copy-paste bug, not a deliberate
# shared-image job) is still caught.
SHARED_IMAGE_VARIABLES = {"ANALYTICS_ENGINE_IMAGE"}

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

# Image variable -> the manifest digest of the release this deployment has reviewed. The
# released-stack smoke (`just smoke-released`) refuses an operator env file that promotes
# anything else. `.env.example` and `config/validation.env` deliberately stay on
# placeholder and syntax-only digests, so these are the only real digests in the tree.
RELEASED_IMAGE_DIGESTS = {
    "DATABASE_SCHEMA_IMAGE": "6fba747ff353d6f4639b566a33ba73ab79515daaee756980704c88e2c6f32b1c",  # database-schema v0.3.0  gitleaks:allow
    "CATALOG_API_IMAGE": "4889f1ce04568a335bdfe698e11a3c8133238e0b0b61c91447ea4448a3e3ae43",  # catalog-api v0.4.0  gitleaks:allow
    "DISCOGS_INGESTION_IMAGE": "db418bfc97d2d364ac0e64045b492ad8492b500c84f8ce105a04cadd400ee17c",  # discogs-ingestion v0.3.1  gitleaks:allow
    "MUSICBRAINZ_INGESTION_IMAGE": "2b348519450cc9811fe8d194d0ef4b4dd3ead901b2f8e5883dec83a839bd9b37",  # musicbrainz-ingestion v0.2.1  gitleaks:allow
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "e95fabb7633859c94e0913f9122ccbaed18a04a017c486886c38840c230ff80d",  # discogs-graph-enricher v0.3.0  gitleaks:allow
    "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE": "541cc5ef9823a970a44af2952e641a6c925011e1d653274e419fbfc72df62b6e",  # musicbrainz-graph-enricher v0.2.0  gitleaks:allow
    "DISCOGS_SQL_LOADER_IMAGE": "09f55827f972ec289baad7128acca061739fd9d4d350f23f3d6d22afeafee7e6",  # discogs-sql-loader v0.3.0  gitleaks:allow
    "MUSICBRAINZ_SQL_LOADER_IMAGE": "cab35264260d6df0e3a86e2022ed3a6b02506b8404aa845921ff7ec18605b027",  # musicbrainz-sql-loader v0.2.0  gitleaks:allow
    "OPERATIONS_CONSOLE_IMAGE": "fa771bc34f5ed69a028587ae62095268301a774426ef90ee18c0b18f2e5f59b1",  # operations-console v0.1.1  gitleaks:allow
    "GRAPH_EXPLORER_IMAGE": "546e3823b811eb9d912c175a28a56153a72c95107089714393b3c21551f6e33b",  # graph-explorer v0.1.1  gitleaks:allow
    "ANALYTICS_ENGINE_IMAGE": "a50a9eb79f58f463f287de379d6a87b68c39c3df84b8aa6a7c80f93294210c69",  # analytics-engine v0.1.1  gitleaks:allow
}

# Compose service -> the reviewed repository (registry host included when it is not Docker
# Hub) of the third-party image it runs. These are public registry images nobody here
# publishes. The review is of WHERE an image comes from and that it is immutable, not of
# which release it is: Dependabot's docker-compose ecosystem bumps the tag and the manifest
# digest together, and those routine bumps must not need a hand edit here. So the check
# pins the repository (an unknown, swapped, or re-hosted repository fails) and requires a
# versioned tag plus a sha256 digest (a bare, `latest`, or digest-less reference fails),
# but accepts any tag and digest for a reviewed repository. Every service NOT in
# INTERNAL_IMAGES must appear here.
THIRD_PARTY_REPOSITORIES = {
    "cadvisor": "gcr.io/cadvisor/cadvisor",
    "grafana": "grafana/grafana",
    "neo4j": "neo4j",
    "node-exporter": "prom/node-exporter",
    "otel-collector": "otel/opentelemetry-collector-contrib",
    "postgres": "postgres",
    "postgres-exporter": "prometheuscommunity/postgres-exporter",
    "rabbitmq": "rabbitmq",
    # rabbitmqadmin (the HTTP-API CLI that declares the catalog-DLQ cap policy,
    # gm-deployment-8mb.1) is bundled in the same image, so this one-shot init
    # service reuses rabbitmq's own repository rather than adding a new
    # third-party image.
    "rabbitmq-dlq-policy-init": "rabbitmq",
    "redis": "redis",
    "redis-exporter": "oliver006/redis_exporter",
    "victoria-metrics": "victoriametrics/victoria-metrics",
    "victoria-traces": "victoriametrics/victoria-traces",
}

# repository[:tag]@sha256:digest, where the repository may carry a registry host:port.
IMAGE_REFERENCE = re.compile(r"(?P<repository>[^@:]+(?::\d+)?(?:/[^@:]+)*?)(?::(?P<tag>[^@:/]+))?@sha256:[0-9a-f]{64}")


def check_third_party_reference(service_name: str, image: str) -> None:
    """Require a reviewed repository, a non-floating tag, and a digest; any version is fine."""
    assert DIGEST.search(image), f"{service_name} image is not digest-pinned: {image}"
    match = IMAGE_REFERENCE.fullmatch(image)
    assert match, f"{service_name} image is not a repository:tag@sha256 reference: {image}"
    expected = THIRD_PARTY_REPOSITORIES[service_name]
    assert match["repository"] == expected, f"{service_name} runs repository {match['repository']}, not the reviewed {expected}"
    assert match["tag"] and match["tag"] != "latest", f"{service_name} image needs a versioned tag, not {match['tag']!r}: {image}"


def check_declarations() -> None:
    """Validate the static ownership and reviewed-release policy tables."""
    assert set(INTERNAL_IMAGES.values()) == set(IMAGE_OWNERS), "every required image variable needs a declared owning repository"
    unshared_variables = [variable for variable in INTERNAL_IMAGES.values() if variable not in SHARED_IMAGE_VARIABLES]
    assert len(set(unshared_variables)) == len(unshared_variables), (
        "each service must promote its own source-owned image unless declared in SHARED_IMAGE_VARIABLES"
    )
    for variable in SHARED_IMAGE_VARIABLES:
        assert variable in IMAGE_OWNERS, f"{variable} is declared shared but is not a known internal image variable"
    assert set(RELEASED_IMAGE_DIGESTS) == set(IMAGE_OWNERS), "every owned image variable needs a reviewed release digest"
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in RELEASED_IMAGE_DIGESTS.values()), (
        "a reviewed release digest must be a full manifest digest"
    )
    assert set(INTERNAL_IMAGES).isdisjoint(THIRD_PARTY_REPOSITORIES), "a service runs either a released GrooveMap image or a public one"
    assert all(
        repository and "@" not in repository and ":" not in repository.rsplit("/", 1)[-1] for repository in THIRD_PARTY_REPOSITORIES.values()
    ), "a reviewed third-party repository is a bare name, with no tag or digest"


def parse_assignments(text: str) -> dict[str, str]:
    """Parse the simple, unquoted assignments used by repository env templates."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def check_compose(compose_text: str, env_templates: Mapping[str, str]) -> None:
    """Validate image ownership and immutability without reading the filesystem."""
    import yaml  # noqa: PLC0415

    compose: dict[str, Any] = yaml.safe_load(compose_text)
    services = compose["services"]
    assert not any("build" in service for service in services.values()), "deployment must consume images, not sibling build contexts"

    for service_name, variable in INTERNAL_IMAGES.items():
        image = services[service_name]["image"]
        assert image.startswith(f"${{{variable}:?"), f"{service_name} must require {variable}"

    third_party_services = sorted(set(services) - set(INTERNAL_IMAGES))
    assert third_party_services == sorted(THIRD_PARTY_REPOSITORIES), (
        f"THIRD_PARTY_REPOSITORIES does not match the compose services: {sorted(set(third_party_services) ^ set(THIRD_PARTY_REPOSITORIES))}"
    )
    for service_name in third_party_services:
        check_third_party_reference(service_name, services[service_name]["image"])

    assert ":latest" not in compose_text

    for env_name, env_text in env_templates.items():
        assignments = parse_assignments(env_text)
        for variable, repository in IMAGE_OWNERS.items():
            assert variable in assignments, f"{env_name} is missing {variable}"
            reference = assignments[variable]
            assert reference.startswith(f"{REGISTRY}/{repository}@sha256:"), (
                f"{env_name}: {variable} must promote {REGISTRY}/{repository} by digest, got {reference}"
            )


def check_provenance(provenance: Mapping[str, Mapping[str, str]], promoted_files: Mapping[str, bytes]) -> None:
    """Validate promoted artifact metadata against supplied file content."""
    assert "extraction-rules.yaml" in provenance, "the promoted extraction rules must stay recorded"
    for relative_path, record in sorted(provenance.items()):
        assert relative_path in promoted_files, f"config/provenance.json records {relative_path}, which is not a promoted file"
        assert hashlib.sha256(promoted_files[relative_path]).hexdigest() == record["promoted_sha256"], (
            f"config/{relative_path} drifted from its recorded promoted digest"
        )
        assert record["owner"].startswith("groovemap-music/"), f"config/{relative_path} must name its owning repository"
        assert len(record["producer_commit"]) == 40, f"config/{relative_path} must record a full producer commit"
        assert len(record["source_sha256"]) == 64, f"config/{relative_path} must record the upstream source digest"
        assert record["source_path"], f"config/{relative_path} must record its path in the owning repository"

    for relative_path in ("media-smoke/discogs-releases.data.json", "media-smoke/musicbrainz-releases.data.json"):
        record = provenance[relative_path]
        assert record["promoted_sha256"] == record["source_sha256"], f"config/{relative_path} must be promoted verbatim from {record['owner']}"


def check_repository(root: Path = ROOT) -> None:
    """Read repository inputs and apply every image and provenance policy."""
    check_declarations()
    env_names = (".env.example", "config/validation.env")
    check_compose(
        (root / "docker-compose.yml").read_text(),
        {name: (root / name).read_text() for name in env_names},
    )
    provenance: dict[str, dict[str, str]] = json.loads((root / "config/provenance.json").read_text())
    promoted_files = {
        relative_path: (root / "config" / relative_path).read_bytes() for relative_path in provenance if (root / "config" / relative_path).is_file()
    }
    check_provenance(provenance, promoted_files)


def released_digest_lines() -> str:
    """Render the reviewed release set for the released-stack shell adapter."""
    check_declarations()
    return "\n".join(f"{variable} {digest}" for variable, digest in RELEASED_IMAGE_DIGESTS.items())


if __name__ == "__main__":
    if "--released-digests" in sys.argv[1:]:
        print(released_digest_lines())
    else:
        check_repository()
