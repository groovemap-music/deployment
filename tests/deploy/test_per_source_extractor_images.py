"""Regression tests for gm-deployment-989.1 — the per-source extractor cutover.

ADR 0005 splits the combined `catalog-ingestion` producer into `discogs-ingestion`
and `musicbrainz-ingestion`, each owning its own image, release, and rollback
boundary. Deployment consequences this module pins:

- the retired `CATALOG_INGESTION_IMAGE` variable is gone from every tracked input,
  replaced by one required variable per source;
- each variable promotes its own owning repository's GHCR image, so a promotion
  cannot silently cross the ownership boundary;
- the per-source entrypoints take no ``--source`` argument (each binary serves one
  source), so a leftover ``command:`` would fail at startup with an unexpected
  argument rather than degrade quietly;
- `DISCOGS_HEALTH_URL` is gone. It existed only for the combined deployment's
  `wait_for_discogs_idle` compatibility path, which the split removed. The
  source-owned containers must ingest concurrently, with no cross-container health
  polling, ordering, shared lock, or mutual exclusion; and
- the deployment identities ADR 0005 freezes (service name, container name,
  hostname, health port, data volume) survive the cutover, which is what keeps a
  rollback source-local.
"""

from __future__ import annotations

import re
import runpy
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]

RETIRED_VARIABLE = "CATALOG_INGESTION_IMAGE"

# Compose service -> (required image variable, owning repository / GHCR image name).
PER_SOURCE_EXTRACTORS = {
    "extractor-discogs": ("DISCOGS_INGESTION_IMAGE", "discogs-ingestion"),
    "extractor-musicbrainz": ("MUSICBRAINZ_INGESTION_IMAGE", "musicbrainz-ingestion"),
}

# Identities ADR 0005 freezes across the split so that rollback stays source-local.
FROZEN_IDENTITIES = {
    "extractor-discogs": {
        "container_name": "groovemap-extractor-discogs",
        "hostname": "extractor-discogs",
        "volume": "discogs_data:/discogs-data",
    },
    "extractor-musicbrainz": {
        "container_name": "groovemap-extractor-musicbrainz",
        "hostname": "extractor-musicbrainz",
        "volume": "musicbrainz_data:/musicbrainz-data",
    },
}

# Inputs that promote an image. None may still name the retired combined variable.
# The runbook is deliberately excluded: it names the variable to tell operators it is
# gone, which is a record of the cutover rather than a live promotion.
TRACKED_IMAGE_INPUTS = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    ".env.example",
    "config/validation.env",
    "scripts/check-images.py",
    "docs/dockerfile-standards.md",
    "docs/architecture.md",
    "docs/troubleshooting.md",
)


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    return loaded


def _env_assignments(relative_path: str) -> dict[str, str]:
    text = (REPO_ROOT / relative_path).read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace so prose-wrapping doesn't break substring checks."""
    return re.sub(r"\s+", " ", text)


def _run_check_images() -> dict[str, Any]:
    """Execute the validation script in-process and return its module namespace."""
    return runpy.run_path(str(REPO_ROOT / "scripts" / "check-images.py"), run_name="check_images")


class TestRetiredCombinedImage:
    def test_no_tracked_input_still_names_the_combined_variable(self) -> None:
        for relative_path in TRACKED_IMAGE_INPUTS:
            assert RETIRED_VARIABLE not in (REPO_ROOT / relative_path).read_text(), f"{relative_path} still references {RETIRED_VARIABLE}"

    def test_runbook_names_the_combined_variable_only_as_retired(self) -> None:
        runbook = (REPO_ROOT / "docs" / "maintenance.md").read_text()
        assert f"`{RETIRED_VARIABLE}` variable no longer exist" in _normalize_whitespace(runbook)

    def test_env_files_declare_one_variable_per_source(self) -> None:
        for relative_path in (".env.example", "config/validation.env"):
            assignments = _env_assignments(relative_path)
            assert RETIRED_VARIABLE not in assignments
            for variable, repository in PER_SOURCE_EXTRACTORS.values():
                assert assignments[variable].startswith(f"ghcr.io/groovemap-music/{repository}@sha256:"), (
                    f"{relative_path}: {variable} must promote the {repository} image by digest"
                )

    def test_check_images_requires_each_source_variable(self) -> None:
        namespace = _run_check_images()
        for service, (variable, repository) in PER_SOURCE_EXTRACTORS.items():
            assert namespace["INTERNAL_IMAGES"][service] == variable, f"{service} must require {variable}"
            assert namespace["IMAGE_OWNERS"][variable] == repository, f"{variable} must be owned by {repository}"


