"""Regression tests for service and infrastructure telemetry wiring (gm-deployment-gxr.2).

A service only exports if compose hands it the standard OTEL env triple, and
infrastructure metrics only appear if the collector is told to scrape them.
Both halves are silent when they break — a missing env var produces no error,
just an absent dashboard — so they are pinned here:

- every internal-image service gets the endpoint, its own service name, the
  shared resource attributes, and the trace sampler pair, with the environment
  tag flipped to ``prod`` and the sampling rate cut to 0.1 by the production
  overlay;
- every internal-image service depends on the collector with
  ``service_started`` and never ``service_healthy``, so telemetry can never
  block an application from booting;
- RabbitMQ enables ``rabbitmq_prometheus``, and the two exporters exist,
  digest pinned, unpublished, and credentialed the same way the app services
  are;
- the collector scrapes every infrastructure target under a job name that
  matches the compose service key, with cadvisor and node-exporter on a 30s
  interval because those two dominate the sample count;
- cadvisor and node-exporter observe the host itself, with every mount and the
  container root filesystem read-only; cadvisor takes two capabilities and a
  read-only /dev/kmsg instead of the upstream recipe's blanket privilege, and
  node-exporter needs no capability at all.
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

# Head sampling is parent-based so a decision taken at the edge of a request is
# honoured by every downstream service and a trace is never half-recorded.
TRACES_SAMPLER = "parentbased_traceidratio"
DEV_SAMPLER_ARG = "1.0"
PROD_SAMPLER_ARG = "0.1"

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

# The two exporters that observe the host rather than a stack service. They are
# not in EXPORTERS because neither takes a credential and neither is scraped on
# the 15s interval the service exporters use.
HOST_EXPORTERS = ("cadvisor", "node-exporter")

# job_name -> scrape target. Job names match the compose service keys because
# that is what the dashboards filter on.
EXPECTED_SCRAPE_JOBS = {
    "rabbitmq": "rabbitmq:15692",
    "postgres-exporter": "postgres-exporter:9187",
    "redis-exporter": "redis-exporter:9121",
    "otel-collector": "otel-collector:8888",
    "cadvisor": "cadvisor:8080",
    "node-exporter": "node-exporter:9100",
}

# The two host exporters emit a series per container per device and per CPU per
# mode respectively, so they are scraped half as often as the rest.
HOST_SCRAPE_JOBS = ("cadvisor", "node-exporter")
HOST_SCRAPE_INTERVAL = "30s"


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

    def test_every_service_gets_the_trace_sampler_pair(self) -> None:
        """Traces share the metrics endpoint; only the sampler is extra."""
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            environment = services[name]["environment"]
            assert environment["OTEL_TRACES_SAMPLER"] == TRACES_SAMPLER, name
            assert str(environment["OTEL_TRACES_SAMPLER_ARG"]) == DEV_SAMPLER_ARG, name

    def test_dev_keeps_every_span(self) -> None:
        """Dev volumes are small and a dropped span is a debugging dead end."""
        assert float(DEV_SAMPLER_ARG) == 1.0

    def test_no_groovemap_specific_telemetry_variables(self) -> None:
        """Only the SDK's own variables are allowed to configure telemetry."""
        allowed = {
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_SERVICE_NAME",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_METRICS_EXPORTER",
            "OTEL_METRIC_EXPORT_INTERVAL",
            "OTEL_TRACES_EXPORTER",
            "OTEL_TRACES_SAMPLER",
            "OTEL_TRACES_SAMPLER_ARG",
        }
        services = _base_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            telemetry_keys = {key for key in services[name]["environment"] if key.startswith("OTEL_")}
            assert telemetry_keys <= allowed, (name, sorted(telemetry_keys - allowed))

    def test_third_party_services_are_not_wired_for_otlp(self) -> None:
        """rabbitmq/postgres/neo4j/redis, the exporters, and the telemetry
        backends themselves carry no OTEL SDK."""
        services = _base_compose()["services"]
        for name in ("rabbitmq", "postgres", "neo4j", "redis", "victoria-metrics", "victoria-traces", *EXPORTERS):
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

    def test_prod_cuts_the_sampling_rate(self) -> None:
        services = _prod_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert str(services[name]["environment"]["OTEL_TRACES_SAMPLER_ARG"]) == PROD_SAMPLER_ARG, name

    def test_prod_leaves_the_sampler_itself_alone(self) -> None:
        """Only the head rate changes between environments; changing the
        sampler would change whether a trace stays whole across services."""
        services = _prod_compose()["services"]
        for name in INSTRUMENTED_SERVICES:
            assert "OTEL_TRACES_SAMPLER" not in services[name]["environment"], name


