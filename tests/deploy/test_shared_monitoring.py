"""Shared monitoring (gm-deployment-0cht): Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor shared with other stacks.

The invariants: with GROOVEMAP_MONITORING unset or local nothing changes; with it shared the local
copies are omitted (never removed), nothing depends on them, the collector writes to the shared
stores from its ONE configuration file (env substitution, validated by its healthcheck in the same
environment), the console probes the shared services instead of reporting the omitted ones
unreachable, and every dashboard and alert query is scoped to the stack. The same holds for the
PostgreSQL exporter with an external PostgreSQL. Real-container end to end is the Linux smoke's
(gm-deployment-yb4e); the shared endpoints here are plain URLs, so a stand-in serves.

Renders use `docker compose config` (skipped without docker). The collector tests that run the
pinned image run only when it is already present locally; nothing here pulls an image.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tests.deploy import collector_config, test_deploy, test_discogs_import_status
from tests.deploy.test_deploy import PROD, Deploy, helpers
from tests.deploy.test_external_stores import EXTERNAL_ENV, POSTGRES_OVERLAY, VALKEY_OVERLAY, PromotedPolicy, _render, _reviewed_env, images


if TYPE_CHECKING:
    from collections.abc import Iterator


ROOT = Path(__file__).resolve().parents[2]
stores = helpers.stores
OVERLAY = "docker-compose.shared-monitoring.yml"
SHARED_ENV = {
    "GROOVEMAP_MONITORING": "shared",
    "SHARED_VICTORIA_METRICS_URL": "http://vm.lan:8428",
    "SHARED_VICTORIA_TRACES_URL": "https://traces.lan/vt",
    "SHARED_GRAFANA_URL": "http://grafana:3000",
    "SHARED_NODE_EXPORTER_ADDRESS": "node-exporter:9100",
    "SHARED_CADVISOR_ADDRESS": "cadvisor.lan:8080",
}
MONITORING_SERVICES = {"grafana", "victoria-metrics", "victoria-traces", "node-exporter", "cadvisor"}
# The local values the collector's env references must default to: exactly the endpoints and targets
# config/otel-collector.yaml held before it read any variable.
LOCAL_COLLECTOR = {
    "COLLECTOR_METRICS_WRITE_URL": "http://victoria-metrics:8428/api/v1/write",
    "COLLECTOR_TRACES_WRITE_URL": "http://victoria-traces:10428/insert/opentelemetry/v1/traces",
    "COLLECTOR_POSTGRES_EXPORTER_TARGETS": "[postgres-exporter:9187]",
    "COLLECTOR_VALKEY_EXPORTER_TARGETS": "[valkey-exporter:9121]",
    "COLLECTOR_CADVISOR_TARGETS": "[cadvisor:8080]",
    "COLLECTOR_NODE_EXPORTER_TARGETS": "[node-exporter:9100]",
}
CONSOLE_TARGETS = ("VICTORIA_METRICS", "VICTORIA_TRACES", "GRAFANA", "NODE_EXPORTER", "CADVISOR", "POSTGRES_EXPORTER", "VALKEY_EXPORTER")
COLLECTOR_IMAGE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["otel-collector"]["image"]


def _shared(*files: str, env: dict[str, str] | None = None, profile: str | None = None) -> dict[str, Any]:
    """The production stack with `files` and then the shared-monitoring overlay; by default only active services."""
    rendered: dict[str, Any] = json.loads(_render(*files, OVERLAY, env={**EXTERNAL_ENV, **SHARED_ENV, **(env or {})}, profile=profile))
    return rendered


def _scrape_targets(config: dict[str, Any]) -> dict[str, list[str] | None]:
    return {job["job_name"]: job["static_configs"][0]["targets"] for job in config["receivers"]["prometheus"]["config"]["scrape_configs"]}


# --- overlay selection (scripts/stores.py) -----------------------------------------------


def test_the_monitoring_mode_selects_the_overlay_after_the_store_overlays() -> None:
    assert stores.read_stores({}, {}).overlays == []
    assert stores.read_stores({"GROOVEMAP_MONITORING": "local"}, {}).overlays == []
    assert stores.read_stores({"GROOVEMAP_MONITORING": "shared"}, {}).overlays == [OVERLAY]
    lux = stores.read_stores({"GROOVEMAP_POSTGRES": "external", "GROOVEMAP_MONITORING": "shared"}, {})
    assert lux.overlays == [POSTGRES_OVERLAY, OVERLAY]
    assert lux.external == ("postgres",)  # monitoring is not a store: no store check, no store secrets
    both = stores.read_stores({"GROOVEMAP_POSTGRES": "external", "GROOVEMAP_VALKEY": "external"}, {"GROOVEMAP_MONITORING": "shared"})
    assert both.overlays == [POSTGRES_OVERLAY, VALKEY_OVERLAY, OVERLAY]  # the shell wins, as in Compose


def test_an_unknown_monitoring_mode_is_refused_not_guessed() -> None:
    with pytest.raises(stores.StoreConfigError, match="GROOVEMAP_MONITORING='external' is not a monitoring mode"):
        stores.read_stores({"GROOVEMAP_MONITORING": "external"}, {})


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"SHARED_VICTORIA_METRICS_URL": ""}, "SHARED_VICTORIA_METRICS_URL is not set"),
        ({"SHARED_VICTORIA_METRICS_URL": "vm.lan:8428"}, "SHARED_VICTORIA_METRICS_URL='vm.lan:8428' is not a base URL"),
        ({"SHARED_VICTORIA_METRICS_URL": "http://vm.lan:8428/"}, "no trailing /"),
        ({"SHARED_VICTORIA_TRACES_URL": "ftp://vt.lan"}, "SHARED_VICTORIA_TRACES_URL='ftp://vt.lan' is not a base URL"),
        ({"SHARED_GRAFANA_URL": "grafana:3000"}, "SHARED_GRAFANA_URL='grafana:3000' is not a base URL"),
        ({"SHARED_NODE_EXPORTER_ADDRESS": "http://node-exporter:9100"}, "is not the host:port of the shared node-exporter"),
        ({"SHARED_CADVISOR_ADDRESS": "cadvisor:99999"}, "is not the host:port of the shared cAdvisor"),
        ({"SHARED_CADVISOR_ADDRESS": "[a, b]"}, "is not the host:port of the shared cAdvisor"),
    ],
)
def test_shared_settings_are_checked_for_shape(values: dict[str, str], problem: str) -> None:
    settings = {**SHARED_ENV, **values}
    problems = stores.monitoring_problems(stores.read_stores(settings, {}), settings, {})
    assert any(problem in item for item in problems), problems


def test_good_shared_settings_and_the_optional_ones_unset_pass() -> None:
    assert stores.monitoring_problems(stores.read_stores(SHARED_ENV, {}), SHARED_ENV, {}) == []
    required = {key: SHARED_ENV[key] for key in ("GROOVEMAP_MONITORING", "SHARED_VICTORIA_METRICS_URL", "SHARED_VICTORIA_TRACES_URL")}
    assert stores.monitoring_problems(stores.read_stores(required, {}), required, {}) == []
    assert stores.monitoring_problems(stores.read_stores({}, {}), {}, {}) == []  # local monitoring reads none of them


def test_the_postgres_exporter_address_belongs_to_an_external_postgres() -> None:
    external = {"GROOVEMAP_POSTGRES": "external", "EXTERNAL_POSTGRES_EXPORTER_ADDRESS": "pg-exporter:9187"}
    assert stores.monitoring_problems(stores.read_stores(external, {}), external, {}) == []
    bad = {**external, "EXTERNAL_POSTGRES_EXPORTER_ADDRESS": "pg-exporter"}
    assert "is not the host:port of the PostgreSQL exporter" in stores.monitoring_problems(stores.read_stores(bad, {}), bad, {})[0]
    local = {"EXTERNAL_POSTGRES_EXPORTER_ADDRESS": "pg-exporter:9187"}
    assert "PostgreSQL is the stack's own" in stores.monitoring_problems(stores.read_stores(local, {}), local, {})[0]


# --- renders -----------------------------------------------------------------------------


def test_local_monitoring_is_byte_identical_whether_unset_or_explicit() -> None:
    """Local adds no overlay; the base files never mention the mode or the SHARED_* settings."""
    for profile, fmt in ((None, "yaml"), ("*", "json")):
        assert _render(profile=profile, fmt=fmt) == _render(profile=profile, fmt=fmt, env={**SHARED_ENV, "GROOVEMAP_MONITORING": "local"})
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        text = (ROOT / name).read_text()
        assert "local-monitoring" not in text
        assert "SHARED_" not in text
        assert "COLLECTOR_" not in text, "the base files set no collector variable, so its defaults (the local services) apply"


def test_shared_monitoring_omits_the_local_monitoring_services() -> None:
    active = _shared()["services"]
    assert not MONITORING_SERVICES & set(active)
    assert {"otel-collector", "dashboard", "postgres-exporter", "valkey-exporter"} <= set(active)
    everything = _shared(profile="*")["services"]
    for name in MONITORING_SERVICES:
        assert everything[name]["profiles"] == [stores.LOCAL_MONITORING_PROFILE], name
    assert set(stores.SHARED_MONITORING_SERVICES) == MONITORING_SERVICES


@pytest.mark.parametrize("files", [(), (POSTGRES_OVERLAY,), (POSTGRES_OVERLAY, VALKEY_OVERLAY)], ids=["shared", "lux", "all-external"])
def test_no_active_service_depends_on_an_omitted_one(files: tuple[str, ...]) -> None:
    services = _shared(*files)["services"]
    for name, service in services.items():
        missing = set(service.get("depends_on") or {}) - set(services)
        assert not missing, f"{name} depends on {missing}, which this render omits"
    assert services["otel-collector"].get("depends_on") in (None, {})
    assert set(services["dashboard"]["depends_on"]) & MONITORING_SERVICES == set()


def test_external_postgres_with_shared_monitoring_omits_the_postgres_exporter_and_keeps_valkeys() -> None:
    """The lux shape: shared PostgreSQL and monitoring, dedicated Valkey."""
    services = _shared(POSTGRES_OVERLAY)["services"]
    assert "postgres-exporter" not in services
    assert "postgres" not in services
    assert "valkey-exporter" in services
    assert services["otel-collector"]["environment"]["COLLECTOR_POSTGRES_EXPORTER_TARGETS"] == "[]"
    assert "COLLECTOR_VALKEY_EXPORTER_TARGETS" not in services["otel-collector"]["environment"]


# --- the collector: one config file, env substitution -----------------------------------


def test_the_collector_reads_only_the_known_variables_and_defaults_to_the_local_services() -> None:
    assert collector_config.references() == LOCAL_COLLECTOR
    local = collector_config.effective()
    exporters = local["exporters"]
    assert exporters["prometheusremotewrite"]["http"]["endpoint"] == LOCAL_COLLECTOR["COLLECTOR_METRICS_WRITE_URL"]
    assert exporters["otlphttp/victoria_traces"]["traces_endpoint"] == LOCAL_COLLECTOR["COLLECTOR_TRACES_WRITE_URL"]
    targets = _scrape_targets(local)
    assert targets["postgres-exporter"] == ["postgres-exporter:9187"]
    assert targets["valkey-exporter"] == ["valkey-exporter:9121"]
    assert targets["cadvisor"] == ["cadvisor:8080"]
    assert targets["node-exporter"] == ["node-exporter:9100"]


def test_the_local_effective_config_differs_from_the_raw_file_only_in_the_references() -> None:
    """With no variable set, expanding the references is the only change: nothing else is templated."""
    raw = (ROOT / "config" / "otel-collector.yaml").read_text()
    expanded = collector_config.expand(raw, {})
    for name, default in LOCAL_COLLECTOR.items():
        raw = raw.replace(f"${{env:{name}:-{default}}}", default)
    assert raw == expanded


def test_the_stack_scope_is_stamped_on_every_series_without_renaming_a_job() -> None:
    exporter = collector_config.effective()["exporters"]["prometheusremotewrite"]
    assert exporter["external_labels"] == {"service_namespace": "groovemap"}
    assert exporter["resource_to_telemetry_conversion"]["enabled"] is True
    assert "processors" not in collector_config.effective() or "resource" not in str(collector_config.effective()["processors"])


@pytest.mark.parametrize("files", [(), (POSTGRES_OVERLAY,), (POSTGRES_OVERLAY, VALKEY_OVERLAY)], ids=["shared", "lux", "all-external"])
def test_the_overlays_set_only_collector_variables_and_never_touch_its_command_or_probe(files: tuple[str, ...]) -> None:
    local = json.loads(_render())["services"]["otel-collector"]
    collector = _shared(*files)["services"]["otel-collector"]
    for key in ("command", "healthcheck", "volumes", "image", "read_only"):
        assert collector.get(key) == local.get(key), key
    assert set(collector["environment"]) <= set(LOCAL_COLLECTOR)
    assert not set(local.get("environment") or {}) & set(LOCAL_COLLECTOR)


def test_the_shared_render_gives_the_collector_the_shared_endpoints_and_targets() -> None:
    environment = _shared()["services"]["otel-collector"]["environment"]
    effective = collector_config.effective(environment)
    assert effective["exporters"]["prometheusremotewrite"]["http"]["endpoint"] == "http://vm.lan:8428/api/v1/write"
    assert effective["exporters"]["otlphttp/victoria_traces"]["traces_endpoint"] == "https://traces.lan/vt/insert/opentelemetry/v1/traces"
    targets = _scrape_targets(effective)
    assert targets["node-exporter"] == ["node-exporter:9100"]
    assert targets["cadvisor"] == ["cadvisor.lan:8080"]
    assert targets["rabbitmq"] == ["rabbitmq:15692"]  # still the stack's own


def test_an_unset_exporter_address_is_a_job_with_no_target() -> None:
    unset = {key: value for key, value in SHARED_ENV.items() if not key.endswith("_ADDRESS")}
    rendered = json.loads(_render(OVERLAY, env={**EXTERNAL_ENV, **unset}, profile=None))
    environment = rendered["services"]["otel-collector"]["environment"]
    assert environment["COLLECTOR_NODE_EXPORTER_TARGETS"] == "[]"
    targets = _scrape_targets(collector_config.effective(environment))
    assert targets["node-exporter"] == []
    assert targets["cadvisor"] == []


def _pinned_collector() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary")
    present = subprocess.run([docker, "image", "inspect", COLLECTOR_IMAGE], capture_output=True, check=False, timeout=30)
    if present.returncode != 0:
        pytest.skip(f"the pinned collector image is not present locally ({COLLECTOR_IMAGE}); this test never pulls it")
    return docker


def _validate(docker: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The healthcheck's own command, in a container with `environment`, on the repository's config file."""
    target = "/etc/otelcol-contrib/config.yaml"
    argv = [docker, "run", "--rm", "-v", f"{ROOT / 'config' / 'otel-collector.yaml'}:{target}:ro"]
    for key, value in environment.items():
        argv += ["-e", f"{key}={value}"]
    return subprocess.run([*argv, COLLECTOR_IMAGE, "validate", f"--config=file:{target}"], capture_output=True, text=True, check=False, timeout=120)


