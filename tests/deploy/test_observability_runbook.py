"""Tests for the Verification runbook in docs/observability.md (gm-deployment-gxr.4).

The runbook is the procedure that proves real service images push metrics that
reach the provisioned dashboards. Its value depends on the queries in it still
matching the metrics services actually emit, and nothing else notices when they
drift: a stale query returns an empty ``result`` array, which looks exactly like
a stack that has not done any work yet.

So the queries are pinned the same way panel queries are — against the catalog
in the same document — and the runbook's structure is pinned against the steps
the acceptance calls for.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs" / "observability.md"
CHECKER = REPO_ROOT / "scripts" / "check-dashboards.py"
DASHBOARD_DIR = REPO_ROOT / "config" / "grafana" / "dashboards"


def _load_checker() -> ModuleType:
    """Import scripts/check-dashboards.py, whose filename is not importable."""
    spec = importlib.util.spec_from_file_location("check_dashboards_runbook", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_dashboards_runbook"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

DOC = CATALOG.read_text(encoding="utf-8")
QUERIES = checker.load_runbook_queries()
ALLOWED = checker.load_catalog() | checker.EXPORTER_METRICS


def _verification_section() -> str:
    """Return the text of the Verification section only."""
    start = DOC.index("\n## Verification\n")
    rest = DOC[start + 1 :]
    end = rest.index("\n## ", 1)
    return rest[: end + 1]


class TestRunbookQueriesAreCatalogued:
    """The acceptance test for this bead: the runbook may only name real metrics."""

    def test_the_runbook_names_promql_queries(self) -> None:
        assert QUERIES, "the Verification runbook must issue PromQL queries"

    def test_every_runbook_metric_is_in_the_catalog_or_the_allowlist(self) -> None:
        assert checker.check_runbook(ALLOWED) == []

    def test_each_query_names_at_least_one_metric(self) -> None:
        for query in QUERIES:
            assert checker.extract_metric_names(query), f"no metric selected by {query!r}"

    def test_an_uncatalogued_runbook_metric_is_rejected(self, tmp_path: Path) -> None:
        forged = tmp_path / "observability.md"
        forged.write_text(
            "```bash\ncurl -sG 'http://localhost:8428/api/v1/query' \\\n  --data-urlencode 'query=sum(groovemap_not_a_real_metric_total)'\n```\n",
            encoding="utf-8",
        )
        problems = checker.check_runbook(ALLOWED, forged)
        assert any("groovemap_not_a_real_metric_total" in problem for problem in problems)

    def test_a_runbook_with_no_queries_is_rejected(self, tmp_path: Path) -> None:
        empty = tmp_path / "observability.md"
        empty.write_text("# Observability\n\nNo runbook here.\n", encoding="utf-8")
        assert checker.check_runbook(ALLOWED, empty) != []

    def test_the_gate_runs_the_runbook_check(self) -> None:
        """check-dashboards.py, which just source-check runs, must invoke check_runbook."""
        assert "check_runbook(allowed_metrics)" in CHECKER.read_text(encoding="utf-8")


class TestRunbookQueriesTrackTheDashboards:
    """A runbook query is only evidence if it is the query a panel actually runs."""

    def test_every_dashboard_has_at_least_one_runbook_query(self) -> None:
        """gm-deployment-dqh.5 replaced an equality with this.

        The count used to be exactly one query per dashboard, which was true
        while step 5 was the only step that queried anything. Step 6 then added
        the per-series checks the wave-2 acceptance calls for, so an equality
        would now forbid the very queries it was meant to guarantee. Covering
        every dashboard is the property that mattered; the count never was.
        """
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            panel_metrics: set[str] = set()
            for expr in checker.collect_expressions(dashboard):
                panel_metrics |= checker.extract_metric_names(expr)
            assert any(checker.extract_metric_names(query) & panel_metrics for query in QUERIES), (
                f"no runbook query names a metric the {dashboard['title']} dashboard charts"
            )

    def test_there_are_at_least_as_many_queries_as_dashboards(self) -> None:
        assert len(QUERIES) >= len(sorted(DASHBOARD_DIR.glob("*.json")))

    def test_every_dashboard_is_named_in_the_runbook(self) -> None:
        section = _verification_section()
        for title in ("Pipeline overview", "Ingestion", "Consumers", "API services", "Infrastructure"):
            assert title in section, f"the runbook does not cover the {title} dashboard"

    def test_every_runbook_metric_is_used_by_some_dashboard(self) -> None:
        """A query nobody's panel runs proves nothing about the dashboards."""
        panel_metrics: set[str] = set()
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            for expr in checker.collect_expressions(dashboard):
                panel_metrics |= checker.extract_metric_names(expr)

        for query in QUERIES:
            selected = checker.extract_metric_names(query)
            assert selected & panel_metrics, f"no panel uses any metric from {query!r}"


