"""Contracts for the disposable released-image stack verifier."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke-released-stack.sh"


def test_released_stack_verifier_requires_every_internal_image() -> None:
    text = SCRIPT.read_text()
    for variable in (
        "DATABASE_SCHEMA_IMAGE",
        "CATALOG_API_IMAGE",
        "CATALOG_INGESTION_IMAGE",
        "DISCOGS_GRAPH_ENRICHER_IMAGE",
        "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE",
        "DISCOGS_SQL_LOADER_IMAGE",
        "MUSICBRAINZ_SQL_LOADER_IMAGE",
        "OPERATIONS_CONSOLE_IMAGE",
        "GRAPH_EXPLORER_IMAGE",
        "ANALYTICS_ENGINE_IMAGE",
    ):
        assert variable in text
    assert "@sha256:[0-9a-f]{64}" in text
    assert "validation-only digest" in text


def test_released_stack_verifier_orders_schema_before_applications() -> None:
    text = SCRIPT.read_text()
    first_schema_run = text.index("run --rm --no-deps schema-init")
    idempotency_run = text.index("run --rm --no-deps schema-init", first_schema_run + 1)
    application_start = text.index('up -d --wait "${GM_APPLICATION_SERVICES[@]}"')
    assert first_schema_run < idempotency_run < application_start

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in (
        "api",
        "graphinator",
        "brainzgraphinator",
        "tableinator",
        "brainztableinator",
        "dashboard",
        "explore",
        "insights",
    ):
        assert compose["services"][name]["depends_on"]["schema-init"]["condition"] == "service_completed_successfully"


def test_released_stack_verifier_checks_health_and_graceful_stop() -> None:
    text = SCRIPT.read_text()
    assert "GM_INFRASTRUCTURE_SERVICES=(rabbitmq postgres neo4j redis)" in text
    assert 'up -d --wait "${GM_INFRASTRUCTURE_SERVICES[@]}"' in text
    assert 'stop --timeout 30 "${GM_APPLICATION_SERVICES[@]}"' in text


def test_released_stack_failures_retain_logs_and_always_clean_up() -> None:
    text = SCRIPT.read_text()
    assert "ps --all >&2 || true" in text
    assert "logs --timestamps --no-color --tail 200 >&2 || true" in text
    assert "down --volumes --remove-orphans || true" in text
    assert "trap gm_cleanup EXIT" in text
