"""Validate the provisioned Grafana dashboards.

Dashboards are code: they live in ``config/grafana/dashboards``, Grafana loads
them read-only, and nobody edits them in the UI. Nothing else notices when one
rots — a panel that references a renamed metric renders an empty graph, not an
error — so this gate runs in ``just source-check``.

It enforces six things:

1. every dashboard parses, carries a ``schemaVersion``, and has a ``uid`` and a
   ``title`` unique across the folder;
2. every datasource reference is the ``${DS_PROMETHEUS}`` variable, so a
   dashboard never pins a datasource uid that only exists on one machine;
3. the ``${DS_TEMPO}`` variable is the one exception, and only inside a panel
   that renders traces — a metric panel that reaches for the trace datasource is
   a mistake, and a panel that pins the raw ``tempo`` uid is the same portability
   bug as pinning the raw Prometheus one;
4. every metric named in a PromQL ``expr`` is either in the catalog in
   ``docs/observability.md`` or in the exporter/collector allowlist below;
5. the provisioning files that make the variables resolvable still exist, still
   declare both datasource uids, and still point at the mounted dashboard
   directory — and that every expression in the provisioned alert rules is
   catalogued exactly like a panel query, that each rule carries a ``summary``,
   a ``description``, a ``severity`` label, a unique Grafana-legal uid, and a
   ``condition`` that names one of its own refIds, and that a rule names its
   datasource uid directly instead of reaching for a dashboard variable it
   cannot resolve;
6. every PromQL query in the Verification runbook in ``docs/observability.md``
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
# Grafana-managed alert rules (gm-deployment-dqh.4). Optional by design: a
# checkout with no rules is not broken, so absence is silence, not a failure.
ALERTING_FILE = ROOT / "config" / "grafana" / "provisioning" / "alerting" / "groovemap.yaml"
CATALOG_FILE = ROOT / "docs" / "observability.md"
CATALOG_HEADING = "## Metric catalog"

DATASOURCE_VARIABLE = "${DS_PROMETHEUS}"
DATASOURCE_UID = "prometheus"
TRACE_DATASOURCE_VARIABLE = "${DS_TEMPO}"
TRACE_DATASOURCE_UID = "tempo"
DATASOURCE_VARIABLES = frozenset({DATASOURCE_VARIABLE, TRACE_DATASOURCE_VARIABLE})
PROVISIONED_DASHBOARD_PATH = "/var/lib/grafana/dashboards"

# Panel types that render spans. Only these may resolve ${DS_TEMPO}: the trace
# store answers TraceQL, not PromQL, so a timeseries panel pointed at it renders
# nothing. A TraceQL search returns a list of traces, which Grafana draws in a
# table; `traces` draws one trace; `nodeGraph` draws a service graph.
TRACE_PANEL_TYPES = frozenset({"nodeGraph", "table", "traces"})

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
        # gcr.io/cadvisor/cadvisor. Per-container series; the compose service
        # key arrives as the container_label_com_docker_compose_service label,
        # which is why no label name appears in this list.
        "container_cpu_usage_seconds_total",
        "container_fs_reads_bytes_total",
        "container_fs_writes_bytes_total",
        "container_last_seen",
        "container_memory_working_set_bytes",
        "container_network_receive_bytes_total",
        "container_network_transmit_bytes_total",
        "container_oom_events_total",
        "container_spec_memory_limit_bytes",
        "container_start_time_seconds",
        # prom/node-exporter. On Docker Desktop these describe the Linux VM
        # that runs the engine, not the laptop.
        "node_cpu_seconds_total",
        "node_disk_read_bytes_total",
        "node_disk_written_bytes_total",
        "node_filesystem_avail_bytes",
        "node_filesystem_size_bytes",
        "node_load1",
        "node_load5",
        "node_load15",
        "node_memory_MemAvailable_bytes",
        "node_memory_MemTotal_bytes",
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
# Grafana's own constraint on a provisioned alert rule uid: at most 40 symbols,
# letters, numbers, hyphen, and underscore only. Grafana rejects the whole file
# when one uid breaks it, so catching it here costs a diff instead of an
# alerting stack that silently provisions nothing.
ALERT_RULE_UID_MAX_LENGTH = 40
_ALERT_RULE_UID = re.compile(r"[A-Za-z0-9_-]+")
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


def _catalog_section(text: str) -> str:
    """Return the ``## Metric catalog`` section of the observability document.

    Empty when the heading is gone, which fails every dashboard at once and
    names the real problem — far better than falling back to the whole document
    and quietly re-admitting the tables this scoping exists to exclude.
    """
    _, marker, remainder = ("\n" + text).partition(f"\n{CATALOG_HEADING}\n")
    if not marker:
        return ""
    return remainder.partition("\n## ")[0]


def load_catalog(catalog_file: Path | None = None) -> set[str]:
    """Return the Prometheus metric names documented in the metric catalog.

    The catalog is a set of markdown tables whose second column is the
    Prometheus name in backticks. Histogram rows name only the base metric, so
    the derived ``_bucket``/``_sum``/``_count`` series are added here.

    Only the ``## Metric catalog`` section is read. The document holds other
    tables — the Dashboards inventory, the Alerts table — whose second column is
    also a backticked identifier, and letting one of those widen the allowlist
    would mean a rule could catalogue its own typo just by tabulating it.
    """
    path = catalog_file if catalog_file is not None else CATALOG_FILE
    names: set[str] = set()
    for line in _catalog_section(path.read_text(encoding="utf-8")).splitlines():
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


def iter_panels(dashboard: dict[str, Any]) -> Any:
    """Yield every panel, including the ones nested inside a collapsed row."""
    for panel in dashboard.get("panels") or []:
        if not isinstance(panel, dict):
            continue
        yield panel
        for nested in panel.get("panels") or []:
            if isinstance(nested, dict):
                yield nested


def _without_nested_panels(node: dict[str, Any]) -> dict[str, Any]:
    """Return the node without its ``panels`` key, so a walk stays out of children."""
    return {key: value for key, value in node.items() if key != "panels"}


def check_trace_datasource(name: str, dashboard: dict[str, Any]) -> list[str]:
    """Confine ${DS_TEMPO} to the panels that can actually render a trace.

    The trace datasource answers TraceQL. A metric panel pointed at it renders
    an empty graph rather than an error, which is the failure mode this whole
    script exists to prevent.
    """
    problems: list[str] = []

    if TRACE_DATASOURCE_VARIABLE in collect_datasource_uids(_without_nested_panels(dashboard)):
        problems.append(f"{name}: {TRACE_DATASOURCE_VARIABLE} is referenced outside a panel")

    for panel in iter_panels(dashboard):
        if TRACE_DATASOURCE_VARIABLE not in collect_datasource_uids(_without_nested_panels(panel)):
            continue
        kind = panel.get("type")
        if kind not in TRACE_PANEL_TYPES:
            title = panel.get("title", "<untitled>")
            allowed = ", ".join(sorted(TRACE_PANEL_TYPES))
            problems.append(f"{name}: panel {title!r} has type {kind!r} and uses {TRACE_DATASOURCE_VARIABLE}; only trace panels ({allowed}) may")

    return problems


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

    uids = collect_datasource_uids(dashboard)
    if TRACE_DATASOURCE_VARIABLE in uids and "DS_TEMPO" not in variables:
        problems.append(f"{name}: uses {TRACE_DATASOURCE_VARIABLE} without defining the DS_TEMPO datasource variable")

    for uid in sorted(uids):
        if uid not in DATASOURCE_VARIABLES:
            problems.append(
                f"{name}: hard-coded datasource uid {uid!r}; use {DATASOURCE_VARIABLE} or, for a trace panel, {TRACE_DATASOURCE_VARIABLE}"
            )

    problems.extend(check_trace_datasource(name, dashboard))

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
    for uid in (DATASOURCE_UID, TRACE_DATASOURCE_UID):
        if not any(entry.get("uid") == uid for entry in datasources):
            problems.append(f"no provisioned datasource with uid {uid!r}")

    providers = yaml.safe_load(PROVIDER_FILE.read_text(encoding="utf-8"))["providers"]
    if not any(provider.get("options", {}).get("path") == PROVISIONED_DASHBOARD_PATH for provider in providers):
        problems.append(f"no dashboard provider loading {PROVISIONED_DASHBOARD_PATH}")

    return problems


def check_alert_rule(name: str, title: str, rule: dict[str, Any], seen_uids: set[str]) -> list[str]:
    """Validate one alert rule's contract with the operator who receives it.

    A rule that fires without saying what broke and how badly is a pager
    message nobody can act on, so ``summary``, ``description``, and a
    ``severity`` label are as mandatory as the query itself. ``condition`` must
    name a refId the rule actually computes — Grafana rejects a dangling
    condition at load time and provisions nothing, which is a silent loss of
    every rule in the file.
    """
    problems: list[str] = []

    uid = rule.get("uid")
    if not uid:
        problems.append(f"{name}: alert rule {title!r} has no uid")
    else:
        if uid in seen_uids:
            problems.append(f"{name}: alert rule uid {uid!r} is used more than once")
        seen_uids.add(uid)
        if len(uid) > ALERT_RULE_UID_MAX_LENGTH or not _ALERT_RULE_UID.fullmatch(uid):
            problems.append(f"{name}: alert rule uid {uid!r} must be at most {ALERT_RULE_UID_MAX_LENGTH} characters of letters, digits, '-', or '_'")

    annotations = rule.get("annotations") or {}
    for annotation in ("summary", "description"):
        if not str(annotations.get(annotation, "")).strip():
            problems.append(f"{name}: alert rule {title!r} has no {annotation} annotation")

    if not str((rule.get("labels") or {}).get("severity", "")).strip():
        problems.append(f"{name}: alert rule {title!r} carries no severity label")

    data = rule.get("data") or []
    ref_ids = {item.get("refId") for item in data if isinstance(item, dict)}
    condition = rule.get("condition")
    if not condition:
        problems.append(f"{name}: alert rule {title!r} names no condition")
    elif condition not in ref_ids:
        problems.append(f"{name}: alert rule {title!r} has condition {condition!r}, which is not one of its refIds {sorted(r for r in ref_ids if r)}")

    if not any(isinstance(item, dict) and isinstance(item.get("model"), dict) and item["model"].get("expr") for item in data):
        problems.append(f"{name}: alert rule {title!r} runs no PromQL query")

    return problems


def check_alerting(allowed_metrics: set[str], alerting_file: Path | None = None) -> list[str]:
    """Validate the provisioned Grafana-managed alert rules, when there are any.

    An alert rule is a saved query with a threshold on it, so it rots the same
    way a panel does and is linted the same way. The file is optional: it is
    provisioned by ``gm-deployment-dqh.4`` and a repository without it is not
    broken, so absence is silence rather than a failure.
    """
    path = alerting_file if alerting_file is not None else ALERTING_FILE
    if not path.is_file():
        return []

    name = _display(path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return [f"{name}: is not a provisioning document"]

    problems: list[str] = []
    if document.get("apiVersion") != 1:
        problems.append(f"{name}: apiVersion must be 1")

    groups = document.get("groups")
    if not isinstance(groups, list) or not groups:
        return [*problems, f"{name}: provisions no alert rule groups"]

    seen_rule_uids: set[str] = set()
    for group in groups:
        if not group.get("name"):
            problems.append(f"{name}: an alert rule group has no name")
        if not group.get("folder"):
            problems.append(f"{name}: alert rule group {group.get('name')!r} names no folder")
        rules = group.get("rules") or []
        if not rules:
            problems.append(f"{name}: alert rule group {group.get('name')!r} has no rules")
        for rule in rules:
            title = rule.get("title")
            if not title:
                problems.append(f"{name}: an alert rule in group {group.get('name')!r} has no title")
            problems.extend(check_alert_rule(name, title or "<untitled>", rule, seen_rule_uids))

    # A rule names its datasource by uid, in `datasourceUid` as well as in the
    # `datasource` block a query model carries. `__expr__` and `-100` are
    # Grafana's built-in server-side expression datasource.
    #
    # This is the one place a raw uid is correct rather than the portability bug
    # `check_dashboard` rejects. A dashboard resolves ${DS_PROMETHEUS} in the
    # browser, against the datasource list of whatever Grafana served the page.
    # An alert rule has no browser and no dashboard: the scheduler evaluates it
    # server-side and looks the datasource up by uid, so a template variable
    # there is an unresolvable string, not a portable reference. The uid is safe
    # to hard-code precisely because provisioning fixes it — `prometheus` and
    # `tempo` are pinned in the datasource file for exactly this reason.
    referenced = collect_datasource_uids(document) | {node["datasourceUid"] for node in _walk(document) if isinstance(node.get("datasourceUid"), str)}
    for uid in sorted(referenced):
        if uid in DATASOURCE_VARIABLES:
            problems.append(f"{name}: alert rule uses the dashboard variable {uid}; a rule is evaluated server-side and must name the uid directly")
        elif uid not in {DATASOURCE_UID, TRACE_DATASOURCE_UID, "__expr__", "-100"}:
            problems.append(f"{name}: alert rule reads unknown datasource uid {uid!r}")

    # Alert rules are provisioned server-side, so they name the datasource uid
    # directly; only their expressions are shared with the dashboards.
    for expr in collect_expressions(document):
        for metric in sorted(extract_metric_names(expr)):
            if metric not in allowed_metrics:
                problems.append(f"{name}: metric {metric!r} is not in the catalog or the exporter allowlist ({expr})")

    return problems


def main() -> int:
    problems = check_provisioning()

    dashboard_files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not dashboard_files:
        problems.append(f"no dashboards found in {_display(DASHBOARD_DIR)}")

    if not _catalog_section(CATALOG_FILE.read_text(encoding="utf-8")).strip():
        problems.append(f"{_display(CATALOG_FILE)}: has no '{CATALOG_HEADING}' section, so no metric name is catalogued")

    allowed_metrics = load_catalog() | EXPORTER_METRICS
    problems.extend(check_runbook(allowed_metrics))
    problems.extend(check_alerting(allowed_metrics))

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