def test_the_pinned_collector_validates_the_local_and_shared_configs() -> None:
    docker = _pinned_collector()
    assert _validate(docker, {}).returncode == 0
    shared = _shared(POSTGRES_OVERLAY)["services"]["otel-collector"]["environment"]
    result = _validate(docker, shared)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("environment", "error"),
    [
        ({"COLLECTOR_METRICS_WRITE_URL": "not a url/api/v1/write"}, "invalid endpoint"),
        ({"COLLECTOR_TRACES_WRITE_URL": "::/insert/opentelemetry/v1/traces"}, "traces_endpoint must be a valid URL"),
        ({"COLLECTOR_NODE_EXPORTER_TARGETS": "[http://node-exporter:9100]"}, "is not a valid hostname"),
    ],
)
def test_the_pinned_collector_healthcheck_fails_an_invalid_shared_endpoint(environment: dict[str, str], error: str) -> None:
    """The healthcheck validates the same file in the same environment, so a bad shared value turns it unhealthy."""
    result = _validate(_pinned_collector(), environment)
    assert result.returncode != 0
    assert error in result.stderr


# --- the console: configurable targets ---------------------------------------------------


def test_the_local_stack_sets_no_console_target_mode() -> None:
    """Local is the console's default for every target, so the dedicated render names none of them."""
    for files in ((), (POSTGRES_OVERLAY, VALKEY_OVERLAY)):
        environment = json.loads(_render(*files, env=EXTERNAL_ENV))["services"]["dashboard"]["environment"]
        modes = {key: value for key, value in environment.items() if key.endswith("_MODE") and key.removesuffix("_MODE") in CONSOLE_TARGETS}
        expected = {"POSTGRES_EXPORTER_MODE": "disabled", "VALKEY_EXPORTER_MODE": "disabled"} if files else {}
        assert modes == expected


