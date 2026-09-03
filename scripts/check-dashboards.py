"""Validate the provisioned Grafana dashboards.

Dashboards are code: they live in ``config/grafana/dashboards``, Grafana loads
them read-only, and nobody edits them in the UI. Nothing else notices when one
rots — a panel that references a renamed metric renders an empty graph, not an
error — so this gate runs in ``just source-check``.

It enforces five things:

1. every dashboard parses, carries a ``schemaVersion``, and has a ``uid`` and a
   ``title`` unique across the folder;
2. every datasource reference is the ``${DS_PROMETHEUS}`` variable, so a
   dashboard never pins a datasource uid that only exists on one machine;
3. every metric named in a PromQL ``expr`` is either in the catalog in
   ``docs/observability.md`` or in the exporter/collector allowlist below;
4. the provisioning files that make the variable resolvable still exist and
   still point at the mounted dashboard directory;
5. every PromQL query in the Verification runbook in ``docs/observability.md``
   names catalogued metrics too — the runbook is how an operator proves the
   dashboards have data, so a query that can never match is worse than useless.

Run directly (``uv run python scripts/check-dashboards.py``) or import
``main()``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "config" / "grafana" / "dashboards"
DATASOURCE_FILE = ROOT / "config" / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
PROVIDER_FILE = ROOT / "config" / "grafana" / "provisioning" / "dashboards" / "groovemap.yaml"
CATALOG_FILE = ROOT / "docs" / "observability.md"

DATASOURCE_VARIABLE = "${DS_PROMETHEUS}"
DATASOURCE_UID = "prometheus"
PROVISIONED_DASHBOARD_PATH = "/var/lib/grafana/dashboards"

# Series that Prometheus and the OTEL collector derive from a histogram or a
# counter. The catalog names the base metric; these are the suffixes a panel may
# append to it.
DERIVED_SUFFIXES = ("_bucket", "_sum", "_count")

# Metrics produced by the infrastructure exporters and by the collector itself.
# They are not GrooveMap metrics, so they are deliberately absent from
# docs/observability.md — but they are enumerated here rather than matched by
# prefix, so a typo in one still fails the gate.
EXPORTER_METRICS = frozenset(
    {
        # Synthesised by the collector's Prometheus receiver, one per scrape job.
        "up",
        # rabbitmq_prometheus plugin (per-object metrics enabled in compose).
        "rabbitmq_channels",
        "rabbitmq_connections",
        "rabbitmq_disk_space_available_bytes",
        "rabbitmq_global_messages_acknowledged_total",
        "rabbitmq_global_messages_published_total",
        "rabbitmq_process_resident_memory_bytes",
        "rabbitmq_queue_consumers",
        "rabbitmq_queue_messages",
        "rabbitmq_queue_messages_ready",
        "rabbitmq_queue_messages_unacked",
        "rabbitmq_resident_memory_limit_bytes",
        # prometheuscommunity/postgres-exporter.
        "pg_database_size_bytes",
        "pg_settings_max_connections",
        "pg_stat_database_blks_hit",
        "pg_stat_database_blks_read",
        "pg_stat_database_deadlocks",
        "pg_stat_database_numbackends",
        "pg_stat_database_xact_commit",
        "pg_stat_database_xact_rollback",
        "pg_up",
        # oliver006/redis_exporter.
        "redis_blocked_clients",
        "redis_commands_processed_total",
        "redis_connected_clients",
        "redis_evicted_keys_total",
        "redis_keyspace_hits_total",
        "redis_keyspace_misses_total",
        "redis_memory_max_bytes",
        "redis_memory_used_bytes",
        "redis_up",
        # The collector's own telemetry, scraped from :8888.
        "otelcol_exporter_send_failed_metric_points_total",
        "otelcol_exporter_sent_metric_points_total",
        "otelcol_receiver_accepted_metric_points_total",
        "otelcol_receiver_refused_metric_points_total",
    }
)

# PromQL words that survive tokenisation but are not metric names. Function and
# aggregation names are dropped earlier because they are followed by "(".
PROMQL_KEYWORDS = frozenset(
    {
        "and",
        "atan2",
        "bool",
        "by",
        "end",
        "group_left",
        "group_right",
        "ignoring",
        "inf",
        "nan",
        "offset",
        "on",
        "or",
        "start",
        "unless",
        "without",
    }
)

_QUOTED = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
_TEMPLATE_VARIABLE = re.compile(r"\$(?:\{[^}]*\}|\w+)")
_RANGE_SELECTOR = re.compile(r"\[[^\]]*\]")
_GROUPING_CLAUSE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
_LABEL_MATCHER = re.compile(r"\{[^{}]*\}")
_OFFSET_CLAUSE = re.compile(r"\boffset\s+[\w.]+")
_IDENTIFIER = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_CATALOG_NAME = re.compile(r"^`([a-z_][a-z0-9_]*)`$")
# The runbook issues each query as `curl -sG ... --data-urlencode 'query=<promql>'`,
# so the PromQL is exactly what sits between the single quotes.
_RUNBOOK_QUERY = re.compile(r"--data-urlencode\s+'query=([^']*)'")


def _display(path: Path) -> str:
    """Render a path relative to the repository root when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def extract_metric_names(expr: str) -> set[str]:
    """Return the metric names a PromQL expression selects.

    Strips everything that can hold an identifier which is *not* a metric name
    — string literals, ``$variables``, range selectors, ``by``/``on`` label
    lists, label matchers, and ``offset`` clauses — then keeps the bare
    identifiers that are left. Anything followed by ``(`` is a function or an
    aggregation, not a metric.
    """
    text = _QUOTED.sub(" ", expr)
    text = _TEMPLATE_VARIABLE.sub(" ", text)
    text = _RANGE_SELECTOR.sub(" ", text)
    text = _GROUPING_CLAUSE.sub(" ", text)
    text = _LABEL_MATCHER.sub(" ", text)
    text = _OFFSET_CLAUSE.sub(" ", text)

    names: set[str] = set()
    for match in _IDENTIFIER.finditer(text):
        remainder = text[match.end() :].lstrip()
        if remainder.startswith("("):
            continue
        name = match.group()
        if name in PROMQL_KEYWORDS:
            continue
        names.add(name)
    return names