class TestRunbookIsExecutable:
    """The runbook has to be followable end to end, not a summary of one."""

    def test_the_verification_section_exists_and_precedes_rollout(self) -> None:
        assert "\n## Verification\n" in DOC
        assert DOC.index("\n## Verification\n") < DOC.index("\n## Rollout\n")

    def test_the_runbook_starts_the_stack(self) -> None:
        section = _verification_section()
        assert "just smoke" in section
        assert "docker compose up -d" in section

    def test_the_runbook_checks_collector_received_points(self) -> None:
        section = _verification_section()
        assert "otelcol_receiver_accepted_metric_points_total" in section
        assert "8888" in section

    def test_the_runbook_enumerates_service_name_values(self) -> None:
        section = _verification_section()
        assert "/api/v1/label/service_name/values" in section
        for service in (
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
            "schema-init",
        ):
            assert f"`{service}`" in section, f"{service} is missing from the expected service.name roll call"

    def test_the_runbook_opens_every_dashboard_by_uid(self) -> None:
        section = _verification_section()
        assert "/api/search?type=dash-db" in section
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            uid = json.loads(path.read_text(encoding="utf-8"))["uid"]
            assert uid in section, f"the runbook never checks for dashboard uid {uid}"

    def test_the_runbook_tears_the_stack_down(self) -> None:
        assert "docker compose down -v" in _verification_section()

    def test_the_runbook_stays_on_the_local_stack(self) -> None:
        """deployment/AGENTS.md forbids touching a live environment without approval."""
        section = _verification_section()
        assert "local Docker Compose stack" in section
        assert "operator approval" in section

    def test_the_runbook_does_not_embed_a_registry_digest(self) -> None:
        """Digests belong in .env, which is never committed."""
        assert not re.search(r"@sha256:[0-9a-f]{64}", _verification_section())


class TestExploreProxyDurationIsCatalogued:
    """The catalog addition accepted at review of gm-graph-explorer-0ng.1."""

    METRIC = "groovemap_explore_proxy_duration_seconds"

    def test_the_metric_is_in_the_catalog(self) -> None:
        assert self.METRIC in checker.load_catalog()

    def test_the_histogram_suffixes_are_derived(self) -> None:
        catalog = checker.load_catalog()
        for suffix in checker.DERIVED_SUFFIXES:
            assert self.METRIC + suffix in catalog

    def test_the_otel_dot_name_and_attributes_are_documented(self) -> None:
        assert "`groovemap.explore.proxy.duration`" in DOC
        assert "`http.route`, `outcome`" in DOC

    def test_the_rationale_against_http_client_request_duration_is_recorded(self) -> None:
        assert "http.client.request.duration" in DOC
        assert "SSE" in DOC

    def test_the_api_services_dashboard_charts_it(self) -> None:
        dashboard = json.loads((DASHBOARD_DIR / "api-services.json").read_text(encoding="utf-8"))
        metrics: set[str] = set()
        for expr in checker.collect_expressions(dashboard):
            metrics |= checker.extract_metric_names(expr)
        assert self.METRIC + "_bucket" in metrics


class TestRunbookRecordsTheKnownTraps:
    """Each of these cost real time during the first execution of this runbook."""

    def test_the_collector_self_scrape_job_label_is_explained(self) -> None:
        """resource_to_telemetry_conversion rewrites the job to the collector's service.name."""
        section = _verification_section()
        assert "otelcol-contrib" in section

    def test_the_curl_healthcheck_gap_is_recorded(self) -> None:
        """The Python images ship no curl, so `up -d --wait` cannot converge on them."""
        section = _verification_section()
        assert "curl" in section
        assert "--wait" in section

    def test_empty_panels_without_workload_are_explained(self) -> None:
        section = _verification_section()
        assert "empty" in section.lower()


