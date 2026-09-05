"""Tests for the provisioned dashboards and their lint gate (gm-deployment-gxr.3,
extended for wave 2 by gm-deployment-dqh.3).

Two layers:

- the real artefacts — the dashboards and the Grafana provisioning that loads
  them — are checked for the properties operators depend on (stable uids, the
  datasource variables, the panels the acceptance calls for);
- ``scripts/check-dashboards.py`` is exercised directly on synthetic
  dashboards, because a gate that cannot fail is not a gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml


if TYPE_CHECKING:
    from types import ModuleType

    import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "config" / "grafana" / "dashboards"
PROVISIONING = REPO_ROOT / "config" / "grafana" / "provisioning"
CHECKER = REPO_ROOT / "scripts" / "check-dashboards.py"

# The nine dashboards docs/observability.md documents, and the wave that
# delivered each. The inventory is closed: exactly these files exist.
EXPECTED_UIDS = {
    "groovemap-pipeline-overview",
    "groovemap-ingestion",
    "groovemap-consumers",
    "groovemap-api-services",
    "groovemap-infrastructure",
    "groovemap-runtime",
    "groovemap-neo4j",
    "groovemap-containers",
    "groovemap-traces",
}

WAVE_ONE_UIDS = {
    "groovemap-pipeline-overview",
    "groovemap-ingestion",
    "groovemap-consumers",
    "groovemap-api-services",
    "groovemap-infrastructure",
}

# `groovemap-containers` is wave 2 as well: gm-deployment-dqh.2 added it
# alongside the cadvisor and node-exporter scrape jobs that feed it.
WAVE_TWO_UIDS = {"groovemap-runtime", "groovemap-neo4j", "groovemap-containers", "groovemap-traces"}

DATASOURCE_VARIABLE = "${DS_PROMETHEUS}"
TRACE_DATASOURCE_VARIABLE = "${DS_TEMPO}"


def _load_checker() -> ModuleType:
    """Import scripts/check-dashboards.py, whose filename is not importable."""
    spec = importlib.util.spec_from_file_location("check_dashboards", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_dashboards"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _dashboards() -> dict[str, dict[str, Any]]:
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(DASHBOARD_DIR.glob("*.json"))}


def _expressions(dashboard: dict[str, Any]) -> list[str]:
    expressions: list[str] = checker.collect_expressions(dashboard)
    return expressions


class TestProvisioning:
    def test_datasource_is_provisioned_with_a_stable_uid(self) -> None:
        config = yaml.safe_load((PROVISIONING / "datasources" / "prometheus.yaml").read_text())
        assert config["apiVersion"] == 1
        datasource = config["datasources"][0]
        # VictoriaMetrics serves the Prometheus query API, so the name, type,
        # and uid outlive the server swap and every dashboard keeps resolving.
        assert datasource["name"] == "Prometheus"
        assert datasource["uid"] == "prometheus"
        assert datasource["type"] == "prometheus"
        assert datasource["url"] == "http://victoria-metrics:8428"
        assert datasource["editable"] is False, "datasources are code; UI edits would be silently reverted"

    def test_the_tempo_datasource_reads_victoria_traces(self) -> None:
        config = yaml.safe_load((PROVISIONING / "datasources" / "prometheus.yaml").read_text())
        tempo = next(entry for entry in config["datasources"] if entry["uid"] == "tempo")
        assert tempo["name"] == "Tempo"
        assert tempo["type"] == "tempo"
        assert tempo["url"] == "http://victoria-traces:10428/select/tempo"
        assert tempo["editable"] is False

    def test_dashboard_provider_loads_the_mounted_directory(self) -> None:
        config = yaml.safe_load((PROVISIONING / "dashboards" / "groovemap.yaml").read_text())
        assert config["apiVersion"] == 1
        provider = config["providers"][0]
        assert provider["type"] == "file"
        assert provider["folder"] == "GrooveMap"
        assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
        assert provider["allowUiUpdates"] is False


class TestDashboardInventory:
    def test_exactly_the_expected_dashboards_exist(self) -> None:
        """gm-deployment-dqh.3 relaxed this to a pair of subset assertions
        because `groovemap-containers` was landing on a sibling branch and an
        equality would have failed on whichever branch merged first. Both
        wave-2 branches are together now, so the inventory is closed again: a
        dashboard nobody documented is one nobody can be told to open, and a
        documented uid with no file behind it is a dead link in the runbook."""
        uids = {dashboard["uid"] for dashboard in _dashboards().values()}
        assert uids == EXPECTED_UIDS

    def test_the_wave_split_accounts_for_every_dashboard(self) -> None:
        assert WAVE_ONE_UIDS | WAVE_TWO_UIDS == EXPECTED_UIDS
        assert WAVE_ONE_UIDS.isdisjoint(WAVE_TWO_UIDS)

    def test_the_wave_one_dashboards_are_still_there(self) -> None:
        """Wave 2 adds dashboards; it never replaces one."""
        uids = {dashboard["uid"] for dashboard in _dashboards().values()}
        assert uids >= WAVE_ONE_UIDS

    def test_the_wave_two_dashboards_exist(self) -> None:
        uids = {dashboard["uid"] for dashboard in _dashboards().values()}
        assert uids >= WAVE_TWO_UIDS

    def test_uids_and_titles_are_unique(self) -> None:
        dashboards = list(_dashboards().values())
        uids = [dashboard["uid"] for dashboard in dashboards]
        titles = [dashboard["title"] for dashboard in dashboards]
        assert len(set(uids)) == len(uids)
        assert len(set(titles)) == len(titles)

    def test_every_dashboard_declares_a_schema_version(self) -> None:
        for name, dashboard in _dashboards().items():
            assert isinstance(dashboard["schemaVersion"], int), name

    def test_every_dashboard_is_read_only_and_tagged(self) -> None:
        for name, dashboard in _dashboards().items():
            assert dashboard["editable"] is False, name
            assert "groovemap" in dashboard["tags"], name

    def test_every_panel_uses_a_datasource_variable(self) -> None:
        allowed = {DATASOURCE_VARIABLE, TRACE_DATASOURCE_VARIABLE}
        for name, dashboard in _dashboards().items():
            uids = checker.collect_datasource_uids(dashboard)
            assert uids <= allowed, (name, sorted(uids))
            assert DATASOURCE_VARIABLE in uids, name

    def test_the_trace_datasource_is_confined_to_trace_panels(self) -> None:
        for name, dashboard in _dashboards().items():
            assert checker.check_trace_datasource(name, dashboard) == []

    def test_a_dashboard_using_the_trace_variable_declares_it(self) -> None:
        for name, dashboard in _dashboards().items():
            if TRACE_DATASOURCE_VARIABLE not in checker.collect_datasource_uids(dashboard):
                continue
            variables = {variable["name"] for variable in dashboard["templating"]["list"]}
            assert "DS_TEMPO" in variables, name

    def test_every_dashboard_defines_the_datasource_variable(self) -> None:
        for name, dashboard in _dashboards().items():
            variables = {variable["name"] for variable in dashboard["templating"]["list"]}
            assert "DS_PROMETHEUS" in variables, name

    def test_dashboards_carry_a_service_or_instance_template_variable(self) -> None:
        """Every dashboard needs a way to narrow to one service, source, or queue."""
        for name, dashboard in _dashboards().items():
            variables = {variable["name"] for variable in dashboard["templating"]["list"]}
            assert variables & {"service", "source", "queue"}, (name, sorted(variables))

    def test_panels_have_unique_ids_within_a_dashboard(self) -> None:
        for name, dashboard in _dashboards().items():
            ids = [panel["id"] for panel in dashboard["panels"]]
            assert len(set(ids)) == len(ids), name

    def test_panels_use_prometheus_translated_metric_names(self) -> None:
        """OTEL dot-names never appear in a PromQL expression."""
        for name, dashboard in _dashboards().items():
            for expr in _expressions(dashboard):
                for metric in checker.extract_metric_names(expr):
                    assert "." not in metric, (name, metric)


class TestAcceptancePanels:
    """Each dashboard must actually plot what the bead asked it to plot."""

    def _metrics(self, filename: str) -> set[str]:
        dashboard = _dashboards()[filename]
        metrics: set[str] = set()
        for expr in _expressions(dashboard):
            metrics |= checker.extract_metric_names(expr)
        return metrics

    def test_pipeline_overview_covers_publish_queues_and_throughput(self) -> None:
        metrics = self._metrics("pipeline-overview.json")
        assert "groovemap_extraction_records_total" in metrics
        assert "rabbitmq_queue_messages_ready" in metrics
        assert "rabbitmq_queue_consumers" in metrics
        assert "groovemap_pipeline_messages_total" in metrics
        assert "rabbitmq_queue_messages" in metrics, "the end-to-end lag proxy needs the total backlog"

    def test_ingestion_covers_download_progress_and_publish_confirm(self) -> None:
        metrics = self._metrics("ingestion.json")
        assert "groovemap_extraction_download_bytes_total" in metrics
        assert "groovemap_extraction_file_progress_ratio" in metrics
        assert "groovemap_extraction_records_total" in metrics
        assert "groovemap_extraction_publish_confirm_duration_seconds_bucket" in metrics
        assert "groovemap_pipeline_reconnects_total" in metrics
        assert "groovemap_extraction_errors_total" in metrics

    def test_consumers_covers_batching_and_dependency_health(self) -> None:
        metrics = self._metrics("consumers.json")
        assert "groovemap_pipeline_messages_total" in metrics
        assert "groovemap_pipeline_message_duration_seconds_bucket" in metrics
        assert "groovemap_pipeline_batch_size_bucket" in metrics
        assert "groovemap_pipeline_batch_flush_duration_seconds_bucket" in metrics
        assert "db_client_operation_duration_seconds_bucket" in metrics
        assert "groovemap_pipeline_circuit_breaker_state" in metrics
        assert "groovemap_pipeline_consumers_active" in metrics

    def test_api_services_covers_red_and_the_service_specifics(self) -> None:
        metrics = self._metrics("api-services.json")
        assert "http_server_request_duration_seconds_count" in metrics
        assert "http_server_request_duration_seconds_bucket" in metrics
        assert "groovemap_api_sync_duration_seconds_bucket" in metrics
        assert "groovemap_api_cache_total" in metrics
        assert "groovemap_api_nlq_requests_total" in metrics
        assert "groovemap_insights_computation_duration_seconds_bucket" in metrics
        assert "groovemap_insights_last_success_seconds" in metrics
        assert "groovemap_mcp_tool_calls_total" in metrics

    def test_runtime_covers_process_gc_event_loop_and_tokio(self) -> None:
        metrics = self._metrics("runtime.json")
        assert "process_cpu_utilization_ratio" in metrics
        assert "process_cpu_time_seconds_total" in metrics, "Rust services have no utilisation gauge"
        assert "process_memory_usage_bytes" in metrics
        assert "process_memory_virtual_bytes" in metrics
        assert "process_thread_count" in metrics
        assert "process_open_file_descriptor_count" in metrics
        assert "cpython_gc_collections_total" in metrics
        assert "groovemap_runtime_event_loop_lag_seconds_bucket" in metrics
        assert "groovemap_runtime_tokio_alive_tasks" in metrics
        assert "groovemap_runtime_tokio_global_queue_depth" in metrics

    def test_runtime_plots_event_loop_lag_at_p95_and_p99(self) -> None:
        expressions = " ".join(_expressions(_dashboards()["runtime.json"]))
        for quantile in ("0.95", "0.99"):
            assert f"histogram_quantile({quantile}" in expressions

    def test_neo4j_covers_the_graph_gauges_and_client_latency(self) -> None:
        metrics = self._metrics("neo4j.json")
        assert "groovemap_neo4j_up" in metrics
        assert "groovemap_neo4j_nodes" in metrics
        assert "groovemap_neo4j_relationships" in metrics
        assert "groovemap_neo4j_transactions_active" in metrics
        assert "groovemap_neo4j_store_size_bytes" in metrics
        assert "db_client_operation_duration_seconds_bucket" in metrics

    def test_neo4j_client_latency_is_scoped_to_the_graph(self) -> None:
        """db.client.operation.duration is shared with PostgreSQL and Redis."""
        for expr in _expressions(_dashboards()["neo4j.json"]):
            if "db_client_operation_duration_seconds" in expr:
                assert 'db_system_name="neo4j"' in expr, expr

    def test_traces_covers_red_from_the_span_metrics(self) -> None:
        metrics = self._metrics("traces.json")
        assert "traces_span_metrics_calls_total" in metrics
        assert "traces_span_metrics_duration_seconds_bucket" in metrics

    def test_traces_plots_p50_p95_and_p99(self) -> None:
        expressions = " ".join(_expressions(_dashboards()["traces.json"]))
        for quantile in ("0.50", "0.95", "0.99"):
            assert f"histogram_quantile({quantile}" in expressions

    def test_traces_measures_errors_by_span_status(self) -> None:
        """Span metrics carry an OTLP status enum, not an HTTP status class."""
        expressions = " ".join(_expressions(_dashboards()["traces.json"]))
        assert 'status_code="STATUS_CODE_ERROR"' in expressions

    def test_traces_has_a_tempo_search_panel_bound_to_the_variable(self) -> None:
        dashboard = _dashboards()["traces.json"]
        search = [panel for panel in checker.iter_panels(dashboard) if TRACE_DATASOURCE_VARIABLE in checker.collect_datasource_uids(panel)]
        assert search, "the Traces dashboard has no panel reading the trace store"
        for panel in search:
            assert panel["type"] in checker.TRACE_PANEL_TYPES
            assert panel["datasource"] == {"type": "tempo", "uid": TRACE_DATASOURCE_VARIABLE}
            assert any(target.get("query") for target in panel["targets"]), panel["title"]

    def test_traces_red_panels_link_into_explore(self) -> None:
        dashboard = _dashboards()["traces.json"]
        links = [link for panel in checker.iter_panels(dashboard) for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])]
        assert links, "no RED panel links into Explore"
        for link in links:
            assert link["url"].startswith("/explore?"), link
            # The link resolves the trace datasource through the same variable
            # the panels do, so it survives a rebuild that renames the uid.
            assert TRACE_DATASOURCE_VARIABLE in link["url"], link

    def test_infrastructure_covers_all_three_exporters_and_the_collector(self) -> None:
        metrics = self._metrics("infrastructure.json")
        assert any(metric.startswith("rabbitmq_") for metric in metrics)
        assert any(metric.startswith("pg_") for metric in metrics)
        assert any(metric.startswith("redis_") for metric in metrics)
        assert "otelcol_receiver_accepted_metric_points_total" in metrics
        assert "otelcol_exporter_sent_metric_points_total" in metrics
        assert "otelcol_exporter_send_failed_metric_points_total" in metrics

    def test_containers_covers_per_container_usage_and_host_saturation(self) -> None:
        metrics = self._metrics("containers.json")
        for metric in (
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "container_spec_memory_limit_bytes",
            "container_network_receive_bytes_total",
            "container_network_transmit_bytes_total",
            "container_fs_reads_bytes_total",
            "container_fs_writes_bytes_total",
            "container_start_time_seconds",
        ):
            assert metric in metrics, metric
        for metric in (
            "node_cpu_seconds_total",
            "node_memory_MemAvailable_bytes",
            "node_load1",
            "node_disk_read_bytes_total",
            "node_filesystem_avail_bytes",
        ):
            assert metric in metrics, metric

    def test_containers_filters_by_the_compose_service_label(self) -> None:
        """cAdvisor's own series carry no compose identity; the whitelisted
        label is what ties a container back to a compose service."""
        dashboard = _dashboards()["containers.json"]
        container_expressions = [expr for expr in _expressions(dashboard) if "container_" in expr]
        assert container_expressions
        for expr in container_expressions:
            assert "container_label_com_docker_compose_service" in expr, expr

    def test_containers_all_excludes_series_without_the_compose_label(self) -> None:
        """`.+`, not `.*`. In PromQL a missing label reads as the empty string,
        so an `=~` matcher that accepts empty also selects every series that
        LACKS the label — and cAdvisor with --store_container_labels=false
        emits no compose label on the root cgroup or on any container started
        outside this stack. With `.*` the default All view is silently
        unfiltered: the container count includes the whole host, and each
        panel gains a blank-legend series covering it."""
        dashboard = _dashboards()["containers.json"]
        variable = next(entry for entry in dashboard["templating"]["list"] if entry["name"] == "service")
        assert variable["allValue"] == ".+"

    def test_the_infrastructure_scrape_stat_covers_the_two_new_jobs(self) -> None:
        """The stat queries the bare `up` series, so it counts every collector
        scrape job; its description names them so a reader knows what to expect."""
        dashboard = _dashboards()["infrastructure.json"]
        panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Scrape targets up")
        assert [target["expr"] for target in panel["targets"]] == ["up"]
        for job in ("cadvisor", "node-exporter"):
            assert job in panel["description"], job


class TestCatalogParsing:
    def test_catalog_yields_the_documented_metrics(self) -> None:
        catalog = checker.load_catalog()
        assert "groovemap_pipeline_messages_total" in catalog
        assert "http_server_request_duration_seconds" in catalog

    def test_the_wave_two_metrics_are_catalogued(self) -> None:
        """The dashboards below may only reference what this catalog names."""
        catalog = checker.load_catalog()
        for metric in (
            "process_cpu_time_seconds_total",
            "process_cpu_utilization_ratio",
            "process_memory_usage_bytes",
            "process_memory_virtual_bytes",
            "process_thread_count",
            "process_open_file_descriptor_count",
            "process_context_switches_total",
            "cpython_gc_collections_total",
            "groovemap_runtime_event_loop_lag_seconds",
            "groovemap_runtime_tokio_workers",
            "groovemap_runtime_tokio_alive_tasks",
            "groovemap_runtime_tokio_global_queue_depth",
            "groovemap_neo4j_up",
            "groovemap_neo4j_nodes",
            "groovemap_neo4j_relationships",
            "groovemap_neo4j_transactions_active",
            "groovemap_neo4j_store_size_bytes",
            "traces_span_metrics_calls_total",
            "traces_span_metrics_duration_seconds",
        ):
            assert metric in catalog, metric

    def test_histogram_suffixes_are_derived(self) -> None:
        catalog = checker.load_catalog()
        for suffix in ("_bucket", "_sum", "_count"):
            assert f"http_server_request_duration_seconds{suffix}" in catalog

    def test_otel_dot_names_are_not_treated_as_prometheus_names(self) -> None:
        assert not any("." in name for name in checker.load_catalog())

    def test_every_metric_a_dashboard_uses_is_allowed(self) -> None:
        allowed = checker.load_catalog() | checker.EXPORTER_METRICS
        for name, dashboard in _dashboards().items():
            for expr in _expressions(dashboard):
                for metric in checker.extract_metric_names(expr):
                    assert metric in allowed, (name, metric)


class TestMetricExtraction:
    def test_functions_and_aggregations_are_not_metrics(self) -> None:
        expr = "histogram_quantile(0.95, sum by (le, store) (rate(groovemap_pipeline_batch_size_bucket[$__rate_interval])))"
        assert checker.extract_metric_names(expr) == {"groovemap_pipeline_batch_size_bucket"}

    def test_label_matchers_and_template_variables_are_ignored(self) -> None:
        expr = 'sum by (queue) (rabbitmq_queue_messages_ready{queue=~"$queue", vhost="/"})'
        assert checker.extract_metric_names(expr) == {"rabbitmq_queue_messages_ready"}

    def test_binary_operators_keep_both_operands(self) -> None:
        expr = "sum(rate(a_total[5m])) / clamp_min(sum(rate(b_total[5m])), 0.001)"
        assert checker.extract_metric_names(expr) == {"a_total", "b_total"}

    def test_offset_and_bool_modifiers_are_dropped(self) -> None:
        assert checker.extract_metric_names("a_gauge offset 5m > bool 3") == {"a_gauge"}

    def test_on_and_ignoring_label_lists_are_dropped(self) -> None:
        assert checker.extract_metric_names("a_gauge and on (instance, job) b_gauge") == {"a_gauge", "b_gauge"}

    def test_a_bare_metric_is_extracted(self) -> None:
        assert checker.extract_metric_names("up") == {"up"}


def _minimal_dashboard(**overrides: Any) -> dict[str, Any]:
    dashboard: dict[str, Any] = {
        "uid": "test-dashboard",
        "title": "Test dashboard",
        "schemaVersion": 41,
        "templating": {"list": [{"name": "DS_PROMETHEUS", "type": "datasource"}]},
        "panels": [
            {
                "datasource": {"type": "prometheus", "uid": DATASOURCE_VARIABLE},
                "targets": [{"expr": "sum(groovemap_pipeline_messages_total)", "refId": "A"}],
            }
        ],
    }
    dashboard.update(overrides)
    return dashboard


class TestCheckerRejectsBrokenDashboards:
    """A gate that cannot fail is not a gate."""

    allowed: ClassVar[set[str]] = {"groovemap_pipeline_messages_total"}

    def test_a_correct_dashboard_passes(self) -> None:
        assert checker.check_dashboard(Path("ok.json"), _minimal_dashboard(), self.allowed) == []

    def test_an_uncatalogued_metric_is_rejected(self) -> None:
        dashboard = _minimal_dashboard()
        dashboard["panels"][0]["targets"][0]["expr"] = "sum(groovemap_typo_total)"
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("groovemap_typo_total" in problem for problem in problems)

    def test_a_hard_coded_datasource_uid_is_rejected(self) -> None:
        dashboard = _minimal_dashboard()
        dashboard["panels"][0]["datasource"] = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("hard-coded datasource uid" in problem for problem in problems)

    def test_a_missing_schema_version_is_rejected(self) -> None:
        dashboard = _minimal_dashboard()
        del dashboard["schemaVersion"]
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("schemaVersion" in problem for problem in problems)

    def test_a_missing_uid_is_rejected(self) -> None:
        dashboard = _minimal_dashboard(uid="")
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("missing uid" in problem for problem in problems)

    def test_a_missing_datasource_variable_is_rejected(self) -> None:
        dashboard = _minimal_dashboard(templating={"list": []})
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("DS_PROMETHEUS" in problem for problem in problems)

    def test_a_dashboard_with_no_queries_is_rejected(self) -> None:
        dashboard = _minimal_dashboard(panels=[])
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("no panel queries" in problem for problem in problems)


def _trace_dashboard(panel_type: str = "table", uid: str = TRACE_DATASOURCE_VARIABLE, **overrides: Any) -> dict[str, Any]:
    """A dashboard whose second panel reads the trace store."""
    dashboard = _minimal_dashboard()
    dashboard["templating"]["list"].append({"name": "DS_TEMPO", "type": "datasource"})
    dashboard["panels"].append(
        {
            "datasource": {"type": "tempo", "uid": uid},
            "title": "Trace search",
            "type": panel_type,
            "targets": [{"query": '{resource.service.name="api"}', "queryType": "traceql", "refId": "A"}],
        }
    )
    dashboard.update(overrides)
    return dashboard


class TestCheckerScopesTheTraceDatasource:
    """${DS_TEMPO} is allowed, but only where a trace can actually be drawn."""

    allowed: ClassVar[set[str]] = {"groovemap_pipeline_messages_total"}

    def test_the_trace_variable_is_accepted_on_a_trace_panel(self) -> None:
        assert checker.check_dashboard(Path("ok.json"), _trace_dashboard(), self.allowed) == []

    def test_every_trace_panel_type_is_accepted(self) -> None:
        for panel_type in sorted(checker.TRACE_PANEL_TYPES):
            dashboard = _trace_dashboard(panel_type=panel_type)
            assert checker.check_dashboard(Path("ok.json"), dashboard, self.allowed) == [], panel_type

    def test_a_raw_tempo_uid_is_rejected(self) -> None:
        """The whole point of the variable: a pinned uid only resolves where it was exported."""
        dashboard = _trace_dashboard(uid="tempo")
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("hard-coded datasource uid 'tempo'" in problem for problem in problems)

    def test_a_generated_tempo_uid_is_rejected(self) -> None:
        dashboard = _trace_dashboard(uid="PA5C4F1D2E3B6A7C8")
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("hard-coded datasource uid" in problem for problem in problems)

    def test_the_trace_variable_on_a_metric_panel_is_rejected(self) -> None:
        dashboard = _trace_dashboard(panel_type="timeseries")
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("only trace panels" in problem for problem in problems)

    def test_the_trace_variable_without_its_variable_declaration_is_rejected(self) -> None:
        dashboard = _trace_dashboard()
        dashboard["templating"]["list"] = [{"name": "DS_PROMETHEUS", "type": "datasource"}]
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("without defining the DS_TEMPO datasource variable" in problem for problem in problems)

    def test_the_trace_variable_outside_a_panel_is_rejected(self) -> None:
        dashboard = _minimal_dashboard()
        dashboard["templating"]["list"].append(
            {"name": "DS_TEMPO", "type": "datasource"},
        )
        dashboard["annotations"] = {"list": [{"datasource": {"type": "tempo", "uid": TRACE_DATASOURCE_VARIABLE}}]}
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("referenced outside a panel" in problem for problem in problems)

    def test_a_malformed_panel_entry_does_not_crash_the_gate(self) -> None:
        """A hand-edited dashboard can hold junk; the gate reports, it does not raise."""
        dashboard = _trace_dashboard()
        dashboard["panels"].append(None)
        dashboard["panels"][1]["panels"] = ["not a panel"]
        assert checker.check_dashboard(Path("ok.json"), dashboard, self.allowed) == []

    def test_a_trace_panel_nested_in_a_row_is_seen(self) -> None:
        """Collapsed rows carry their panels inside themselves, not next to them."""
        dashboard = _minimal_dashboard()
        dashboard["templating"]["list"].append({"name": "DS_TEMPO", "type": "datasource"})
        dashboard["panels"].append(
            {
                "collapsed": True,
                "title": "Traces",
                "type": "row",
                "panels": [
                    {
                        "datasource": {"type": "tempo", "uid": TRACE_DATASOURCE_VARIABLE},
                        "title": "Nested chart",
                        "type": "timeseries",
                        "targets": [{"query": "{}", "refId": "A"}],
                    }
                ],
            }
        )
        problems = checker.check_dashboard(Path("bad.json"), dashboard, self.allowed)
        assert any("'Nested chart'" in problem for problem in problems)
        assert not any("'Traces'" in problem for problem in problems), "the row itself is not the offender"


ALERT_RULES = """
apiVersion: 1
groups:
  - orgId: 1
    name: groovemap
    folder: GrooveMap
    interval: 1m
    rules:
      - uid: groovemap-queue-backlog
        title: Queue backlog is growing
        condition: C
        data:
          - refId: A
            datasourceUid: prometheus
            model:
              expr: sum(groovemap_pipeline_messages_total)
              refId: A
          - refId: C
            datasourceUid: __expr__
            model:
              type: threshold
              expression: A
              refId: C