class TestRabbitMqPrometheusPlugin:
    def test_enabled_plugins_file_lists_the_prometheus_plugin(self) -> None:
        plugins = ENABLED_PLUGINS.read_text()
        assert "rabbitmq_prometheus" in plugins
        assert "rabbitmq_management" in plugins, "the management plugin must survive; the console reads its API"

    def test_enabled_plugins_file_is_mounted_read_only(self) -> None:
        volumes = _base_compose()["services"]["rabbitmq"]["volumes"]
        assert "./config/rabbitmq-enabled-plugins:/etc/rabbitmq/enabled_plugins:ro" in volumes

    def test_per_object_metrics_are_enabled(self) -> None:
        """Without this the plugin emits node aggregates only, and the per-queue
        depth and consumer-count panels have nothing to plot."""
        erl_args = _base_compose()["services"]["rabbitmq"]["environment"]["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
        assert "-rabbitmq_prometheus return_per_object_metrics true" in erl_args

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

    def test_the_host_exporters_are_scraped_every_30s(self) -> None:
        """Container and host saturation move over minutes. These two jobs
        dominate the sample count, so they run at half the rate of the rest."""
        intervals = {job["job_name"]: job["scrape_interval"] for job in _scrape_configs()}
        for job_name in HOST_SCRAPE_JOBS:
            assert intervals[job_name] == HOST_SCRAPE_INTERVAL, job_name

    def test_no_application_service_is_scraped(self) -> None:
        """Applications push; scraping one would double-count and duplicate labels."""
        scraped = {job["job_name"] for job in _scrape_configs()}
        assert scraped.isdisjoint(INSTRUMENTED_SERVICES)


class TestHostExporters:
    """cAdvisor and node-exporter answer the two questions the application
    metrics cannot: which container is eating the box, and is the box full."""

    def test_both_host_exporters_exist(self) -> None:
        services = _base_compose()["services"]
        for name in HOST_EXPORTERS:
            assert name in services, f"docker-compose.yml is missing {name}"

    def test_host_exporters_are_digest_pinned(self) -> None:
        services = _base_compose()["services"]
        expected = {"cadvisor": "gcr.io/cadvisor/cadvisor", "node-exporter": "prom/node-exporter"}
        for name, repository in expected.items():
            image = services[name]["image"]
            assert image.startswith(f"{repository}:"), image
            digest = image.partition("@sha256:")[2]
            assert len(digest) == 64, f"{name} is not digest pinned: {image}"

    def test_host_exporters_publish_no_host_ports(self) -> None:
        """cAdvisor serves an unauthenticated UI listing every container on the
        host; node-exporter exposes the host's own saturation."""
        services = _base_compose()["services"]
        for name in HOST_EXPORTERS:
            assert "ports" not in services[name], name

    def test_host_exporters_restart_in_prod(self) -> None:
        services = _prod_compose()["services"]
        for name in HOST_EXPORTERS:
            assert services[name]["restart"] == "always", name

    def test_cadvisor_mounts_are_all_read_only(self) -> None:
        """cAdvisor observes; it never writes. No mount it holds is writable."""
        volumes = _base_compose()["services"]["cadvisor"]["volumes"]
        assert set(volumes) == {
            "/:/rootfs:ro",
            "/var/run:/var/run:ro",
            "/sys:/sys:ro",
            "/var/lib/docker/:/var/lib/docker:ro",
        }
        for volume in volumes:
            assert volume.endswith(":ro"), volume

    def test_cadvisor_is_not_privileged(self) -> None:
        """The upstream recipe runs privileged. Measured against a privileged
        run on the same engine, the two capabilities below produce an identical
        series set, so the privilege is not required and is not taken."""
        cadvisor = _base_compose()["services"]["cadvisor"]
        assert "privileged" not in cadvisor
        assert "no-new-privileges:true" in cadvisor["security_opt"]
        assert cadvisor["cap_drop"] == ["ALL"]
        assert cadvisor["read_only"] is True

    def test_cadvisor_takes_exactly_the_two_capabilities_it_needs(self) -> None:
        """DAC_READ_SEARCH walks the per-container directories under
        /var/lib/docker whose modes exclude it. SYSLOG opens /dev/kmsg."""
        assert _base_compose()["services"]["cadvisor"]["cap_add"] == ["DAC_READ_SEARCH", "SYSLOG"]

    def test_cadvisor_can_read_kernel_oom_messages(self) -> None:
        """/dev/kmsg is the only source of OOM kill messages. Without it
        cAdvisor disables OOM detection with a warning and
        container_oom_events_total never leaves zero — which, for a stack that
        scrapes cAdvisor precisely to catch OOM kills, is the worst failure
        mode available. The bind is read-only."""
        assert _base_compose()["services"]["cadvisor"]["devices"] == ["/dev/kmsg:/dev/kmsg:r"]

    def test_cadvisor_converts_only_the_two_compose_labels(self) -> None:
        """Storing every container label turns every image's labels into
        Prometheus labels. The dashboard filters on the compose service key."""
        command = _base_compose()["services"]["cadvisor"]["command"]
        assert "--store_container_labels=false" in command
        assert "--whitelisted_container_labels=com.docker.compose.project,com.docker.compose.service" in command

    def test_cadvisor_keeps_the_metric_families_the_dashboard_plots(self) -> None:
        """--disable_metrics replaces the upstream default wholesale, so the
        families behind the panels must survive it."""
        disabled = next(flag for flag in _base_compose()["services"]["cadvisor"]["command"] if flag.startswith("--disable_metrics="))
        disabled_sets = set(disabled.partition("=")[2].split(","))
        for required in ("cpu", "memory", "network", "diskIO", "oom_event"):
            assert required not in disabled_sets, required

    def test_node_exporter_reads_the_host_read_only(self) -> None:
        service = _base_compose()["services"]["node-exporter"]
        assert set(service["volumes"]) == {"/proc:/host/proc:ro", "/sys:/host/sys:ro", "/:/rootfs:ro"}
        for flag in ("--path.procfs=/host/proc", "--path.sysfs=/host/sys", "--path.rootfs=/rootfs"):
            assert flag in service["command"], flag

    def test_node_exporter_excludes_the_container_overlay_mounts(self) -> None:
        """Without this every container's overlay is a separate filesystem and
        the host filesystem panel is unreadable."""
        command = _base_compose()["services"]["node-exporter"]["command"]
        exclusion = next(flag for flag in command if flag.startswith("--collector.filesystem.mount-points-exclude="))
        assert "var/lib/docker" in exclusion

    def test_node_exporter_is_hardened(self) -> None:
        """It needs no capability at all: three read-only bind mounts are
        enough, so unlike cAdvisor it keeps an empty capability set."""
        service = _base_compose()["services"]["node-exporter"]
        assert "privileged" not in service
        assert "cap_add" not in service
        assert "devices" not in service
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["cap_drop"] == ["ALL"]
        assert service["read_only"] is True

    def test_both_host_exporters_have_healthchecks(self) -> None:
        """Both images ship a shell and busybox wget, so unlike redis-exporter
        they can probe themselves."""
        services = _base_compose()["services"]
        assert services["cadvisor"]["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:8080/healthz"]
        assert services["node-exporter"]["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:9100/"]

    def test_neither_host_exporter_is_wired_for_otlp(self) -> None:
        services = _base_compose()["services"]
        for name in HOST_EXPORTERS:
            environment = services[name].get("environment", {}) or {}
            assert not [key for key in environment if key.startswith("OTEL_")], name

    def test_the_docker_desktop_caveat_is_documented(self) -> None:
        """On macOS and Windows node-exporter reports the engine's Linux VM,
        not the laptop. An operator who does not know that reads the wrong box."""
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        assert "Docker Desktop" in doc
        assert "Linux VM" in doc


class TestCollectorTracesPipeline:
    """Spans ride the same receiver as metrics and take their own pipeline."""

    def test_the_traces_pipeline_exists_and_reuses_the_otlp_receiver(self) -> None:
        pipeline = _collector_config()["service"]["pipelines"]["traces"]
        assert pipeline["receivers"] == ["otlp"]

    def test_spans_reach_victoria_traces(self) -> None:
        pipeline = _collector_config()["service"]["pipelines"]["traces"]
        assert "otlphttp/victoria_traces" in pipeline["exporters"]
        exporter = _collector_config()["exporters"]["otlphttp/victoria_traces"]
        assert exporter["traces_endpoint"].startswith("http://victoria-traces:10428/")

    def test_span_metrics_rejoin_the_metrics_pipeline(self) -> None:
        pipelines = _collector_config()["service"]["pipelines"]
        assert "spanmetrics" in pipelines["traces"]["exporters"]
        assert "spanmetrics" in pipelines["metrics"]["receivers"]

    def test_no_application_service_is_scraped_for_spans(self) -> None:
        """Spans are pushed, exactly like metrics; nothing pulls them."""
        assert "traces" not in _collector_config()["receivers"]["prometheus"]["config"]


class TestWiringDocumentation:
    def test_env_var_table_and_exporter_inventory_are_documented(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for token in (
            "OTEL_SERVICE_NAME",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_TRACES_SAMPLER",
            "OTEL_TRACES_SAMPLER_ARG",
            "postgres-exporter",
            "redis-exporter",
            "cadvisor",
            "node-exporter",
            "15692",
            "8080",
            "9100",
        ):
            assert token in doc, f"docs/observability.md does not document {token}"

    def test_every_instrumented_service_appears_in_the_env_table(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for name in INSTRUMENTED_SERVICES:
            assert f"`{name}`" in doc, f"docs/observability.md does not list {name}"
