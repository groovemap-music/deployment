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
    # groovemap-music/postgres: the official postgres:18-alpine plus pgvector, published from its own
    # repository (gm-deployment-p9ql) and reviewed here like every other GrooveMap release. It stays
    # Alpine (musl), so data directories created under postgres:18-alpine keep their collation.
    "postgres": "POSTGRES_IMAGE",
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
    "POSTGRES_IMAGE": "postgres",
}

# Image variable -> the manifest digest of the release this deployment has reviewed. The
# released-stack smoke (`just smoke-released`) refuses an operator env file that promotes
# anything else. `.env.example` and `config/validation.env` deliberately stay on
# placeholder and syntax-only digests, so these are the only real digests in the tree.
RELEASED_IMAGE_DIGESTS = {
    "DATABASE_SCHEMA_IMAGE": "f04f4e08f5fa7df3c5613333ce57f92f7ee306b127e26d6387c496b5a82d01af",  # database-schema v0.13.0  gitleaks:allow
    "CATALOG_API_IMAGE": "6593499eeb761c45e0229e91ebfd1bf370587cdbc35f4b06ac3231479db1bb2a",  # catalog-api v0.11.4  gitleaks:allow
    "DISCOGS_INGESTION_IMAGE": "aff3558504b8166ec8fb11a96b48eaee2754a57209b4b57a5f39603000dc74d9",  # discogs-ingestion v0.4.1  gitleaks:allow
    "MUSICBRAINZ_INGESTION_IMAGE": "b3f03785faa03b0d445b74a3ab31f82d842bc0e739500b2021ac515fcf6a712c",  # musicbrainz-ingestion v0.4.1  gitleaks:allow
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "69634f1473574fec51623d0ba289137be7b0dce65684251357e333a57798d929",  # discogs-graph-enricher v0.7.0  gitleaks:allow
    "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE": "f382c6d1497fb598948876acb310ec355f98d99fbffe5820582e30df6d0c5329",  # musicbrainz-graph-enricher v0.5.0  gitleaks:allow
    "DISCOGS_SQL_LOADER_IMAGE": "64e18ea3554d5e3c944a59fd73397c6f5d0d2bb6ad821e939e0a3f5965b9f491",  # discogs-sql-loader v0.6.0  gitleaks:allow
    "MUSICBRAINZ_SQL_LOADER_IMAGE": "16345121813c9b85d70d97f3ce28a2364299ff64e46c7accc4b6443424715212",  # musicbrainz-sql-loader v0.7.0  gitleaks:allow
    "OPERATIONS_CONSOLE_IMAGE": "07825a1c1c4ca622a3199364085b5821f2ebb69e004547dc4734de8216156816",  # operations-console v0.3.1  gitleaks:allow
    "GRAPH_EXPLORER_IMAGE": "885991da887c8f139b9ad5216c2e9b4a8cc95da9171e39b5e8d49ce56c8df2ce",  # graph-explorer v0.3.3  gitleaks:allow
    "ANALYTICS_ENGINE_IMAGE": "eb6eecf916c876ea22bbfb0c669a780e702941b24c55dfacdea4a1f48968bd4f",  # analytics-engine v0.7.0  gitleaks:allow
    "POSTGRES_IMAGE": "43fee8e4ebe980dfec290ec46aecfbc78368a2c95d1ba5d7b591e60025ac04e1",  # postgres v18.6.0  gitleaks:allow
}

# Image variable -> the release RELEASED_IMAGE_DIGESTS pins, as written in the comment beside each
# digest (tests keep the two in step). The capability floors below are judged against these.
RELEASED_IMAGE_VERSIONS = {
    "DATABASE_SCHEMA_IMAGE": "0.13.0",
    "CATALOG_API_IMAGE": "0.11.4",
    "DISCOGS_INGESTION_IMAGE": "0.4.1",
    "MUSICBRAINZ_INGESTION_IMAGE": "0.4.1",
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "0.7.0",
    "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE": "0.5.0",
    "DISCOGS_SQL_LOADER_IMAGE": "0.6.0",
    "MUSICBRAINZ_SQL_LOADER_IMAGE": "0.7.0",
    "OPERATIONS_CONSOLE_IMAGE": "0.3.1",
    "GRAPH_EXPLORER_IMAGE": "0.3.3",
    "ANALYTICS_ENGINE_IMAGE": "0.7.0",
    # MAJOR.MINOR is the PostgreSQL release; the last number is groovemap-music/postgres's own revision.
    "POSTGRES_IMAGE": "18.6.0",
}