def test_shared_monitoring_points_the_console_at_the_shared_services() -> None:
    environment = _shared()["services"]["dashboard"]["environment"]
    assert environment["VICTORIA_METRICS_MODE"] == "shared"
    assert environment["VICTORIA_METRICS_URL"] == "http://vm.lan:8428"
    assert environment["VICTORIA_TRACES_MODE"] == "shared"
    assert environment["VICTORIA_TRACES_URL"] == "https://traces.lan/vt"
    assert environment["GRAFANA_MODE"] == "shared"
    assert environment["GRAFANA_API_URL"] == "http://grafana:3000"
    assert environment["NODE_EXPORTER_MODE"] == "shared"
    assert environment["NODE_EXPORTER_URL"] == "http://node-exporter:9100"
    assert environment["CADVISOR_MODE"] == "shared"
    assert environment["CADVISOR_URL"] == "http://cadvisor.lan:8080"
    assert "OTEL_COLLECTOR_MODE" not in environment  # the collector stays the stack's own
    assert "POSTGRES_EXPORTER_MODE" not in environment  # PostgreSQL is local here, and so is its exporter
    assert "VALKEY_EXPORTER_MODE" not in environment


def test_an_unset_shared_address_reads_as_not_configured_and_grafana_falls_back_to_the_browser_url() -> None:
    """shared with no URL is the console's `unconfigured`, which is truthful: never `unreachable`."""
    unset = {key: value for key, value in SHARED_ENV.items() if key.startswith(("GROOVEMAP_", "SHARED_VICTORIA"))}
    rendered = json.loads(_render(OVERLAY, env={**EXTERNAL_ENV, **unset, "GRAFANA_URL": "https://grafana.example.net"}, profile=None))
    environment = rendered["services"]["dashboard"]["environment"]
    assert (environment["NODE_EXPORTER_MODE"], environment["NODE_EXPORTER_URL"]) == ("shared", "")
    assert (environment["CADVISOR_MODE"], environment["CADVISOR_URL"]) == ("shared", "")
    assert environment["GRAFANA_API_URL"] == environment["GRAFANA_URL"] == "https://grafana.example.net"


