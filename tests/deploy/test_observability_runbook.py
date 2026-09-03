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
            "```bash\ncurl -sG 'http://localhost:9090/api/v1/query' \\\n  --data-urlencode 'query=sum(groovemap_not_a_real_metric_total)'\n```\n",
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

    def test_there_is_one_query_per_dashboard(self) -> None:
        assert len(QUERIES) == len(sorted(DASHBOARD_DIR.glob("*.json")))

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