# The digest a release is recorded with before it is published. check_declarations refuses it.
UNPUBLISHED_DIGEST = "0" * 64

# A capability floor that no release meets yet. `just deploy` refuses the external store it guards.
UNRELEASED = "unreleased"

# External stores (docker-compose.external-*.yml) -> image variable -> the first release that
# supports it. `just deploy` refuses GROOVEMAP_POSTGRES=external or GROOVEMAP_VALKEY=external until
# RELEASED_IMAGE_VERSIONS meets every floor of that store and .env runs those reviewed digests.
# Promoting a release that supports it means replacing UNRELEASED with its version here.
EXTERNAL_STORE_FLOORS = {
    # POSTGRES_PREPROVISIONED: verify an operator-provisioned database instead of creating one
    # (gm-database-schema-2aty), first released in database-schema v0.6.0.
    "postgres": {"DATABASE_SCHEMA_IMAGE": "0.6.0"},
    # VALKEY_KEY_PREFIX applied to every key (python-libraries fd0e548, gm-python-libraries-3sm),
    # adopted by the only two Valkey clients (gm-catalog-api-rchs, gm-analytics-engine-bah5), and
    # VALKEY_TLS (valkeys://, python-libraries 5f35c03), adopted in gm-catalog-api-vd7n and
    # gm-analytics-engine-m0sz: the floor is the first release of each with both.
    "valkey": {"CATALOG_API_IMAGE": "0.6.0", "ANALYTICS_ENGINE_IMAGE": "0.5.0"},
}

# The first operations-console release that reads the configurable Platform Health targets
# (<NAME>_MODE / <NAME>_URL, operations-console 5af1a0d, after v0.2.0). The shared-monitoring and
# external-store overlays set them; an older console ignores them, which is harmless, but keeps
# probing the omitted local services by their compose names, so `just deploy` warns (never stops)
# while the reviewed console is below this floor. Met by the v0.3.0 promotion (gm-deployment-8nbg).
CONSOLE_TARGETS_FLOOR = {"OPERATIONS_CONSOLE_IMAGE": "0.3.0"}

# Releases that only work together: (image, its first release that needs the other, the image it
# needs, the first release of that image that has it). check-images refuses a reviewed set that
# breaks a pair, so it cannot be promoted. A pair is both UNRELEASED, which leaves it inert, or both
# set: one value alone is refused. The promotion replaces UNRELEASED with versions.
PAIRED_IMAGE_FLOORS = (
    # gm-discogs-sql-loader-gna2 writes purge_vetoed on loader_extraction_latch and its startup probe
    # requires the column, so against an older schema it declines the latch and reports degraded.
    ("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", "0.8.0"),
    # gm-musicbrainz-sql-loader: v0.6.0 writes purge_vetoed, and its hash ON CONFLICT target is the only
    # key once database-schema v0.9.0 drops relationships_natural_key. An older schema lacks the column.
    ("MUSICBRAINZ_SQL_LOADER_IMAGE", "0.6.0", "DATABASE_SCHEMA_IMAGE", "0.9.0"),
    # catalog-api v0.11.0 ranks unfiltered release search from public.release_search, which
    # database-schema v0.11.0 creates. An older schema lacks the table, so search would fail.
    ("CATALOG_API_IMAGE", "0.11.0", "DATABASE_SCHEMA_IMAGE", "0.11.0"),
    # catalog-api v0.11.2 reads release_search's facet columns and master_search, which database-schema
    # v0.12.0 creates. An older schema lacks them, so the facets and master search would fail.
    ("CATALOG_API_IMAGE", "0.11.2", "DATABASE_SCHEMA_IMAGE", "0.12.0"),
    # No row for the 2026-11-09 loaders (discogs-sql-loader v0.6.0, musicbrainz-sql-loader v0.7.0): their
    # loader_seen seen lists need database-schema v0.13.0, but on an older schema a startup probe makes
    # them fall back to touch mode, so no paired floor is required.
)

