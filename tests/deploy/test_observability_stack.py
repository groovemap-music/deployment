"""Regression tests for the OpenTelemetry backend (gm-deployment-gxr.1, dqh.1).

The four backend services are the only thing that makes exported telemetry
visible, so their wiring is pinned here rather than left to a live smoke run:

- all four images are digest pinned and consumed from public registries;
- the metrics pipeline stays ``otlp -> memory_limiter -> batch ->
  prometheusremotewrite`` pointed at VictoriaMetrics' remote-write endpoint,
  with memory_limiter first so back-pressure precedes batching;
- the traces pipeline pushes OTLP to VictoriaTraces and, through the
  spanmetrics connector, feeds RED metrics back into the metrics pipeline;
- both Victoria servers carry a bounded retention and their own volume;
- the production overlay sources the Grafana admin password from a Docker
  secret, turns anonymous access off, and loopback-binds both Victoria ports.

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

OBSERVABILITY_SERVICES = ("otel-collector", "victoria-metrics", "victoria-traces", "grafana")

DIGEST_PREFIX = "@sha256:"


def _duration_seconds(value: str | float) -> float:
    """Parse a collector duration literal (``5ms``, ``2500ms``, ``1s``) into seconds."""
    text = str(value)
    if text.endswith("ms"):
        return float(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def _base_compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    return loaded


def _prod_compose() -> dict[str, Any]:
    return _load_compose(REPO_ROOT / "docker-compose.prod.yml")


def _collector_config() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COLLECTOR_CONFIG.read_text())
    return loaded


class TestBackendServicesExist:
    def test_all_four_services_are_defined(self) -> None:
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
            "victoria-metrics": "victoriametrics/victoria-metrics",
            "victoria-traces": "victoriametrics/victoria-traces",
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
        assert "victoria_metrics_data" in volumes
        assert "victoria_traces_data" in volumes
        assert "grafana_data" in volumes

    def test_no_second_tsdb_survives_anywhere(self) -> None:
        """VictoriaMetrics is the organisation's backend; shipping Prometheus
        alongside it means two stores, two retentions, and two truths."""
        compose = _base_compose()
        assert "prometheus" not in compose["services"]
        assert "prometheus_data" not in compose["volumes"]
        assert not (REPO_ROOT / "config" / "prometheus.yml").exists()
        assert "prom/prometheus" not in (REPO_ROOT / "docker-compose.yml").read_text()

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

    def test_grafana_provisioning_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["grafana"]["volumes"]
        assert "./config/grafana/provisioning:/etc/grafana/provisioning:ro" in volumes
        assert "./config/grafana/dashboards:/var/lib/grafana/dashboards:ro" in volumes

    def test_config_files_exist(self) -> None:
        assert COLLECTOR_CONFIG.is_file()

    def test_the_victoria_servers_need_no_config_file(self) -> None:
        """Both are configured entirely by command-line flags, so neither
        mounts anything a stale file could contradict."""
        services = _base_compose()["services"]
        for name in ("victoria-metrics", "victoria-traces"):
            mounts = [volume for volume in services[name]["volumes"] if volume.startswith("./")]
            assert mounts == [], (name, mounts)


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

    def test_traces_pipeline_shape(self) -> None:
        pipeline = _collector_config()["service"]["pipelines"]["traces"]
        assert pipeline["receivers"] == ["otlp"], "spans arrive on the same OTLP receiver as metrics"
        assert pipeline["processors"] == ["memory_limiter", "batch"], "memory_limiter must run before batch"
        assert set(pipeline["exporters"]) == {"spanmetrics", "otlphttp/victoria_traces"}

    def test_memory_limiter_and_batch_are_configured(self) -> None:
        processors = _collector_config()["processors"]
        assert processors["memory_limiter"]["limit_mib"] > 0
        assert processors["memory_limiter"]["check_interval"]
        assert processors["batch"]["send_batch_size"] > 0

    def test_remote_write_targets_victoria_metrics(self) -> None:
        exporter = _collector_config()["exporters"]["prometheusremotewrite"]
        assert exporter["http"]["endpoint"] == "http://victoria-metrics:8428/api/v1/write"
        assert exporter["http"]["tls"]["insecure"] is True, "plain HTTP on the internal network needs insecure: true"

    def test_otlp_traces_are_pushed_to_victoria_traces(self) -> None:
        exporter = _collector_config()["exporters"]["otlphttp/victoria_traces"]
        assert exporter["traces_endpoint"] == "http://victoria-traces:10428/insert/opentelemetry/v1/traces"

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


class TestSpanMetricsConnector:
    """RED metrics for every instrumented operation, derived from spans."""

    def _connector(self) -> dict[str, Any]:
        connector: dict[str, Any] = _collector_config()["connectors"]["spanmetrics"]
        return connector

    def test_it_bridges_the_traces_pipeline_into_the_metrics_pipeline(self) -> None:
        pipelines = _collector_config()["service"]["pipelines"]
        assert "spanmetrics" in pipelines["traces"]["exporters"], "the connector consumes spans"
        assert "spanmetrics" in pipelines["metrics"]["receivers"], "the connector produces metrics"

    def test_the_namespace_produces_the_catalogued_prometheus_names(self) -> None:
        """traces.span.metrics.calls -> traces_span_metrics_calls_total."""
        assert self._connector()["namespace"] == "traces.span.metrics"

    def test_the_histogram_is_explicit_buckets_in_seconds(self) -> None:
        histogram = self._connector()["histogram"]
        assert histogram["unit"] == "s", "every GrooveMap histogram is in seconds"
        buckets = histogram["explicit"]["buckets"]
        assert buckets, "exponential histograms do not survive remote write intact"
        assert buckets == sorted(buckets, key=_duration_seconds), buckets

    def test_no_dimension_beyond_the_declared_label_set_is_added(self) -> None:
        """service.name, span.name, span.kind and status.code are built in and
        are exactly what the conventions declare. collector.instance.id is
        excluded: one collector runs here, so it is a constant label."""
        connector = self._connector()
        assert "dimensions" not in connector
        assert connector["exclude_dimensions"] == ["collector.instance.id"]

    def test_the_span_metrics_are_in_the_catalog(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        assert "`traces_span_metrics_calls_total`" in doc
        assert "`traces_span_metrics_duration_seconds`" in doc


class TestVictoriaMetricsServer:
    def test_retention_is_bounded(self) -> None:
        command = _base_compose()["services"]["victoria-metrics"]["command"]
        assert "-retentionPeriod=15d" in command

    def test_storage_path_and_volume_agree(self) -> None:
        service = _base_compose()["services"]["victoria-metrics"]
        assert "-storageDataPath=/victoria-metrics-data" in service["command"]
        assert "victoria_metrics_data:/victoria-metrics-data" in service["volumes"]

    def test_it_listens_on_8428(self) -> None:
        service = _base_compose()["services"]["victoria-metrics"]
        assert "-httpListenAddr=:8428" in service["command"]
        assert "8428:8428" in [str(port) for port in service["ports"]]

    def test_the_healthcheck_probes_the_health_endpoint(self) -> None:
        """127.0.0.1, not localhost: the server binds IPv4 only and busybox
        wget tries ::1 first, which is refused."""
        test = _base_compose()["services"]["victoria-metrics"]["healthcheck"]["test"]
        assert "http://127.0.0.1:8428/health" in test

    def test_it_scrapes_nothing_itself(self) -> None:
        """Collection belongs to the collector; this is a write receiver, and
        it carries no scrape configuration to drift from the collector's."""
        service = _base_compose()["services"]["victoria-metrics"]
        assert not any("promscrape" in str(flag) for flag in service["command"]), service["command"]