def load_catalog(catalog_file: Path | None = None) -> set[str]:
    """Return the Prometheus metric names documented in the metric catalog.

    The catalog is a set of markdown tables whose second column is the
    Prometheus name in backticks. Histogram rows name only the base metric, so
    the derived ``_bucket``/``_sum``/``_count`` series are added here.
    """
    path = catalog_file if catalog_file is not None else CATALOG_FILE
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        match = _CATALOG_NAME.match(cells[1])
        if match:
            names.add(match.group(1))

    derived = {name + suffix for name in names for suffix in DERIVED_SUFFIXES}
    return names | derived


def load_runbook_queries(catalog_file: Path | None = None) -> list[str]:
    """Return the PromQL expressions the Verification runbook tells an operator to run.

    They are the ``--data-urlencode 'query=…'`` arguments of the ``curl`` calls in
    the runbook's fenced shell blocks. Order is preserved so a failure can name
    the offending query by position as well as by text.
    """
    path = catalog_file if catalog_file is not None else CATALOG_FILE
    return [match.group(1).strip() for match in _RUNBOOK_QUERY.finditer(path.read_text(encoding="utf-8"))]


def check_runbook(allowed_metrics: set[str], catalog_file: Path | None = None) -> list[str]:
    """Validate the runbook's queries against the same metric names a dashboard may use."""
    queries = load_runbook_queries(catalog_file)
    if not queries:
        return ["docs/observability.md: the Verification runbook names no PromQL queries"]

    problems: list[str] = []
    for query in queries:
        for metric in sorted(extract_metric_names(query)):
            if metric not in allowed_metrics:
                problems.append(f"runbook: metric {metric!r} is not in the catalog or the exporter allowlist ({query})")
    return problems


def _walk(node: Any) -> Any:
    """Yield every dict and list nested anywhere inside a dashboard model."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def collect_expressions(dashboard: dict[str, Any]) -> list[str]:
    """Return every PromQL expression a dashboard's panels query."""
    return [node["expr"] for node in _walk(dashboard) if isinstance(node.get("expr"), str) and node["expr"].strip()]