# Overlay service -> the base service whose pinned image it must run, byte for byte: the external
# store checks use the stack's own postgres and valkey images, so one Dependabot bump moves both.
OVERLAY_IMAGES = {
    "docker-compose.external-postgres.yml": {"store-check-postgres": "postgres"},
    "docker-compose.external-valkey.yml": {"store-check-valkey": "valkey"},
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
    "postgres-exporter": "prometheuscommunity/postgres-exporter",
    "rabbitmq": "rabbitmq",
    # rabbitmqadmin (the HTTP-API CLI that declares the catalog-DLQ cap policy,
    # gm-deployment-8mb.1) is bundled in the same image, so this one-shot init
    # service reuses rabbitmq's own repository rather than adding a new
    # third-party image.
    "rabbitmq-dlq-policy-init": "rabbitmq",
    # data-volume-init (chowns the extractor data volumes to the host UID) runs a shell script, so
    # it reuses the same pinned rabbitmq image (Ubuntu: sh, find, chown) rather than adding one.
    "data-volume-init": "rabbitmq",
    "valkey": "valkey/valkey",
    "valkey-exporter": "oliver006/redis_exporter",
    "victoria-metrics": "victoriametrics/victoria-metrics",
    "victoria-traces": "victoriametrics/victoria-traces",
}

# repository[:tag]@sha256:digest, where the repository may carry a registry host:port.
IMAGE_REFERENCE = re.compile(r"(?P<repository>[^@:]+(?::\d+)?(?:/[^@:]+)*?)(?::(?P<tag>[^@:/]+))?@sha256:[0-9a-f]{64}")

# A tag that names one CPU architecture (`0.162.0-386`, `v1.2.3-arm64`) resolves to a
# single-platform manifest, not the multi-arch index, so its digest runs on that platform
# only. Dependabot proposed exactly this for otel-collector (0.162.0 -> 0.162.0-386): the
# pinned linux/386 binary segfaulted at startup on an arm64 engine, and on amd64 it would
# run as a 32-bit process. A third-party pin must be the index digest of a platform-neutral
# tag. Variant suffixes such as `-alpine` or `-community` are not platforms and stay valid.
PLATFORM_TAG_SUFFIX = re.compile(r"-(?:386|i386|amd64|x86_64|arm64(?:v8)?|aarch64|arm(?:v[5-7])?|armhf|ppc64le|s390x|riscv64)$")