"""


class TestCheckerLintsTheAlertRules:
    """gm-deployment-dqh.4 provisions the rules; the gate must be ready for them
    and must not fail the repository before they exist."""

    allowed: ClassVar[set[str]] = {"groovemap_pipeline_messages_total"}

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "groovemap.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_missing_file_is_not_a_failure(self, tmp_path: Path) -> None:
        assert checker.check_alerting(self.allowed, tmp_path / "absent.yaml") == []

    def test_the_repository_passes_whether_or_not_the_rules_exist(self) -> None:
        assert checker.check_alerting(checker.load_catalog() | checker.EXPORTER_METRICS) == []

    def test_a_well_formed_rule_file_passes(self, tmp_path: Path) -> None:
        assert checker.check_alerting(self.allowed, self._write(tmp_path, ALERT_RULES)) == []

    def test_an_uncatalogued_alert_metric_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("groovemap_pipeline_messages_total", "groovemap_typo_total")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("groovemap_typo_total" in problem for problem in problems)

    def test_an_unknown_alert_datasource_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("datasourceUid: prometheus", "datasourceUid: PBFA97CFB590B2093")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("unknown datasource uid" in problem for problem in problems)

    def test_a_file_with_no_groups_is_rejected(self, tmp_path: Path) -> None:
        empty = "apiVersion: 1\ngroups: []\n"
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, empty))
        assert any("provisions no alert rule groups" in problem for problem in problems)

    def test_a_group_without_rules_is_rejected(self, tmp_path: Path) -> None:
        body = "apiVersion: 1\ngroups:\n  - name: groovemap\n    folder: GrooveMap\n    rules: []\n"
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("has no rules" in problem for problem in problems)

    def test_a_rule_without_a_title_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("        title: Queue backlog is growing\n", "")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("has no title" in problem for problem in problems)

    def test_a_group_without_a_name_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("    name: groovemap\n", "")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("has no name" in problem for problem in problems)

    def test_a_group_without_a_folder_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("    folder: GrooveMap\n", "")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("names no folder" in problem for problem in problems)

    def test_the_wrong_api_version_is_rejected(self, tmp_path: Path) -> None:
        body = ALERT_RULES.replace("apiVersion: 1", "apiVersion: 2")
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, body))
        assert any("apiVersion must be 1" in problem for problem in problems)

    def test_a_document_that_is_not_a_mapping_is_rejected(self, tmp_path: Path) -> None:
        problems = checker.check_alerting(self.allowed, self._write(tmp_path, "- not a mapping\n"))
        assert any("not a provisioning document" in problem for problem in problems)

    def test_the_gate_runs_the_alerting_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() must fail on a broken rule file, not merely offer the check."""
        body = ALERT_RULES.replace("groovemap_pipeline_messages_total", "groovemap_typo_total")
        monkeypatch.setattr(checker, "ALERTING_FILE", self._write(tmp_path, body))
        assert checker.main() == 1