class TestPerSourceImages:
    def test_each_extractor_requires_its_own_image_variable(self) -> None:
        services = _compose()["services"]
        for service, (variable, _repository) in PER_SOURCE_EXTRACTORS.items():
            assert services[service]["image"].startswith(f"${{{variable}:?"), f"{service} must require {variable}"

    def test_extractors_do_not_share_an_image_variable(self) -> None:
        services = _compose()["services"]
        images = {services[service]["image"] for service in PER_SOURCE_EXTRACTORS}
        assert len(images) == len(PER_SOURCE_EXTRACTORS)

    def test_extractors_pass_no_source_selection_argument(self) -> None:
        """Each source-owned binary serves exactly one source; the combined image's
        ``--source`` flag no longer exists, so passing it would abort startup."""
        services = _compose()["services"]
        for service in PER_SOURCE_EXTRACTORS:
            assert "command" not in services[service], f"{service} must not pass a source-selection argument"


class TestConcurrentIngestion:
    def test_musicbrainz_does_not_wait_on_discogs_health(self) -> None:
        environment = _compose()["services"]["extractor-musicbrainz"].get("environment", {}) or {}
        assert "DISCOGS_HEALTH_URL" not in environment

    def test_no_compose_file_wires_cross_source_health_polling(self) -> None:
        for relative_path in ("docker-compose.yml", "docker-compose.prod.yml"):
            assert "DISCOGS_HEALTH_URL" not in (REPO_ROOT / relative_path).read_text(), f"{relative_path} still wires cross-source health polling"

    def test_neither_extractor_depends_on_the_other(self) -> None:
        services = _compose()["services"]
        for service in PER_SOURCE_EXTRACTORS:
            depends_on = services[service].get("depends_on", {}) or {}
            assert not set(depends_on) & set(PER_SOURCE_EXTRACTORS), f"{service} must not order itself against the other source"


class TestFrozenDeploymentIdentities:
    def test_identities_survive_the_cutover(self) -> None:
        services = _compose()["services"]
        for service, expected in FROZEN_IDENTITIES.items():
            spec = services[service]
            assert spec["container_name"] == expected["container_name"]
            assert spec["hostname"] == expected["hostname"]
            assert expected["volume"] in spec["volumes"]

    def test_health_port_is_unchanged(self) -> None:
        services = _compose()["services"]
        for service in PER_SOURCE_EXTRACTORS:
            assert services[service]["healthcheck"]["test"] == ["CMD", "curl", "-f", "http://localhost:8000/health"]


class TestCutoverRunbook:
    """The operator-facing procedure is part of the deliverable, not incidental prose."""

    def test_runbook_records_the_per_source_cutover_and_rollback(self) -> None:
        runbook = (REPO_ROOT / "docs" / "maintenance.md").read_text()
        assert "## Per-source extractor cutover" in runbook
        for variable, repository in PER_SOURCE_EXTRACTORS.values():
            assert variable in runbook
            assert repository in runbook
        assert "one source at a time" in runbook.lower()
        assert "rollback is source-local" in runbook.lower()
        assert "untracked" in runbook.lower()

    def test_runbook_records_the_published_producer_digests(self) -> None:
        runbook = (REPO_ROOT / "docs" / "maintenance.md").read_text()
        assert "sha256:4a961aab647bb830074414b30e121d927c8287d2a1b2e4d61a34f42a1b50e94b" in runbook
        assert "sha256:2b348519450cc9811fe8d194d0ef4b4dd3ead901b2f8e5883dec83a839bd9b37" in runbook


class TestOwnershipDocumentation:
    def test_standards_table_names_each_source_repository(self) -> None:
        standards = (REPO_ROOT / "docs" / "dockerfile-standards.md").read_text()
        for service, (variable, repository) in PER_SOURCE_EXTRACTORS.items():
            assert f"| `{service}` | [`{repository}`](https://github.com/groovemap-music/{repository}) | `{variable}` |" in standards