def collect_datasource_uids(dashboard: dict[str, Any]) -> set[str]:
    """Return every datasource uid a dashboard references."""
    uids: set[str] = set()
    for node in _walk(dashboard):
        datasource = node.get("datasource")
        if isinstance(datasource, dict) and isinstance(datasource.get("uid"), str):
            uids.add(datasource["uid"])
        elif isinstance(datasource, str):
            uids.add(datasource)
    return uids


def check_dashboard(path: Path, dashboard: dict[str, Any], allowed_metrics: set[str]) -> list[str]:
    """Validate one parsed dashboard, returning a list of human-readable problems."""
    problems: list[str] = []
    name = path.name

    for field in ("uid", "title", "schemaVersion"):
        if not dashboard.get(field):
            problems.append(f"{name}: missing {field}")

    variables = {variable.get("name") for variable in dashboard.get("templating", {}).get("list", [])}
    if "DS_PROMETHEUS" not in variables:
        problems.append(f"{name}: does not define the DS_PROMETHEUS datasource variable")

    for uid in sorted(collect_datasource_uids(dashboard)):
        if uid != DATASOURCE_VARIABLE:
            problems.append(f"{name}: hard-coded datasource uid {uid!r}; use {DATASOURCE_VARIABLE}")

    expressions = collect_expressions(dashboard)
    if not expressions:
        problems.append(f"{name}: has no panel queries")

    for expr in expressions:
        for metric in sorted(extract_metric_names(expr)):
            if metric not in allowed_metrics:
                problems.append(f"{name}: metric {metric!r} is not in the catalog or the exporter allowlist ({expr})")

    return problems


def check_provisioning() -> list[str]:
    """Verify the provisioning that makes ${DS_PROMETHEUS} resolvable."""
    problems: list[str] = []

    if not DATASOURCE_FILE.is_file():
        return [f"missing {_display(DATASOURCE_FILE)}"]
    if not PROVIDER_FILE.is_file():
        return [f"missing {_display(PROVIDER_FILE)}"]

    datasources = yaml.safe_load(DATASOURCE_FILE.read_text(encoding="utf-8"))["datasources"]
    if not any(entry.get("uid") == DATASOURCE_UID for entry in datasources):
        problems.append(f"no provisioned datasource with uid {DATASOURCE_UID!r}")

    providers = yaml.safe_load(PROVIDER_FILE.read_text(encoding="utf-8"))["providers"]
    if not any(provider.get("options", {}).get("path") == PROVISIONED_DASHBOARD_PATH for provider in providers):
        problems.append(f"no dashboard provider loading {PROVISIONED_DASHBOARD_PATH}")

    return problems


def main() -> int:
    problems = check_provisioning()

    dashboard_files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not dashboard_files:
        problems.append(f"no dashboards found in {_display(DASHBOARD_DIR)}")

    allowed_metrics = load_catalog() | EXPORTER_METRICS
    problems.extend(check_runbook(allowed_metrics))

    seen_uids: dict[str, str] = {}
    seen_titles: dict[str, str] = {}
    for path in dashboard_files:
        try:
            dashboard = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(f"{path.name}: is not valid JSON ({error})")
            continue

        problems.extend(check_dashboard(path, dashboard, allowed_metrics))

        uid = dashboard.get("uid")
        if isinstance(uid, str) and uid in seen_uids:
            problems.append(f"{path.name}: uid {uid!r} is already used by {seen_uids[uid]}")
        elif isinstance(uid, str):
            seen_uids[uid] = path.name

        title = dashboard.get("title")
        if isinstance(title, str) and title in seen_titles:
            problems.append(f"{path.name}: title {title!r} is already used by {seen_titles[title]}")
        elif isinstance(title, str):
            seen_titles[title] = path.name

    if problems:
        print("Dashboard check failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"Dashboard check passed: {len(dashboard_files)} dashboards, "
        f"{len(load_runbook_queries())} runbook queries, {len(allowed_metrics)} allowed metric names."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
