"""Regression tests for service and infrastructure telemetry wiring (gm-deployment-gxr.2).

A service only exports if compose hands it the standard OTEL env triple, and
infrastructure metrics only appear if the collector is told to scrape them.
Both halves are silent when they break — a missing env var produces no error,
just an absent dashboard — so they are pinned here:

- every internal-image service gets the endpoint, its own service name, and the
  shared resource attributes, with the environment tag flipped to ``prod`` by
  the production overlay;
- every internal-image service depends on the collector with
  ``service_started`` and never ``service_healthy``, so telemetry can never
  block an application from booting;
- RabbitMQ enables ``rabbitmq_prometheus``, and the two exporters exist,
  digest pinned, unpublished, and credentialed the same way the app services
  are;
- the collector scrapes all four infrastructure targets under job names that
  match the compose service keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.deploy.test_docker_compose_prod import _load_compose


REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_CONFIG = REPO_ROOT / "config" / "otel-collector.yaml"
ENABLED_PLUGINS = REPO_ROOT / "config" / "rabbitmq-enabled-plugins"

OTLP_ENDPOINT = "http://otel-collector:4318"
DEV_RESOURCE_ATTRIBUTES = "service.namespace=groovemap,deployment.environment.name=dev"
PROD_RESOURCE_ATTRIBUTES = "service.namespace=groovemap,deployment.environment.name=prod"

# Every service running an internally released GrooveMap image. These are the
# services that carry an OTEL SDK and therefore must be wired for export.
INSTRUMENTED_SERVICES = (
    "schema-init",
    "api",
    "extractor-discogs",
    "extractor-musicbrainz",
    "graphinator",
    "brainzgraphinator",
    "tableinator",
    "brainztableinator",
    "dashboard",
    "explore",
    "insights",
)

EXPORTERS = ("postgres-exporter", "redis-exporter")

# job_name -> scrape target. Job names match the compose service keys because
# that is what the dashboards filter on.
EXPECTED_SCRAPE_JOBS = {
    "rabbitmq": "rabbitmq:15692",
    "postgres-exporter": "postgres-exporter:9187",
    "redis-exporter": "redis-exporter:9121",
    "otel-collector": "otel-collector:8888",
}


def _base_compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    return loaded


def _prod_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.prod.yml")


def _collector_config() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COLLECTOR_CONFIG.read_text())
    return loaded


def _scrape_configs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = _collector_config()["receivers"]["prometheus"]["config"]["scrape_configs"]
    return jobs


class TestEveryInstrumentedServiceExports:
    """The env triple is the whole contract — there are no GrooveMap-specific knobs."""

    def test_endpoint_is_the_collector(self) -> None:
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert services[name]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == OTLP_ENDPOINT, name

    def test_service_name_is_the_compose_key(self) -> None:
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert services[name]["environment"]["OTEL_SERVICE_NAME"] == name

    def test_resource_attributes_tag_namespace_and_dev_environment(self) -> None:
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert services[name]["environment"]["OTEL_RESOURCE_ATTRIBUTES"] == DEV_RESOURCE_ATTRIBUTES, name

    def test_no_groovemap_specific_telemetry_variables(self) -> None:
        """Only the SDK's own variables are allowed to configure telemetry."""
        allowed = {
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_SERVICE_NAME",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_METRICS_EXPORTER",
            "OTEL_METRIC_EXPORT_INTERVAL",
        }
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            telemetry_keys = {key for key in services[name]["environment"] if key.startswith("OTEL_")}
            assert telemetry_keys <= allowed, (name, sorted(telemetry_keys - allowed))

    def test_third_party_services_are_not_wired_for_otlp(self) -> None:
        """rabbitmq/postgres/neo4j/redis and the exporters carry no OTEL SDK."""
        services = _base_compose()["services"]
        for name in ("rabbitmq", "postgres", "neo4j", "redis", *EXPORTERS):
            environment = services[name].get("environment", {}) or {}
            assert not [key for key in environment if key.startswith("OTEL_")], name


class TestTelemetryNeverBlocksStartup:
    def test_every_instrumented_service_depends_on_the_collector(self) -> None:
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert "otel-collector" in services[name]["depends_on"], name

    def test_the_dependency_is_service_started_not_service_healthy(self) -> None:
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            condition = services[name]["depends_on"]["otel-collector"]["condition"]
            assert condition == "service_started", f"{name} would let a sick collector block the app: {condition}"


class TestProdOverlayRetagsTheEnvironment:
    def test_prod_flips_the_environment_attribute(self) -> None:
        services = _prod_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert services[name]["environment"]["OTEL_RESOURCE_ATTRIBUTES"] == PROD_RESOURCE_ATTRIBUTES, name

    def test_prod_does_not_re_declare_the_endpoint_or_service_name(self) -> None:
        """The base values carry over; re-stating them invites drift."""
        services = _prod_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            environment = services[name]["environment"]
            assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in environment, name
            assert "OTEL_SERVICE_NAME" not in environment, name


