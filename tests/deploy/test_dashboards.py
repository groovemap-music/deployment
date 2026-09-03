"""Tests for the provisioned dashboards and their lint gate (gm-deployment-gxr.3).

Two layers:

- the real artefacts — the five dashboards and the Grafana provisioning that
  loads them — are checked for the properties operators depend on (stable uids,
  the datasource variable, the panels the acceptance calls for);
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

EXPECTED_UIDS = {
    "groovemap-pipeline-overview",
    "groovemap-ingestion",
    "groovemap-consumers",
    "groovemap-api-services",
    "groovemap-infrastructure",
}

DATASOURCE_VARIABLE = "${DS_PROMETHEUS}"


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
        assert datasource["uid"] == "prometheus"
        assert datasource["type"] == "prometheus"
        assert datasource["url"] == "http://prometheus:9090"
        assert datasource["editable"] is False, "datasources are code; UI edits would be silently reverted"

    def test_dashboard_provider_loads_the_mounted_directory(self) -> None:
        config = yaml.safe_load((PROVISIONING / "dashboards" / "groovemap.yaml").read_text())
        assert config["apiVersion"] == 1
        provider = config["providers"][0]
        assert provider["type"] == "file"
        assert provider["folder"] == "GrooveMap"
        assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
        assert provider["allowUiUpdates"] is False


class TestDashboardInventory:
    def test_exactly_the_five_expected_dashboards_exist(self) -> None:
        uids = {dashboard["uid"] for dashboard in _dashboards().values()}
        assert uids == EXPECTED_UIDS

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

    def test_every_panel_uses_the_datasource_variable(self) -> None:
        for name, dashboard in _dashboards().items():
            uids = checker.collect_datasource_uids(dashboard)
            assert uids == {DATASOURCE_VARIABLE}, (name, sorted(uids))

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

    def test_infrastructure_covers_all_three_exporters_and_the_collector(self) -> None:
        metrics = self._metrics("infrastructure.json")
        assert any(metric.startswith("rabbitmq_") for metric in metrics)
        assert any(metric.startswith("pg_") for metric in metrics)
        assert any(metric.startswith("redis_") for metric in metrics)
        assert "otelcol_receiver_accepted_metric_points_total" in metrics
        assert "otelcol_exporter_sent_metric_points_total" in metrics
        assert "otelcol_exporter_send_failed_metric_points_total" in metrics


class TestCatalogParsing:
    def test_catalog_yields_the_documented_metrics(self) -> None:
        catalog = checker.load_catalog()
        assert "groovemap_pipeline_messages_total" in catalog
        assert "http_server_request_duration_seconds" in catalog

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