@pytest.mark.parametrize(
    ("address", "mode", "url", "targets"),
    [("", "disabled", "", "[]"), ("pg-exporter.lan:9187", "shared", "http://pg-exporter.lan:9187", "[pg-exporter.lan:9187]")],
)
def test_an_external_postgres_exporter_is_shared_or_disabled_never_unreachable(address: str, mode: str, url: str, targets: str) -> None:
    for render in (
        json.loads(_render(POSTGRES_OVERLAY, env={**EXTERNAL_ENV, "EXTERNAL_POSTGRES_EXPORTER_ADDRESS": address})),
        _shared(POSTGRES_OVERLAY, env={"EXTERNAL_POSTGRES_EXPORTER_ADDRESS": address}),
    ):
        services = render["services"]
        assert services["dashboard"]["environment"]["POSTGRES_EXPORTER_MODE"] == mode
        assert services["dashboard"]["environment"]["POSTGRES_EXPORTER_URL"] == url
        assert services["otel-collector"]["environment"]["COLLECTOR_POSTGRES_EXPORTER_TARGETS"] == targets


def test_the_console_target_env_files_exist_only_for_the_modes_they_set() -> None:
    directory = ROOT / "config" / "console-targets"
    assert sorted(path.name for path in directory.iterdir()) == ["postgres-exporter.disabled.env", "postgres-exporter.shared.env"]
    assert not (directory / "postgres-exporter.env").exists(), "the unset path must not exist, or `required: false` would load it"


