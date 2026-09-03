"""Regression tests for the OpenTelemetry metrics backend (gm-deployment-gxr.1).

The three backend services are the only thing that makes exported telemetry
visible, so their wiring is pinned here rather than left to a live smoke run:

- all three images are digest pinned and consumed from public registries;
- the collector pipeline stays ``otlp -> memory_limiter -> batch ->
  prometheusremotewrite`` pointed at Prometheus' remote-write endpoint, with
  memory_limiter first so back-pressure precedes batching;
- Prometheus runs as a remote-write receiver with a bounded retention;
- the production overlay sources the Grafana admin password from a Docker
  secret and turns anonymous access off.

The tests parse the compose and collector YAML directly — no ``docker`` binary
and no running environment are required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.deploy.test_docker_compose_prod import _load_compose


REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_CONFIG = REPO_ROOT / "config" / "otel-collector.yaml"
PROMETHEUS_CONFIG = REPO_ROOT / "config" / "prometheus.yml"

OBSERVABILITY_SERVICES = ("otel-collector", "prometheus", "grafana")

DIGEST_PREFIX = "@sha256:"


def _base_compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    return loaded


def _prod_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.prod.yml")


def _collector_config() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COLLECTOR_CONFIG.read_text())
    return loaded


class TestBackendServicesExist:
    def test_all_three_services_are_defined(self) -> None:
        services = _base_compose()["services"]
        for name in OBSERVABILITY_SERVICES:
            assert name in services, f"docker-compose.yml is missing the {name} service"

    def test_images_are_digest_pinned(self) -> None:
        services = _base_compose()["services"]
        for name in OBSERVABILITY_SERVICES:
            image = services[name]["image"]
            digest = image.partition(DIGEST_PREFIX)[2]
            assert len(digest) == 64, f"{name} is not digest pinned: {image}"
            assert all(character in "0123456789abcdef" for character in digest), image

    def test_images_come_from_the_expected_upstream_repositories(self) -> None:
        services = _base_compose()["services"]
        expected = {
            "otel-collector": "otel/opentelemetry-collector-contrib",
            "prometheus": "prom/prometheus",
            "grafana": "grafana/grafana",
        }
        for name, repository in expected.items():
            assert services[name]["image"].startswith(f"{repository}:"), services[name]["image"]

    def test_services_have_healthchecks(self) -> None:
        services = _base_compose()["services"]
        for name in OBSERVABILITY_SERVICES:
            assert services[name].get("healthcheck", {}).get("test"), f"{name} has no healthcheck"

    def test_services_use_default_logging_and_hardening(self) -> None:
        services = _base_compose()["services"]
        for name in OBSERVABILITY_SERVICES:
            spec = services[name]
            assert spec["logging"]["options"]["max-size"] == "10m", name
            assert "no-new-privileges:true" in spec["security_opt"], name

    def test_named_volumes_are_declared(self) -> None:
        volumes = _base_compose()["volumes"]
        assert "prometheus_data" in volumes
        assert "grafana_data" in volumes

    def test_collector_publishes_no_host_ports(self) -> None:
        """OTLP ingest is internal-only; nothing outside the stack pushes metrics."""
        assert "ports" not in _base_compose()["services"]["otel-collector"]

    def test_grafana_is_published_on_3000(self) -> None:
        ports = _base_compose()["services"]["grafana"]["ports"]
        assert any(str(port).endswith("3000:3000") or str(port) == "3000:3000" for port in ports)


class TestConfigFilesAreMountedReadOnly:
    def test_collector_config_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["otel-collector"]["volumes"]
        assert "./config/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro" in volumes

    def test_prometheus_config_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["prometheus"]["volumes"]
        assert "./config/prometheus.yml:/etc/prometheus/prometheus.yml:ro" in volumes

    def test_grafana_provisioning_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["grafana"]["volumes"]
        assert "./config/grafana/provisioning:/etc/grafana/provisioning:ro" in volumes
        assert "./config/grafana/dashboards:/var/lib/grafana/dashboards:ro" in volumes

    def test_config_files_exist(self) -> None:
        assert COLLECTOR_CONFIG.is_file()
        assert PROMETHEUS_CONFIG.is_file()


class TestCollectorPipeline:
    def test_config_parses(self) -> None:
        assert isinstance(_collector_config(), dict)

    def test_otlp_receiver_serves_grpc_and_http(self) -> None:
        protocols = _collector_config()["receivers"]["otlp"]["protocols"]
        assert protocols["grpc"]["endpoint"] == "0.0.0.0:4317"
        assert protocols["http"]["endpoint"] == "0.0.0.0:4318"

    def test_metrics_pipeline_shape(self) -> None:
        pipeline = _collector_config()["service"]["pipelines"]["metrics"]
        assert "otlp" in pipeline["receivers"]
        assert pipeline["processors"] == ["memory_limiter", "batch"], "memory_limiter must run before batch"
        assert pipeline["exporters"] == ["prometheusremotewrite"]

    def test_memory_limiter_and_batch_are_configured(self) -> None:
        processors = _collector_config()["processors"]
        assert processors["memory_limiter"]["limit_mib"] > 0
        assert processors["memory_limiter"]["check_interval"]
        assert processors["batch"]["send_batch_size"] > 0

    def test_remote_write_targets_prometheus(self) -> None:
        exporter = _collector_config()["exporters"]["prometheusremotewrite"]
        assert exporter["http"]["endpoint"] == "http://prometheus:9090/api/v1/write"
        assert exporter["http"]["tls"]["insecure"] is True, "plain HTTP on the internal network needs insecure: true"

    def test_resource_attributes_become_labels(self) -> None:
        exporter = _collector_config()["exporters"]["prometheusremotewrite"]
        assert exporter["resource_to_telemetry_conversion"]["enabled"] is True

    def test_self_metrics_are_exposed_on_8888(self) -> None:
        readers = _collector_config()["service"]["telemetry"]["metrics"]["readers"]
        prometheus_reader = readers[0]["pull"]["exporter"]["prometheus"]
        assert prometheus_reader["port"] == 8888
        # S104: binding the self-metrics endpoint to all interfaces is required —
        # the collector scrapes it by compose hostname, which localhost would not match.
        assert prometheus_reader["host"] == "0.0.0.0"  # noqa: S104

    def test_health_check_extension_is_enabled(self) -> None:
        config = _collector_config()
        assert config["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
        assert "health_check" in config["service"]["extensions"]


class TestPrometheusServer:
    def test_remote_write_receiver_is_enabled(self) -> None:
        command = _base_compose()["services"]["prometheus"]["command"]
        assert "--web.enable-remote-write-receiver" in command

    def test_retention_is_bounded(self) -> None:
        command = _base_compose()["services"]["prometheus"]["command"]
        assert "--storage.tsdb.retention.time=15d" in command

    def test_data_volume_is_mounted(self) -> None:
        volumes = _base_compose()["services"]["prometheus"]["volumes"]
        assert "prometheus_data:/prometheus" in volumes

    def test_prometheus_scrapes_nothing_itself(self) -> None:
        """Collection belongs to the collector; Prometheus is a write receiver."""
        config = yaml.safe_load(PROMETHEUS_CONFIG.read_text())
        assert config["scrape_configs"] == []
        assert config["global"]["scrape_interval"]


class TestProdOverlayHardening:
    def test_grafana_admin_password_comes_from_a_secret(self) -> None:
        grafana = _prod_compose()["services"]["grafana"]
        assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_admin_password"
        assert "grafana_admin_password" in grafana["secrets"]

    def test_grafana_base_password_is_unset_so_the_file_wins(self) -> None:
        grafana = _prod_compose()["services"]["grafana"]
        assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD"] is None

    def test_anonymous_access_is_disabled_in_prod(self) -> None:
        grafana = _prod_compose()["services"]["grafana"]
        assert grafana["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"

    def test_anonymous_viewer_access_is_enabled_in_dev(self) -> None:
        grafana = _base_compose()["services"]["grafana"]
        assert grafana["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
        assert grafana["environment"]["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"

    def test_grafana_secret_is_declared_and_bootstrapped(self) -> None:
        secrets = _prod_compose()["secrets"]
        assert secrets["grafana_admin_password"]["file"] == "./secrets/grafana_admin_password.txt"
        assert (REPO_ROOT / "secrets.example" / "grafana_admin_password.txt").is_file()
        assert "grafana_admin_password.txt" in (REPO_ROOT / "scripts" / "create-secrets.sh").read_text()

    def test_prometheus_is_not_published_publicly_in_prod(self) -> None:
        prometheus = _prod_compose()["services"]["prometheus"]
        assert prometheus["ports"] == ["127.0.0.1:9090:9090"]


class TestObservabilityDocumentation:
    def test_documentation_exists_and_covers_the_backend(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for token in ("otel-collector", "prometheus", "grafana", "4318", "9090", "3000"):
            assert token in doc, f"docs/observability.md does not mention {token}"
