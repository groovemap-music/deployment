"""Deploy-config regression tests for a batch of P3 config/deploy bug-hunt beads.

Covers:

- The extractor image never honored STARTUP_DELAY (no start.sh shim, raw
  ENTRYPOINT), so the dead ``STARTUP_DELAY: "30"`` knob must not remain on
  extractor-discogs/extractor-musicbrainz — it silently did nothing and misled
  operators into believing a startup stagger existed.
- The Python consumer images run a PID-1 shell
  (``/app/start.sh``) that sleeps ``STARTUP_DELAY`` seconds before ``exec``-ing
  python. A non-interactive shell installs no SIGTERM handler, so without an init
  process forwarding signals, a stop/restart landing in that window is silently
  ignored until Docker's SIGKILL grace period. ``init: true`` (tini as PID 1) fixes
  this uniformly for every affected service.
- Operator helpers that invoke the API container must match the
  Compose ``container_name:`` the API service actually runs under.

These tests parse the compose YAML directly (no ``docker`` binary required), mirroring
the pattern in test_docker_compose_prod.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]

# Every Python service whose Dockerfile builds a /app/start.sh shim that sleeps
# STARTUP_DELAY before `exec`-ing python (i.e. runs a real PID-1 shell at startup).
# schema-init execs python directly (no shim) and extractor-* runs the raw Rust
# binary (no shim) so neither needs init: true for this reason.
PYTHON_START_SH_SERVICES = [
    "api",
    "graphinator",
    "brainzgraphinator",
    "tableinator",
    "brainztableinator",
    "dashboard",
    "explore",
    "insights",
]

EXTRACTOR_SERVICES = ["extractor-discogs", "extractor-musicbrainz"]


def _load_compose(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def _base_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.yml")


class TestExtractorStartupDelayRemoved:
    """STARTUP_DELAY was dead on the catalog-ingestion image."""

    def test_extractor_services_have_no_startup_delay_env(self) -> None:
        compose = _base_compose()
        for name in EXTRACTOR_SERVICES:
            env = compose["services"][name].get("environment", {}) or {}
            assert "STARTUP_DELAY" not in env, f"{name} still sets the dead STARTUP_DELAY knob"

    def test_extractor_services_consume_their_source_owned_images(self) -> None:
        """gm-deployment-989.1: the combined image was split per source (ADR 0005),
        so the two extractors no longer share one released image."""
        compose = _base_compose()
        images = {compose["services"][name]["image"] for name in EXTRACTOR_SERVICES}
        assert len(images) == 2
        assert compose["services"]["extractor-discogs"]["image"].startswith("${DISCOGS_INGESTION_IMAGE:?")
        assert compose["services"]["extractor-musicbrainz"]["image"].startswith("${MUSICBRAINZ_INGESTION_IMAGE:?")


class TestPythonServicesForwardSignalsDuringStartupDelay:
    """Use init so SIGTERM reaches each Python image's PID-1 shell."""

    def test_start_sh_python_services_have_init_true(self) -> None:
        compose = _base_compose()
        for name in PYTHON_START_SH_SERVICES:
            assert compose["services"][name].get("init") is True, f"{name} is missing init: true"

    def test_extractor_services_do_not_need_init_true(self) -> None:
        """extractor-* run the raw binary directly (no shell/sleep at PID 1), so
        init: true is not required there — its absence is intentional, not an
        oversight of the fix-one-fix-all sweep."""
        compose = _base_compose()
        for name in EXTRACTOR_SERVICES:
            assert compose["services"][name].get("init") is not True

    def test_schema_init_does_not_need_init(self) -> None:
        """The deployment consumes the schema repository's released image."""
        compose = _base_compose()
        assert compose["services"]["schema-init"].get("init") is not True
        assert "build" not in compose["services"]["schema-init"]
        assert compose["services"]["schema-init"]["image"].startswith("${DATABASE_SCHEMA_IMAGE:?")


class TestRepositoryBoundary:
    def test_no_service_uses_a_sibling_build_context(self) -> None:
        compose = _base_compose()
        assert all("build" not in service for service in compose["services"].values())


class TestOperatorHelperServiceBoundary:
    """Operator helpers address Compose services, not fixed container names."""

    def test_reset_password_uses_compose_services(self) -> None:
        script = (REPO_ROOT / "scripts" / "reset-password.sh").read_text()
        assert "docker exec" not in script
        assert 'exec -T -e "RESET_PW=${GM_NEW_PASSWORD}" api python3' in script
        assert 'exec -T -e "PGPASSWORD=${GM_POSTGRES_PASSWORD}" postgres' in script


class TestBatchProcessingDeploymentContract:
    """Stack defaults retain the proven throughput and drain thresholds."""

    def test_neo4j_consumers_use_optimized_batch_settings(self) -> None:
        services = _base_compose()["services"]
        for name in ("graphinator", "brainzgraphinator"):
            environment = services[name]["environment"]
            assert int(environment["NEO4J_BATCH_SIZE"]) >= 500
            assert float(environment["NEO4J_BATCH_FLUSH_INTERVAL"]) <= 2.0

    def test_postgres_consumers_use_optimized_batch_settings(self) -> None:
        services = _base_compose()["services"]
        for name in ("tableinator", "brainztableinator"):
            environment = services[name]["environment"]
            assert int(environment["POSTGRES_BATCH_SIZE"]) >= 500
            assert float(environment["POSTGRES_BATCH_FLUSH_INTERVAL"]) <= 2.0