def check_third_party_reference(service_name: str, image: str) -> None:
    """Require a reviewed repository, a non-floating tag, and a digest; any version is fine."""
    assert DIGEST.search(image), f"{service_name} image is not digest-pinned: {image}"
    match = IMAGE_REFERENCE.fullmatch(image)
    assert match, f"{service_name} image is not a repository:tag@sha256 reference: {image}"
    expected = THIRD_PARTY_REPOSITORIES[service_name]
    assert match["repository"] == expected, f"{service_name} runs repository {match['repository']}, not the reviewed {expected}"
    assert match["tag"] and match["tag"] != "latest", f"{service_name} image needs a versioned tag, not {match['tag']!r}: {image}"
    assert not PLATFORM_TAG_SUFFIX.search(match["tag"]), (
        f"{service_name} image tag {match['tag']!r} names a single platform; pin the multi-arch index digest of the platform-neutral tag: {image}"
    )


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
    unpinned = sorted(variable for variable, digest in RELEASED_IMAGE_DIGESTS.items() if digest == UNPUBLISHED_DIGEST)
    assert not unpinned, f"replace the placeholder digest with the reviewed published digest for: {', '.join(unpinned)}"
    assert set(RELEASED_IMAGE_VERSIONS) == set(RELEASED_IMAGE_DIGESTS), "every reviewed release digest needs its version"
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", version) for version in RELEASED_IMAGE_VERSIONS.values()), "a reviewed version is X.Y.Z"
    for store, floors in EXTERNAL_STORE_FLOORS.items():
        assert floors, f"the external {store} floor must name at least one image"
        for variable, floor in floors.items():
            assert variable in IMAGE_OWNERS, f"the external {store} floor names {variable}, which is not an owned image"
            assert floor == UNRELEASED or re.fullmatch(r"\d+\.\d+\.\d+", floor), f"the external {store} floor for {variable} is X.Y.Z or UNRELEASED"
    for variable, floor in CONSOLE_TARGETS_FLOOR.items():
        assert variable in IMAGE_OWNERS, f"the console targets floor names {variable}, which is not an owned image"
        assert floor == UNRELEASED or re.fullmatch(r"\d+\.\d+\.\d+", floor), f"the console targets floor for {variable} is X.Y.Z or UNRELEASED"
    for image, image_floor, needed, needed_floor in PAIRED_IMAGE_FLOORS:
        for variable, floor in ((image, image_floor), (needed, needed_floor)):
            assert variable in IMAGE_OWNERS, f"a paired floor names {variable}, which is not an owned image"
            assert floor == UNRELEASED or re.fullmatch(r"\d+\.\d+\.\d+", floor), f"the paired floor for {variable} is X.Y.Z or UNRELEASED"
        # One value alone would leave the rule silently inert.
        assert (image_floor == UNRELEASED) == (needed_floor == UNRELEASED), f"the floors of {image} and {needed} are both UNRELEASED or both set"
    problems = paired_floor_problems(RELEASED_IMAGE_VERSIONS)
    assert not problems, "; ".join(problems)
    assert set(INTERNAL_IMAGES).isdisjoint(THIRD_PARTY_REPOSITORIES), "a service runs either a released GrooveMap image or a public one"
    assert all(
        repository and "@" not in repository and ":" not in repository.rsplit("/", 1)[-1] for repository in THIRD_PARTY_REPOSITORIES.values()
    ), "a reviewed third-party repository is a bare name, with no tag or digest"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def paired_floor_problems(versions: Mapping[str, str], floors: tuple[tuple[str, str, str, str], ...] = PAIRED_IMAGE_FLOORS) -> list[str]:
    """Each pair in `floors` that the reviewed `versions` break. See PAIRED_IMAGE_FLOORS."""
    problems = []
    for image, image_floor, needed, needed_floor in floors:
        if UNRELEASED in (image_floor, needed_floor) or version_key(versions[image]) < version_key(image_floor):
            continue
        if version_key(versions[needed]) < version_key(needed_floor):
            problems.append(f"{image} {versions[image]} needs {needed} {needed_floor} or newer, and the reviewed set has {versions[needed]}")
    return problems


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


def check_overlay_images(base_text: str, overlay_texts: Mapping[str, str]) -> None:
    """Each overlay service in OVERLAY_IMAGES runs exactly the image of its base service."""
    import yaml  # noqa: PLC0415

    class ComposeLoader(yaml.SafeLoader):
        """Reads Compose's merge tags (`!reset`, `!override`) as their plain value."""

    ComposeLoader.add_multi_constructor(
        "!", lambda loader, _suffix, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None
    )
    base = yaml.safe_load(base_text)["services"]
    for name, text in overlay_texts.items():
        services = yaml.load(text, Loader=ComposeLoader)["services"]  # noqa: S506 - a SafeLoader subclass
        for service, source in OVERLAY_IMAGES[name].items():
            assert services[service]["image"] == base[source]["image"], f"{name}: {service} must run {source}'s image {base[source]['image']}"


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
    check_overlay_images((root / "docker-compose.yml").read_text(), {name: (root / name).read_text() for name in OVERLAY_IMAGES})
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