def test_an_external_valkey_disables_the_omitted_exporter_everywhere() -> None:
    services = json.loads(_render(VALKEY_OVERLAY, env=EXTERNAL_ENV))["services"]
    assert services["dashboard"]["environment"]["VALKEY_EXPORTER_MODE"] == "disabled"
    assert services["otel-collector"]["environment"]["COLLECTOR_VALKEY_EXPORTER_TARGETS"] == "[]"


# --- deploy helpers ----------------------------------------------------------------------


def _findings(env: dict[str, str], policy: Any = None) -> list[Any]:
    findings: list[Any] = helpers.monitoring_findings(stores.read_stores(env, {}), env_values=env, environ={}, image_policy=policy or images)
    return findings


class ConsoleTargetsPromoted(PromotedPolicy):
    CONSOLE_TARGETS_FLOOR: dict[str, str] = {"OPERATIONS_CONSOLE_IMAGE": images.RELEASED_IMAGE_VERSIONS["OPERATIONS_CONSOLE_IMAGE"]}  # noqa: RUF012


class ConsoleTargetsUnreleased(PromotedPolicy):
    """The image policy before any console release reads its configurable targets."""

    CONSOLE_TARGETS_FLOOR: dict[str, str] = {"OPERATIONS_CONSOLE_IMAGE": images.UNRELEASED}  # noqa: RUF012


def test_local_monitoring_is_one_ok_line() -> None:
    findings = _findings({})
    assert [(finding.level, finding.message) for finding in findings] == [
        ("ok", "Monitoring: the stack's own Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor")
    ]


def test_shared_monitoring_says_where_telemetry_goes_and_warns_while_the_console_ignores_its_targets() -> None:
    findings = _findings(SHARED_ENV, ConsoleTargetsUnreleased)
    assert findings[0].level == "ok"
    assert "metrics to http://vm.lan:8428, traces to https://traces.lan/vt, Grafana at http://grafana:3000" in findings[0].message
    warning = findings[-1]
    assert warning.level == "warn"
    assert "will report Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor as unreachable" in warning.message
    assert "predates its configurable Platform Health targets" in warning.message
    promoted = _findings({**SHARED_ENV, **_reviewed_env()}, ConsoleTargetsPromoted)
    assert [finding.level for finding in promoted] == ["ok"]
    assert [finding.level for finding in _findings({**SHARED_ENV, **_reviewed_env()})] == ["ok"], "the reviewed table meets the floor"


def test_shared_monitoring_warns_about_what_it_cannot_monitor() -> None:
    settings = {key: value for key, value in SHARED_ENV.items() if not key.startswith(("SHARED_GRAFANA", "SHARED_NODE", "SHARED_CADVISOR"))}
    messages = [finding.message for finding in _findings({**settings, **_reviewed_env()}, ConsoleTargetsPromoted) if finding.level == "warn"]
    assert any("localhost inside its container is the console itself" in message for message in messages)
    assert any(message.startswith("SHARED_NODE_EXPORTER_ADDRESS is not set") for message in messages)
    assert any(message.startswith("SHARED_CADVISOR_ADDRESS is not set") for message in messages)
    with_host = {**settings, **_reviewed_env(), "GRAFANA_URL": "http://host.docker.internal:3000"}
    assert not any("localhost" in finding.message for finding in _findings(with_host, ConsoleTargetsPromoted))