class TestFirstExecutionRecord:
    """The dated evidence from the first run, and what that run never proved.

    The point of pinning this is the honesty of the negative list. Partial
    evidence written down without its gaps reads as a pass, and the next
    operator inherits a false belief that the pipeline is verified end to end.
    """

    SECTION_HEADING = "### First execution, 2026-09-03"

    def _record(self) -> str:
        section = _verification_section()
        return section[section.index(self.SECTION_HEADING) :]

    def test_the_record_exists_inside_the_verification_section(self) -> None:
        assert self.SECTION_HEADING in _verification_section()

    def test_every_built_repository_is_recorded_with_a_commit(self) -> None:
        record = self._record()
        for repository, commit in (
            ("database-schema", "c201562"),
            ("catalog-api", "1b742a4"),
            ("graph-explorer", "3078603"),
            ("analytics-engine", "787c836"),
            ("operations-console", "ca364dc"),
            ("discogs-sql-loader", "fa52a26"),
            ("musicbrainz-sql-loader", "8134b8d"),
            ("musicbrainz-graph-enricher", "d9984ea"),
            ("discogs-ingestion", "e1ddf7e"),
            ("musicbrainz-ingestion", "9a0caee"),
        ):
            assert f"`{repository}`" in record, f"{repository} is missing from the image table"
            assert f"`{commit}`" in record, f"{repository} is recorded without commit {commit}"

    def test_the_observed_service_names_are_recorded(self) -> None:
        record = self._record()
        for service in (
            "api",
            "brainzgraphinator",
            "brainztableinator",
            "dashboard",
            "explore",
            "extractor-discogs",
            "extractor-musicbrainz",
            "insights",
            "schema-init",
            "tableinator",
        ):
            assert f"`{service}`" in record, f"{service} is missing from the observed roll call"

    def test_the_confirmed_proxy_duration_series_is_recorded(self) -> None:
        assert "groovemap_explore_proxy_duration_seconds" in self._record()

    def test_the_unverified_list_is_present_and_names_all_four_gaps(self) -> None:
        record = self._record()
        assert "Not verified in this run" in record
        assert "/api/search" in record
        assert "graphinator" in record
        assert "mcp-server" in record
        assert "clean pass" in record

    def test_the_cause_of_the_interruption_is_recorded(self) -> None:
        """A gap without its cause invites someone to assume it was a flake."""
        record = self._record()
        assert "host volume filled" in record
        assert "ext4" in record

    def test_the_silent_no_op_exporter_failure_is_recorded(self) -> None:
        """discogs-sql-loader 1f9af11 shipped without the OTEL SDK and exported nothing."""
        record = self._record()
        assert "`1f9af11`" in record
        assert "no-op" in record

    def test_the_record_claims_no_dashboard_it_did_not_check(self) -> None:
        """Grafana was never confirmed through the API, so the record must not say it was."""
        record = self._record()
        assert "Unauthorized" in record
        assert "never confirmed" in record


