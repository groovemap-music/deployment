"""Contracts for the retained Neo4j graph authority and recovery record."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


class _ComposeLoader(yaml.SafeLoader):
    """Treat Compose merge tags as their underlying YAML values."""


def _construct_tagged(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    assert isinstance(node, yaml.ScalarNode)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor(None, _construct_tagged)  # type: ignore[arg-type]


def _compose(name: str) -> dict[str, Any]:
    loaded = yaml.load((REPO_ROOT / name).read_text(), Loader=_ComposeLoader)  # noqa: S506 -- trusted repository YAML
    assert isinstance(loaded, dict)
    return loaded


def test_base_api_is_wired_directly_to_neo4j() -> None:
    api = _compose("docker-compose.yml")["services"]["api"]
    assert api["environment"]["NEO4J_HOST"] == "neo4j"
    assert "GRAPH_BACKEND" not in api["environment"]
    assert "neo4j" in api["depends_on"]


def test_production_overlay_keeps_neo4j_secret_without_a_backend_selector() -> None:
    prod = _compose("docker-compose.prod.yml")
    api = prod["services"]["api"]
    assert "GRAPH_BACKEND" not in api["environment"]
    assert api["environment"]["NEO4J_PASSWORD_FILE"] == "/run/secrets/neo4j_password"
    assert "neo4j_password" in api["secrets"]


def test_neo4j_persistence_health_and_resource_settings_remain_wired() -> None:
    base_neo4j = _compose("docker-compose.yml")["services"]["neo4j"]
    prod_neo4j = _compose("docker-compose.prod.yml")["services"]["neo4j"]

    assert "neo4j_data:/data" in base_neo4j["volumes"]
    assert "neo4j_logs:/logs" in base_neo4j["volumes"]
    assert "cypher-shell" in " ".join(prod_neo4j["healthcheck"]["test"])
    assert "neo4j_password" in prod_neo4j["secrets"]
    assert prod_neo4j["deploy"]["resources"]["limits"]["memory"] == "${NEO4J_MEMORY_LIMIT:-48G}"


def test_no_pgq_activation_remains_in_deployable_configuration() -> None:
    for name in ("docker-compose.yml", "docker-compose.prod.yml", ".env.example", "config/validation.env"):
        text = (REPO_ROOT / name).read_text().lower()
        assert "graph_backend: postgres" not in text
        assert "graph_backend=postgres" not in text
        assert "enable_postgres_graph" not in text
        assert "postgres_graph_enabled" not in text


def test_runbook_records_git_recovery_and_release_boundary() -> None:
    runbook = (REPO_ROOT / "docs" / "graph-backend-continuity.md").read_text()
    for required in (
        "77449dd807ac97fa0d2dfbb1f4374f72b40b30df",
        "9a50949b1810f3e61adae2f89b86acec2e20c4a3",
        "24c95868af6f0f5f28bc8bd61418b32ac0039185",
        "baa289b6264d567384648d8c6ab7a013a9287325",
        "cd4e59931d586eab5d6ba0c2462ac4ce7853fc38",
        "immutable registry manifest digest",
        "PostgreSQL 19 plus pgvector",
        "docker compose down",
        "service_healthy",
    ):
        assert required in runbook