def test_bad_shared_settings_stop_and_say_where_to_fix_them() -> None:
    findings = _findings({"GROOVEMAP_MONITORING": "shared"})
    assert {finding.level for finding in findings} == {"stop"}
    assert "docs/homelab.md, Shared monitoring" in findings[0].hint


def test_external_postgres_reports_its_exporter() -> None:
    disabled = _findings({"GROOVEMAP_POSTGRES": "external", **_reviewed_env()}, ConsoleTargetsPromoted)
    assert disabled[-1].message.startswith("PostgreSQL exporter: none (EXTERNAL_POSTGRES_EXPORTER_ADDRESS is unset)")
    shared = _findings({"GROOVEMAP_POSTGRES": "external", "EXTERNAL_POSTGRES_EXPORTER_ADDRESS": "pgx:9187"}, ConsoleTargetsUnreleased)
    assert shared[1].message == "PostgreSQL exporter: shared, pgx:9187 (scraped by the collector, probed by the console)"
    assert "will report the PostgreSQL exporter as unreachable" in shared[-1].message


def test_store_findings_include_monitoring_and_own_compose_file_for_shared_monitoring_alone(tmp_path: Path) -> None:
    def findings(environ: dict[str, str], values: dict[str, str]) -> list[Any]:
        result: list[Any] = helpers.store_findings(
            "dev", env_values=values, environ=environ, secrets_dir=tmp_path, image_policy=PromotedPolicy, local_volumes=lambda: ()
        )
        return result

    shared = findings({}, SHARED_ENV)
    assert not [finding for finding in shared if finding.level == "stop"], "shared monitoring needs neither prod nor secrets"
    assert any(finding.message.startswith("Monitoring: shared") for finding in shared)
    stop = [finding for finding in findings({"COMPOSE_FILE": "x.yml"}, SHARED_ENV) if finding.level == "stop"]
    assert stop[0].message.startswith("COMPOSE_FILE is set in your shell")