class TestWaveTwoRunbookSteps:
    """The checks gm-deployment-dqh.5 added, one per wave-2 signal.

    Each is a step an operator can be told to run. Pinning them here is what
    stops the runbook quietly losing a signal when someone edits the section:
    a missing query looks like nothing at all in a markdown document.
    """

    def _section(self) -> str:
        return _verification_section()

    def test_the_runtime_series_are_checked(self) -> None:
        section = self._section()
        for metric in (
            "process_cpu_time_seconds_total",
            "process_cpu_utilization_ratio",
            "process_memory_virtual_bytes",
            "process_thread_count",
            "process_open_file_descriptor_count",
            "process_context_switches_total",
            "cpython_gc_collections_total",
            "groovemap_runtime_event_loop_lag_seconds_bucket",
            "groovemap_runtime_tokio_workers",
            "groovemap_runtime_tokio_alive_tasks",
            "groovemap_runtime_tokio_global_queue_depth",
        ):
            assert metric in section, f"the runbook never queries {metric}"

    def test_the_neo4j_gauges_are_checked(self) -> None:
        section = self._section()
        for metric in (
            "groovemap_neo4j_up",
            "groovemap_neo4j_transactions_active",
            "groovemap_neo4j_relationships",
            "groovemap_neo4j_store_size_bytes",
        ):
            assert metric in section, f"the runbook never queries {metric}"

    def test_the_span_metrics_are_checked(self) -> None:
        section = self._section()
        assert "traces_span_metrics_calls_total" in section
        assert "traces_span_metrics_duration_seconds_bucket" in section

    def test_the_container_and_host_series_are_checked(self) -> None:
        section = self._section()
        for metric in (
            "container_last_seen",
            "container_memory_working_set_bytes",
            "node_load1",
            "node_cpu_seconds_total",
            "node_memory_MemAvailable_bytes",
            "node_filesystem_avail_bytes",
        ):
            assert metric in section, f"the runbook never queries {metric}"

    def test_up_is_checked_per_scrape_job(self) -> None:
        """Every no-data canary rests on `up`, so the runbook proves it directly."""
        section = self._section()
        assert "min by (job) (up)" in section
        for job in ("cadvisor", "node-exporter", "otelcol-contrib", "postgres-exporter", "rabbitmq", "redis-exporter"):
            assert f"`{job}`" in section, f"{job} is missing from the expected scrape-job list"

    def test_the_trace_check_uses_the_tempo_api_and_not_promql(self) -> None:
        """TraceQL is not PromQL; a `query=` here would be linted as a metric name."""
        section = self._section()
        assert "10428/select/tempo/api/search" in section
        assert "10428/select/tempo/api/traces" in section
        assert "--data-urlencode 'q={" in section

    def test_the_trace_check_names_both_ends_of_the_trace(self) -> None:
        section = self._section()
        assert "PRODUCER" in section
        assert "CONSUMER" in section
        assert "traceparent" in section

    def test_the_traceql_syntax_traps_are_recorded(self) -> None:
        """Both cost real time on the running stack."""
        section = self._section()
        assert "{span:kind=producer}" in section
        assert "${service:regex}" in section

    def test_the_alert_rules_are_checked(self) -> None:
        section = self._section()
        assert "/api/v1/provisioning/alert-rules" in section
        assert "/api/prometheus/grafana/api/v1/rules" in section
        assert "seventeen" in section

    def test_the_extractor_download_trap_is_recorded(self) -> None:
        """It destroyed the first execution's stack; nothing else in the doc warns."""
        section = self._section()
        assert "downloading real monthly dumps" in section
        assert "docker compose stop extractor-discogs extractor-musicbrainz" in section