class TestCheckerEntryPoint:
    def test_the_real_repository_passes(self) -> None:
        assert checker.main() == 0

    def test_provisioning_check_passes(self) -> None:
        assert checker.check_provisioning() == []

    def test_the_gate_is_wired_into_source_check(self) -> None:
        justfile = (REPO_ROOT / "Justfile").read_text()
        assert "scripts/check-dashboards.py" in justfile
        source_check = justfile.partition("source-check:")[2].partition("\n\ncheck:")[0]
        assert "check-dashboards.py" in source_check, "the gate must run in just source-check, not somewhere else"


class TestDashboardDocumentation:
    def test_adding_a_dashboard_is_documented(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        assert "check-dashboards.py" in doc
        assert "DS_PROMETHEUS" in doc
        for uid in EXPECTED_UIDS:
            assert uid in doc, f"docs/observability.md does not list {uid}"

    def test_all_nine_dashboards_are_listed(self) -> None:
        """The section is the operator's index; a dashboard missing from it is invisible."""
        assert len(EXPECTED_UIDS) == 9
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        section = doc.partition("\n## Dashboards\n")[2].partition("\n### Provisioning\n")[0]
        for uid in EXPECTED_UIDS:
            assert f"`{uid}`" in section, f"the Dashboards section does not list {uid}"

    def test_the_trace_datasource_rule_is_documented(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        assert "DS_TEMPO" in doc
        assert "TraceQL" in doc

    def test_the_wave_two_catalog_sections_exist(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        for heading in ("### Runtime metrics", "### Neo4j metrics", "### Span metrics", "### Traces"):
            assert f"\n{heading}\n" in doc, f"docs/observability.md has no {heading} section"

    def test_the_trace_conventions_sit_with_the_other_conventions(self) -> None:
        doc = (REPO_ROOT / "docs" / "observability.md").read_text()
        assert doc.index("\n## Conventions\n") < doc.index("\n### Traces\n") < doc.index("\n## Metric catalog\n")


class TestCheckerMainDetectsFolderLevelProblems:
    """``main()`` owns the checks that need every dashboard at once."""

    @staticmethod
    def _run_against(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> tuple[int, str]:
        directory = tmp_path / "dashboards"
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_text(content, encoding="utf-8")
        monkeypatch.setattr(checker, "DASHBOARD_DIR", directory)
        return checker.main(), ""

    def test_a_duplicate_uid_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        first = json.dumps(_minimal_dashboard(uid="shared", title="First"))
        second = json.dumps(_minimal_dashboard(uid="shared", title="Second"))
        assert self._run_against(tmp_path, monkeypatch, {"a.json": first, "b.json": second})[0] == 1
        assert "already used by" in capsys.readouterr().out

    def test_a_duplicate_title_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        first = json.dumps(_minimal_dashboard(uid="one", title="Shared"))
        second = json.dumps(_minimal_dashboard(uid="two", title="Shared"))
        assert self._run_against(tmp_path, monkeypatch, {"a.json": first, "b.json": second})[0] == 1
        assert "'Shared' is already used by" in capsys.readouterr().out

    def test_malformed_json_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        assert self._run_against(tmp_path, monkeypatch, {"broken.json": "{not json"})[0] == 1
        assert "is not valid JSON" in capsys.readouterr().out

    def test_an_empty_dashboard_folder_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        assert self._run_against(tmp_path, monkeypatch, {})[0] == 1
        assert "no dashboards found" in capsys.readouterr().out

    def test_two_distinct_valid_dashboards_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        first = json.dumps(_minimal_dashboard(uid="one", title="First"))
        second = json.dumps(_minimal_dashboard(uid="two", title="Second"))
        assert self._run_against(tmp_path, monkeypatch, {"a.json": first, "b.json": second})[0] == 0


class TestCheckerDetectsMissingProvisioning:
    def test_a_missing_datasource_file_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(checker, "DATASOURCE_FILE", tmp_path / "gone.yaml")
        assert any("missing" in problem for problem in checker.check_provisioning())

    def test_a_missing_provider_file_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(checker, "PROVIDER_FILE", tmp_path / "gone.yaml")
        assert any("missing" in problem for problem in checker.check_provisioning())

    def test_a_datasource_with_the_wrong_uid_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        replacement = tmp_path / "prometheus.yaml"
        replacement.write_text("apiVersion: 1\ndatasources:\n  - name: Prometheus\n    uid: something-else\n", encoding="utf-8")
        monkeypatch.setattr(checker, "DATASOURCE_FILE", replacement)
        assert any("no provisioned datasource" in problem for problem in checker.check_provisioning())

    def test_a_provider_with_the_wrong_path_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        replacement = tmp_path / "groovemap.yaml"
        replacement.write_text("apiVersion: 1\nproviders:\n  - name: groovemap\n    options:\n      path: /elsewhere\n", encoding="utf-8")
        monkeypatch.setattr(checker, "PROVIDER_FILE", replacement)
        assert any("no dashboard provider" in problem for problem in checker.check_provisioning())
