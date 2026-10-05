"""Production log context must reach every source-owned application container."""

from pathlib import Path

import yaml

from tests.deploy.test_docker_compose_prod import _load_compose


ROOT = Path(__file__).resolve().parents[2]


def test_production_sets_log_environment_for_every_internal_image() -> None:
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    production = _load_compose(ROOT / "docker-compose.prod.yml")
    application_services = {name for name, service in base["services"].items() if service["image"].startswith("${")}
    assert {"schema-init", "embeddings"} <= application_services
    for name in application_services:
        environment = production["services"][name]["environment"]
        assert environment["ENVIRONMENT"] == "production", name
        # The log label and existing telemetry resource label are separate inputs.
        assert "deployment.environment.name=prod" in environment["OTEL_RESOURCE_ATTRIBUTES"], name
        assert "ENVIRONMENT" not in base["services"][name]["environment"], name