def test_compose_file_is_kept_for_shared_monitoring_and_an_old_marker_is_recognised(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    files = ["docker-compose.yml", "docker-compose.prod.yml", OVERLAY]
    old_marker = stores.COMPOSE_FILE_MARKERS[1]
    env.write_text(f"APP_BASE_URL=https://x\n{old_marker}\nCOMPOSE_FILE={':'.join(files)}\n")
    assert helpers.compose_file_managed(env)
    assert "Set COMPOSE_FILE=" in helpers.sync_compose_file(env, files, overlaid=True, separator=":")  # the wording moves on
    text = env.read_text()
    assert stores.COMPOSE_FILE_MARKER in text and old_marker not in text and text.count("COMPOSE_FILE=") == 1
    assert helpers.sync_compose_file(env, files, overlaid=True, separator=":") == ""
    assert "monitoring is local again" in helpers.sync_compose_file(env, files[:2], overlaid=False)
    assert env.read_text() == "APP_BASE_URL=https://x\n"


def test_the_store_files_helper_adds_the_overlay_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    env.write_text("GROOVEMAP_POSTGRES=external\nGROOVEMAP_MONITORING=shared\n")
    monkeypatch.setattr(helpers, "ENV_FILE", env)
    assert helpers.main(["store-files"], environ={}) == 0
    assert capsys.readouterr().out.splitlines() == [f"{POSTGRES_OVERLAY} store-check-postgres", OVERLAY]


def test_omitted_monitoring_services_publish_no_port_and_hold_no_checked_disk() -> None:
    assert helpers.omitted_service_ports(SHARED_ENV, {}) == ["GRAFANA_HOST_PORT", "VICTORIA_METRICS_HOST_PORT", "VICTORIA_TRACES_HOST_PORT"]
    assert helpers.omitted_service_ports({**SHARED_ENV, "GROOVEMAP_POSTGRES": "external"}, {})[0] == "POSTGRES_HOST_PORT"
    everything = _shared(profile="*")
    assert [volume.name for volume in helpers.omitted_volumes(everything)] == ["victoria_metrics_data"]
    assert helpers._omitted_as(everything, helpers.omitted_volumes(everything)[0]) == "shared"


def test_the_endpoints_point_at_the_shared_grafana_and_never_at_a_local_admin_password() -> None:
    rows = {label: (url, note) for label, url, note in helpers.endpoints("prod", {**SHARED_ENV, "GRAFANA_URL": "https://grafana.lan"}, {}, "lab")}
    assert "Grafana" not in rows
    assert rows["Grafana (shared)"] == ("https://grafana.lan", "your own sign-in on the shared Grafana")
    assert not any("grafana_admin_password" in note for _, note in rows.values())


# --- secrets bootstrap -------------------------------------------------------------------


def _bootstrap(tmp_path: Path, env_text: str, **environ: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("create-secrets.sh", "stores.py"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / ".env").write_text(env_text)
    base = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(tmp_path / "scripts" / "create-secrets.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=base | environ,
    )


def test_secrets_bootstrap_never_writes_a_local_grafana_admin_password_in_shared_mode(tmp_path: Path) -> None:
    result = _bootstrap(tmp_path, "GROOVEMAP_MONITORING=shared\n")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "secrets" / "grafana_admin_password.txt").exists()
    assert "[skip] grafana_admin_password.txt: Grafana is shared" in result.stdout
    assert (tmp_path / "secrets" / "grafana_viewer_token.txt").exists(), "the optional token file is still mounted by the console"


def test_secrets_bootstrap_writes_it_locally_and_leaves_an_existing_one_alone(tmp_path: Path) -> None:
    assert _bootstrap(tmp_path, "").returncode == 0
    password = (tmp_path / "secrets" / "grafana_admin_password.txt").read_text()
    assert password
    assert _bootstrap(tmp_path, "", GROOVEMAP_MONITORING="shared").returncode == 0  # the shell wins, like Compose
    assert (tmp_path / "secrets" / "grafana_admin_password.txt").read_text() == password  # never deleted


def test_secrets_bootstrap_refuses_an_unknown_mode_before_writing_anything(tmp_path: Path) -> None:
    result = _bootstrap(tmp_path, "GROOVEMAP_MONITORING=remote\n")
    assert result.returncode != 0
    assert "not a monitoring mode" in result.stderr
    assert not (tmp_path / "secrets").exists()


# --- deploy.sh ---------------------------------------------------------------------------


class SharedDeploy(Deploy):
    """A scratch repository whose .env shares monitoring (and, for lux, PostgreSQL)."""

    def __init__(self, root: Path, extra: dict[str, str] | None = None) -> None:
        super().__init__(root)
        shutil.copy(ROOT / OVERLAY, root / OVERLAY)
        created = subprocess.run([sys.executable, str(root / "scripts" / "env-init.py")], capture_output=True, text=True, check=False, cwd=root)
        assert created.returncode == 0, created.stderr
        self.write_env((root / ".env").read_text() + "".join(f"{key}={value}\n" for key, value in {**SHARED_ENV, **(extra or {})}.items()))


@pytest.fixture
def shared(tmp_path: Path) -> Iterator[SharedDeploy]:
    yield SharedDeploy(tmp_path)


def test_deploy_adds_the_overlay_everywhere_and_runs_no_check_for_it(shared: SharedDeploy) -> None:
    result = shared.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 0, result.stdout + result.stderr
    stack = f"docker compose {PROD} -f {OVERLAY}"
    assert test_deploy._compose_calls(shared.calls) == [
        f"{stack} --profile * config --format json",
        f"{stack} up -d --wait",
        "gate --prod --wait --interval 300",
        f"{stack} up -d --wait",
        f"{stack} exec -T api catalog-admin-setup --list",
    ]
    assert shared.env_values["COMPOSE_FILE"].split(":") == ["docker-compose.yml", "docker-compose.prod.yml", OVERLAY]
    assert "Monitoring: shared; metrics to http://vm.lan:8428" in result.stdout
    assert "Grafana (shared)" in result.stdout
    assert "grafana_admin_password" not in result.stdout.replace("[skip] grafana_admin_password.txt", "")
    assert not (shared.root / "secrets" / "grafana_admin_password.txt").exists()


def test_switching_back_to_local_monitoring_removes_deploys_compose_file(shared: SharedDeploy) -> None:
    assert shared.run("--no-wait").returncode == 0
    shared.write_env((shared.root / ".env").read_text().replace("GROOVEMAP_MONITORING=shared", "GROOVEMAP_MONITORING=local"))
    result = shared.run("--no-wait")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPOSE_FILE" not in shared.env_values
    assert all(OVERLAY not in call for call in shared.calls)


def test_deploy_stops_on_a_missing_shared_endpoint_before_anything_changes(tmp_path: Path) -> None:
    deploy = SharedDeploy(tmp_path)
    deploy.write_env((tmp_path / ".env").read_text().replace("SHARED_VICTORIA_TRACES_URL=https://traces.lan/vt\n", ""))
    result = deploy.run(FAKE_GATE_WAIT="0")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "SHARED_VICTORIA_TRACES_URL is not set" in result.stdout
    assert test_deploy._compose_calls(deploy.calls) == []
    assert not (tmp_path / "secrets").exists()


# --- the override check and the homelab example ------------------------------------------


EXAMPLE = ROOT / "skills" / "groovemap-homelab" / "assets" / "override-shared-monitoring-network.yml"


def test_the_example_attaches_exactly_the_monitoring_clients() -> None:
    example = yaml.safe_load(EXAMPLE.read_text())
    assert sorted(example["services"]) == sorted(stores.MONITORING_CLIENTS)
    assert all(service == {"networks": {"monitoring": {}}} for service in example["services"].values())
    assert example["networks"]["monitoring"]["external"] is True


def test_the_override_check_accepts_the_example_on_top_of_the_overlay() -> None:
    before = _shared(POSTGRES_OVERLAY, profile="*")
    after: dict[str, Any] = json.loads(
        _render(POSTGRES_OVERLAY, OVERLAY, str(EXAMPLE.relative_to(ROOT)), env={**EXTERNAL_ENV, **SHARED_ENV}, profile="*")
    )
    findings = helpers.check_override(before, after)
    assert [finding.level for finding in findings] == ["ok"], findings
    assert "monitoring" in after["services"]["otel-collector"]["networks"]


def test_the_override_check_still_refuses_an_override_that_flips_a_monitoring_profile() -> None:
    """The overlay is part of the baseline, so its profiles pass; an override cannot re-enable an omitted service."""
    before = _shared(profile="*")
    after = json.loads(json.dumps(before))
    after["services"]["grafana"]["profiles"] = []
    assert any(finding.level == "stop" and "changes profiles on grafana" in finding.message for finding in helpers.check_override(before, after))


# --- documentation -----------------------------------------------------------------------


def test_the_docs_and_skill_describe_shared_monitoring() -> None:
    homelab = (ROOT / "docs" / "homelab.md").read_text()
    assert "## Shared monitoring" in homelab
    for variable in (
        "GROOVEMAP_MONITORING",
        *stores.SHARED_URLS,
        stores.SHARED_OPTIONAL_URL,
        *stores.SHARED_ADDRESSES,
        stores.POSTGRES_EXPORTER_ADDRESS,
    ):
        assert variable in homelab, variable
        assert variable in (ROOT / "docs" / "configuration.md").read_text(), variable
        assert variable in (ROOT / ".env.example").read_text(), variable
    skill = (ROOT / "skills" / "groovemap-homelab" / "SKILL.md").read_text()
    assert "GROOVEMAP_MONITORING" in skill
    references = (ROOT / "skills" / "groovemap-homelab" / "references" / "shared-services.md").read_text()
    assert "## Shared monitoring" in references
    for client in stores.MONITORING_CLIENTS:
        assert f"`{client}`" in references, client
    assert "override-shared-monitoring-network.yml" in references
    observability = (ROOT / "docs" / "observability.md").read_text()
    assert "## Shared Grafana" in observability
    assert 'service_namespace="groovemap"' in observability


def _section(path: Path, heading: str) -> str:
    """From `heading` up to the next heading of the same or a higher level."""
    text = path.read_text()
    level = heading.split(" ", 1)[0]
    body = text[text.index(f"\n{heading}\n") + len(heading) + 2 :]
    ends = [body.find(f"\n{'#' * depth} ") for depth in range(1, len(level) + 1)]
    return body[: min((end for end in ends if end >= 0), default=len(body))]


def test_shared_monitoring_guidance_never_suggests_a_local_grafana_admin_password() -> None:
    sections = (
        (ROOT / "docs" / "homelab.md", "## Shared monitoring"),
        (ROOT / "skills" / "groovemap-homelab" / "references" / "shared-services.md", "## Shared monitoring"),
        (ROOT / "docs" / "maintenance.md", "### On a shared Grafana"),
    )
    for path, heading in sections:
        section = _section(path, heading)
        assert "Viewer" in section, (path, heading)
        assert "grafana_admin_password" not in section, (path, heading)
        assert "sign in as admin" not in section.lower(), (path, heading)


def test_the_gate_names_the_same_files_as_deploy() -> None:
    """The Discogs gate (`--prod -f docker-compose.override.yml`, as deploy runs it) adds the overlay after the store overlays."""
    gate = test_discogs_import_status.gate
    args = gate.parse_args(["--prod", "-f", str(ROOT / "docker-compose.override.yml")])
    lux = stores.read_stores({"GROOVEMAP_POSTGRES": "external", "GROOVEMAP_MONITORING": "shared"}, {})
    files = [Path(name).name for name in gate.compose_files(args, lux)]
    assert files == ["docker-compose.yml", "docker-compose.prod.yml", POSTGRES_OVERLAY, OVERLAY, "docker-compose.override.yml"]
    configured, problem = gate.store_settings(gate.parse_args(["--prod"]), {"GROOVEMAP_MONITORING": "shared"})
    assert problem is None
    assert configured.overlays == [OVERLAY]
    _, problem = gate.store_settings(gate.parse_args(["--prod"]), {"GROOVEMAP_MONITORING": "bogus"})
    assert problem is not None and "not a monitoring mode" in problem