class TestSecondExecutionRecord:
    """The dated evidence from the wave-2 run, and what that run still did not prove.

    Pinned for the same reason the first record is: the value of an execution
    record is its negative list, and a negative list is the first thing to rot
    when someone updates the numbers above it and leaves the table alone.
    """

    SECTION_HEADING = "### Second execution, 2026-09-05"

    def _record(self) -> str:
        section = _verification_section()
        return section[section.index(self.SECTION_HEADING) :]

    def test_the_record_exists_inside_the_verification_section(self) -> None:
        assert self.SECTION_HEADING in _verification_section()

    def test_it_comes_after_the_first_execution_record(self) -> None:
        section = _verification_section()
        assert section.index("### First execution, 2026-09-03") < section.index(self.SECTION_HEADING)

    def test_every_built_repository_is_recorded_with_a_commit_and_a_service(self) -> None:
        record = self._record()
        for repository, commit, service in (
            ("database-schema", "91cbf6e", "schema-init"),
            ("catalog-api", "acdf9be", "api"),
            ("graph-explorer", "9ef18c8", "explore"),
            ("analytics-engine", "ec733f2", "insights"),
            ("operations-console", "b563f10", "dashboard"),
            ("discogs-sql-loader", "c3fba8f", "tableinator"),
            ("musicbrainz-sql-loader", "8b5f346", "brainztableinator"),
            ("discogs-graph-enricher", "ab23f7b", "graphinator"),
            ("musicbrainz-graph-enricher", "c6e3514", "brainzgraphinator"),
            ("discogs-ingestion", "403e70f", "extractor-discogs"),
            ("musicbrainz-ingestion", "0aa08e2", "extractor-musicbrainz"),
        ):
            assert f"`{repository}`" in record, f"{repository} is missing from the image table"
            assert f"`{commit}`" in record, f"{repository} is recorded without commit {commit}"
            assert f"`{service}`" in record, f"{repository} is recorded without its compose service {service}"

    def test_the_images_are_recorded_as_local_and_unpublished(self) -> None:
        """Claiming a registry digest for an image nobody pushed would be a lie."""
        record = self._record()
        assert "local image IDs, not" in record
        assert "manifest digest" in record

    def test_the_record_does_not_embed_a_registry_digest(self) -> None:
        assert not re.search(r"@sha256:[0-9a-f]{64}", self._record())

    def test_all_eleven_service_names_were_observed(self) -> None:
        record = self._record()
        for service in (
            "api",
            "brainzgraphinator",
            "brainztableinator",
            "dashboard",
            "explore",
            "extractor-discogs",
            "extractor-musicbrainz",
            "graphinator",
            "insights",
            "schema-init",
            "tableinator",
        ):
            assert f"`{service}`" in record, f"{service} is missing from the observed roll call"

    def test_up_being_queryable_is_recorded(self) -> None:
        assert "min by (job) (up)" in self._record()

    def test_the_per_language_runtime_split_is_recorded(self) -> None:
        record = self._record()
        assert "groovemap_runtime_tokio_" in record
        assert "cpython_gc_collections_total" in record
        assert "generation" in record

    def test_the_neo4j_result_and_its_one_omission_are_recorded(self) -> None:
        record = self._record()
        assert "groovemap_neo4j_up" in record
        assert "groovemap_neo4j_store_size_bytes" in record
        assert "dbms.queryJmx" in record

    def test_the_store_size_omission_names_jmx_as_the_cause(self) -> None:
        """Not-available-on-Community was the guess; no JMX agent is the cause.

        The distinction decides what to do about it: the first reads as a
        limitation to accept, the second as one compose setting away.
        """
        record = self._record()
        assert "zero beans" in record
        assert "NEO4J_server_jvm_additional" in record

    def test_the_trace_spanning_publish_and_process_is_recorded(self) -> None:
        """The acceptance in one object; a record without it proves nothing new."""
        record = self._record()
        assert "PRODUCER" in record
        assert "CONSUMER" in record
        assert "publish groovemap-discogs-artists" in record
        assert "traceparent" in record

    def test_the_per_dashboard_panel_counts_are_recorded(self) -> None:
        record = self._record()
        table = record[record.index("**Panels with data**") :]
        for dashboard in (
            "Runtime",
            "Containers & host",
            "Consumers",
            "Neo4j",
            "Infrastructure",
            "Pipeline overview",
            "API services",
            "Traces",
            "Ingestion",
        ):
            assert f"| {dashboard} |" in table, f"{dashboard} has no panel count"
        assert re.search(r"\| \d+ of \d+ \|", table), "the panel counts are not recorded as 'n of m'"

    def test_the_alert_rules_are_recorded_with_their_state(self) -> None:
        record = self._record()
        assert "seventeen" in record
        assert "`HostDiskLow`" in record
        assert "`firing`" in record
        assert "`inactive`" in record
        assert "`Neo4jDown`" in record

    def test_the_postgres_naming_defect_is_recorded_with_its_evidence(self) -> None:
        """The first record blamed sampling; this one names the cause."""
        record = self._record()
        assert "pg_stat_database_xact_commit_total" in record
        assert "rabbitmq_global_messages_received_total" in record
        assert "was wrong" in record

    def test_the_operations_console_rebuild_is_recorded(self) -> None:
        """A service that had not adopted looked healthy and said nothing."""
        record = self._record()
        assert "41805b6" in record
        assert "silent on every wave-2 series" in record

    def test_the_unverified_table_is_present_with_a_cause_for_each_row(self) -> None:
        record = self._record()
        assert "Not verified in this run" in record
        table = record[record.index("**Not verified in this run.**") :]
        rows = [line for line in table.splitlines() if line.startswith("|") and "---" not in line]
        assert len(rows) >= 7, "the not-verified table lost rows"
        for row in rows[1:]:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            assert len(cells) == 2, f"row is not item + cause: {row}"
            assert cells[1], f"row {cells[0]!r} has no cause"

    def test_the_unverified_table_names_the_known_gaps(self) -> None:
        table = self._record()
        for gap in ("mcp-server", "groovemap_neo4j_store_size_bytes", "production overlay", "Published images"):
            assert gap in table, f"{gap} is missing from the not-verified table"

    def test_the_follow_ups_are_recorded_with_their_evidence(self) -> None:
        """A defect named without its evidence is re-litigated by the next reader."""
        record = self._record()
        assert "Follow-ups this run found" in record
        table = record[record.index("**Follow-ups this run found.**") :]
        rows = [line for line in table.splitlines() if line.startswith("|") and "---" not in line]
        assert len(rows) >= 7, "the follow-up table lost rows"
        for row in rows[1:]:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            assert len(cells) == 2, f"row is not follow-up + evidence: {row}"
            assert cells[1], f"follow-up {cells[0]!r} has no evidence"

    def test_the_trace_size_follow_up_is_recorded(self) -> None:
        assert "69,760" in self._record()