class TestVictoriaTracesServer:
    def test_retention_is_bounded(self) -> None:
        command = _base_compose()["services"]["victoria-traces"]["command"]
        assert "-retentionPeriod=7d" in command

    def test_storage_path_and_volume_agree(self) -> None:
        service = _base_compose()["services"]["victoria-traces"]
        assert "-storageDataPath=/victoria-traces-data" in service["command"]
        assert "victoria_traces_data:/victoria-traces-data" in service["volumes"]

    def test_it_listens_on_10428(self) -> None:
        service = _base_compose()["services"]["victoria-traces"]
        assert "-httpListenAddr=:10428" in service["command"]
        assert "10428:10428" in [str(port) for port in service["ports"]]

    def test_the_healthcheck_runs_the_servers_own_binary(self) -> None:
        """The image is distroless — no shell, no wget, no curl — so the probe
        cannot call /health the way victoria-metrics does."""
        test = _base_compose()["services"]["victoria-traces"]["healthcheck"]["test"]
        assert test == ["CMD", "/victoria-traces-prod", "-version"]


class TestBackendsGateTheirReaders:
    def test_the_collector_waits_for_both_stores(self) -> None:
        depends_on = _base_compose()["services"]["otel-collector"]["depends_on"]
        for name in ("victoria-metrics", "victoria-traces"):
            assert depends_on[name]["condition"] == "service_healthy", name

    def test_grafana_waits_for_both_stores(self) -> None:
        depends_on = _base_compose()["services"]["grafana"]["depends_on"]
        for name in ("victoria-metrics", "victoria-traces"):
            assert depends_on[name]["condition"] == "service_healthy", name


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

    def test_neither_victoria_server_is_published_publicly_in_prod(self) -> None:
        services = _prod_compose()["services"]
        assert services["victoria-metrics"]["ports"] == ["127.0.0.1:8428:8428"]
        assert services["victoria-traces"]["ports"] == ["127.0.0.1:10428:10428"]

    def test_the_loopback_binds_replace_rather_than_extend_the_base_publish(self) -> None:
        """Without !override compose MERGES port lists, and 0.0.0.0:8428 would
        survive alongside the loopback bind — the opposite of the intent."""
        overlay = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        for port in ("127.0.0.1:8428:8428", "127.0.0.1:10428:10428"):
            index = overlay.index(port)
            assert "ports: !override" in overlay[index - 200 : index], port


class TestObservabilityDocumentation:
    def test_documentation_exists_and_covers_the_backend(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for token in ("otel-collector", "victoria-metrics", "victoria-traces", "grafana", "4318", "8428", "10428", "3000"):
            assert token in doc, f"docs/observability.md does not mention {token}"