class TestRabbitMqPrometheusPlugin:
    def test_enabled_plugins_file_lists_the_prometheus_plugin(self) -> None:
        plugins = ENABLED_PLUGINS.read_text()
        assert "rabbitmq_prometheus" in plugins
        assert "rabbitmq_management" in plugins, "the management plugin must survive; the console reads its API"

    def test_enabled_plugins_file_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["rabbitmq"]["volumes"]
        assert "./config/rabbitmq-enabled-plugins:/etc/rabbitmq/enabled_plugins:ro" in volumes

    def test_prometheus_port_is_not_published(self) -> None:
        ports = [str(port) for port in _base_compose()["services"]["rabbitmq"]["ports"]]
        assert not any("15692" in port for port in ports), "15692 is scraped internally, not exposed"


class TestInfrastructureExporters:
    def test_both_exporters_exist(self) -> None:
        services = _base_compose()["services"]
        for name in EXPORTERS:
            assert name in services, f"docker-compose.yml is missing {name}"

    def test_exporters_are_digest_pinned(self) -> None:
        services = _base_compose()["services"]
        for name in EXPORTERS:
            image = services[name]["image"]
            digest = image.partition("@sha256:")[2]
            assert len(digest) == 64, f"{name} is not digest pinned: {image}"

    def test_exporters_publish_no_host_ports(self) -> None:
        services = _base_compose()["services"]
        for name in EXPORTERS:
            assert "ports" not in services[name], f"{name} exposes server internals and must stay internal"

    def test_exporters_wait_for_their_backing_service(self) -> None:
        services = _base_compose()["services"]
        assert services["postgres-exporter"]["depends_on"]["postgres"]["condition"] == "service_healthy"
        assert services["redis-exporter"]["depends_on"]["redis"]["condition"] == "service_healthy"

    def test_exporters_are_hardened(self) -> None:
        services = _base_compose()["services"]
        for name in EXPORTERS:
            spec = services[name]
            assert "no-new-privileges:true" in spec["security_opt"], name
            assert spec["cap_drop"] == ["ALL"], name
            assert spec["read_only"] is True, name

    def test_postgres_exporter_targets_the_stack_database(self) -> None:
        environment = _base_compose()["services"]["postgres-exporter"]["environment"]
        assert environment["DATA_SOURCE_URI"] == "postgres:5432/groovemap?sslmode=disable"
        assert environment["DATA_SOURCE_USER"] == "groovemap"

    def test_redis_exporter_targets_the_stack_cache(self) -> None:
        environment = _base_compose()["services"]["redis-exporter"]["environment"]
        assert environment["REDIS_ADDR"] == "redis://redis:6379"


class TestExporterCredentialsInProd:
    """The exporters must follow the same _FILE secret pattern as the app services."""

    def test_postgres_exporter_reads_file_backed_credentials(self) -> None:
        exporter = _prod_compose()["services"]["postgres-exporter"]
        assert exporter["environment"]["DATA_SOURCE_USER_FILE"] == "/run/secrets/postgres_username"
        assert exporter["environment"]["DATA_SOURCE_PASS_FILE"] == "/run/secrets/postgres_password"
        assert exporter["environment"]["DATA_SOURCE_USER"] is None, "the dev value must be unset so the file wins"
        assert exporter["environment"]["DATA_SOURCE_PASS"] is None
        assert set(exporter["secrets"]) == {"postgres_username", "postgres_password"}

    def test_redis_exporter_reads_the_file_backed_password(self) -> None:
        exporter = _prod_compose()["services"]["redis-exporter"]
        assert exporter["environment"]["REDIS_PASSWORD_FILE"] == "/run/secrets/redis_password"
        assert "redis_password" in exporter["secrets"]


class TestCollectorScrapesInfrastructure:
    def test_the_prometheus_receiver_is_in_the_metrics_pipeline(self) -> None:
        pipeline = _collector_config()["service"]["pipelines"]["metrics"]
        assert "prometheus" in pipeline["receivers"]
        assert "otlp" in pipeline["receivers"], "application push must keep working alongside the scrape"

    def test_every_expected_target_is_scraped(self) -> None:
        actual = {job["job_name"]: job["static_configs"][0]["targets"] for job in _scrape_configs()}
        assert set(actual) == set(EXPECTED_SCRAPE_JOBS), sorted(actual)
        for job_name, target in EXPECTED_SCRAPE_JOBS.items():
            assert actual[job_name] == [target], job_name

    def test_job_names_match_compose_service_keys(self) -> None:
        services = _base_compose()["services"]
        for job in _scrape_configs():
            assert job["job_name"] in services, f"job {job['job_name']} matches no compose service"

    def test_every_job_sets_a_scrape_interval(self) -> None:
        for job in _scrape_configs():
            assert job["scrape_interval"], job["job_name"]

    def test_no_application_service_is_scraped(self) -> None:
        """Applications push; scraping one would double-count and duplicate labels."""
        scraped = {job["job_name"] for job in _scrape_configs()}
        assert scraped.isdisjoint(INSTRUMENTED_SERVICES)


class TestWiringDocumentation:
    def test_env_var_table_and_exporter_inventory_are_documented(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for token in ("OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES", "postgres-exporter", "redis-exporter", "15692"):
            assert token in doc, f"docs/observability.md does not document {token}"

    def test_every_instrumented_service_appears_in_the_env_table(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for name in INSTRUMENTED_SERVICES:
            assert f"`{name}`" in doc, f"docs/observability.md does not list {name}"
