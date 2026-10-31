"""Report whether the Discogs import has finished, from the signals the consumers persist.

"Extraction finished" is not "import finished". The extractor publishes asynchronously, so
the import is done only when both Discogs consumers have taken in every entity type of the
same dump and the SQL loader has finished its derived-relation pass over it. Each consumer
records that durably, keyed on the dump's `version` (the extractor's YYYYMMDD stamp, carried
on every `extraction_complete` message to both consumers' queues):

- PostgreSQL `public.loader_extraction_latch` (declared by database-schema, written by
  discogs-sql-loader's `tableinator/extraction_latch.py` and `durable_refresh.py`): one row
  per `(loader, version)`. `signals` collects the entity types that have signalled, and
  `refreshed_at` is stamped in the same transaction that commits the derived-relation pass
  (graph counters, `artist_member_of`, `vertex_degree`) — on both the durable job path and
  the retained inline path. All four signals alone means only that every queue reached its
  terminal marker; the pass that sums whole edge tables may still be running or retrying,
  so a complete import also requires `refreshed_at IS NOT NULL`.
- Neo4j `(:ExtractionCompletion {version})` (discogs-graph-enricher's
  `graphinator._persist_extraction_signals`), whose `signals` collects the same four types.
- RabbitMQ: the eight `groovemap-discogs-{graphinator,tableinator}-<type>` consumer queues
  at zero ready and zero unacknowledged messages. That rules out a newer dump already in
  flight behind a latch the previous dump completed. Consumers are reported but do not gate:
  both consumers cancel their consumers `CONSUMER_CANCEL_DELAY` (300s) after finishing and
  then re-check hourly, so a finished import's queues normally have none. A backlog with no
  consumer attached is called out, because nothing is draining it.

The three must agree on one version. Only the newest version is judged, so a previous
dump's complete rows can never stand in for the current one, and a graph latch for a newer
version than PostgreSQL has seen (or the reverse) is still in progress.

Every store is queried through `docker compose exec` into the running stack, so the host
needs no database clients and no secrets: credentials are read inside each container, from
the Docker secret files the production overlay mounts or from the base file's environment.
RabbitMQ is queried with `rabbitmqctl`, which authenticates with the node's Erlang cookie.
PostgreSQL goes through the store-access layer (scripts/stores.py): with GROOVEMAP_POSTGRES=external
in .env (or the shell) `--prod` adds docker-compose.external-postgres.yml, exactly as `just
deploy` does, and psql runs in a throwaway store-check-postgres container against the external
server the loaders write to. An external store that cannot be reached is "cannot determine".

This script only reads. Exit codes: 0 complete, 3 in progress, 2 cannot determine (stack or
service down, relation or queue missing, unreadable output). Anything uncertain is 2, never
0. `--wait` polls until complete and is safe to interrupt and re-run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _load_stores() -> Any:
    """scripts/stores.py by path: this script runs without the repository on sys.path."""
    name = "groovemap_stores"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("stores.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stores = _load_stores()

# `just config-prod` composes the production stack from exactly these files.
PROD_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")

# catalog-api's `DISCOGS_DATA_TYPES` and the producer's `ENTITY_TYPES`: the four entity types
# whose `extraction_complete` one Discogs dump collects.
DATA_TYPES = ("artists", "labels", "masters", "releases")

# The producer-owned names (ADR 0005 freezes them): `{prefix}-{consumer}-{entity}`, with
# `.dlq` dead-letter queues beside each.
EXCHANGE_PREFIX = "groovemap-discogs"
CONSUMERS = ("graphinator", "tableinator")
CONSUMER_QUEUES = tuple(f"{EXCHANGE_PREFIX}-{consumer}-{entity}" for consumer in CONSUMERS for entity in DATA_TYPES)
DEAD_LETTER_QUEUES = tuple(f"{queue}.dlq" for queue in CONSUMER_QUEUES)

# discogs-sql-loader's durable path accepts only this shape (`durable_refresh.ordered_version`)
# and orders extractions by it, not by arrival.
ORDERED_VERSION = re.compile(r"\A[0-9]{8}\Z")

PROBE_TIMEOUT_SECONDS = 60.0
DEFAULT_INTERVAL_SECONDS = 30.0

# The store-access layer's psql shell (scripts/stores.py): run in the postgres container, or in
# the store-check-postgres one-shot for an external PostgreSQL. The query is `$1`.
PSQL_SHELL: str = stores.PSQL_SHELL

# Run inside the neo4j container. `neo4j-entrypoint.sh` seeds the server from the secret file
# when the production overlay mounts it; otherwise the base file's NEO4J_AUTH holds it.
# cypher-shell reads NEO4J_PASSWORD from its environment, which keeps it off the argv.
CYPHER_SHELL = (
    'if [ -f /run/secrets/neo4j_password ]; then NEO4J_PASSWORD="$(cat /run/secrets/neo4j_password)"; '
    'else NEO4J_PASSWORD="${NEO4J_AUTH#*/}"; fi; '
    "export NEO4J_PASSWORD; "
    'exec cypher-shell -u neo4j -d neo4j --format plain "$1"'
)

RELATIONS_SQL = "SELECT to_regclass('public.loader_extraction_latch') IS NOT NULL, to_regclass('public.loader_derived_refresh_job') IS NOT NULL"

LATCH_SQL = """
SELECT coalesce(json_agg(json_build_object(
    'version', latch.version,
    'signals', latch.signals,
    'refreshed_at', latch.refreshed_at,
    'created_at', latch.created_at,
    'job_state', {job_state},
    'job_error', {job_error}
) ORDER BY latch.created_at), '[]'::json)
FROM public.loader_extraction_latch AS latch
{job_join}
WHERE latch.loader = 'discogs'
"""

JOB_JOIN = "LEFT JOIN public.loader_derived_refresh_job AS job ON job.loader = latch.loader AND job.version = latch.version"

GRAPH_CYPHER = "MATCH (m:ExtractionCompletion) RETURN coalesce(toString(m.version), '') AS version, coalesce(m.signals, []) AS signals"
GRAPH_HEADER = "version, signals"

RABBITMQCTL = (
    "rabbitmqctl",
    "--quiet",
    "--formatter",
    "json",
    "list_queues",
    "--vhost",
    "/",
    "name",
    "messages_ready",
    "messages_unacknowledged",
    "consumers",
)


class Status(IntEnum):
    """One signal's verdict, and the process exit code for the overall verdict."""

    COMPLETE = 0
    UNKNOWN = 2
    IN_PROGRESS = 3

    @property
    def label(self) -> str:
        return {Status.COMPLETE: "complete", Status.UNKNOWN: "cannot determine", Status.IN_PROGRESS: "in progress"}[self]


class ProbeError(RuntimeError):
    """A store could not be asked, or its answer could not be read."""


@dataclass(frozen=True)
class LatchRow:
    """One `loader_extraction_latch` row for loader 'discogs'."""

    version: str
    signals: frozenset[str]
    refreshed: bool
    created_at: datetime
    job_state: str | None = None
    job_error: str | None = None


@dataclass(frozen=True)
class GraphCompletion:
    """One `(:ExtractionCompletion)` node."""

    version: str
    signals: frozenset[str]


@dataclass(frozen=True)
class QueueState:
    """One RabbitMQ queue's depth and consumers."""

    name: str
    ready: int
    unacked: int
    consumers: int


@dataclass(frozen=True)
class Observation:
    """What each store answered, or why it could not answer.

    A store's value is None exactly when its error is set; an empty tuple is a real answer
    ("no rows"), never a stand-in for a failure.
    """

    latch: tuple[LatchRow, ...] | None = None
    latch_error: str | None = None
    graph: tuple[GraphCompletion, ...] | None = None
    graph_error: str | None = None
    queues: tuple[QueueState, ...] | None = None
    queues_error: str | None = None


@dataclass(frozen=True)
class Signal:
    """One gate condition and how it stands."""

    name: str
    status: Status
    summary: str
    detail: str


@dataclass(frozen=True)
class Report:
    """The verdict over every signal, for one version."""

    status: Status
    version: str | None
    signals: tuple[Signal, ...]
    queues: tuple[QueueState, ...]
    warnings: tuple[str, ...]


# --- decision logic (pure) -------------------------------------------------------------


def _ordered(version: str) -> bool:
    return bool(ORDERED_VERSION.match(version))


def latest_latch(rows: Iterable[LatchRow]) -> LatchRow | None:
    """Return the current extraction's row.

    Ordered by the YYYYMMDD version, the same order the durable loader fences extractions
    by — a superseded straggler can insert a row after a newer dump started, so arrival
    order is not extraction order. Only when no row carries an ordered version (legacy
    `started_at` keys) does creation time decide.
    """
    rows = list(rows)
    ordered = [row for row in rows if _ordered(row.version)]
    if ordered:
        return max(ordered, key=lambda row: row.version)
    return max(rows, key=lambda row: row.created_at, default=None)


def _signal_list(signals: frozenset[str]) -> str:
    present = [data_type for data_type in DATA_TYPES if data_type in signals]
    return ", ".join(present) if present else "none"


def _missing(signals: frozenset[str]) -> list[str]:
    return [data_type for data_type in DATA_TYPES if data_type not in signals]


def _count(signals: frozenset[str]) -> str:
    return f"{len(DATA_TYPES) - len(_missing(signals))}/{len(DATA_TYPES)}"


def evaluate_latch(observation: Observation) -> tuple[Signal, Signal, LatchRow | None]:
    """Judge the PostgreSQL latch: all four signals, then the derived-relation stamp."""
    if observation.latch is None:
        reason = observation.latch_error or "not queried"
        return (
            Signal("postgres latch", Status.UNKNOWN, "unknown", reason),
            Signal("postgres refresh", Status.UNKNOWN, "unknown", reason),
            None,
        )
    latest = latest_latch(observation.latch)
    if latest is None:
        detail = "no loader='discogs' row yet: no entity type of any extraction has signalled"
        return (
            Signal("postgres latch", Status.IN_PROGRESS, "no extraction", detail),
            Signal("postgres refresh", Status.IN_PROGRESS, "no extraction", "waiting for an extraction to complete"),
            None,
        )
    missing = _missing(latest.signals)
    previous = sorted(
        (row.version for row in observation.latch if row is not latest and not _missing(row.signals) and row.refreshed),
        reverse=True,
    )
    stale = f"; previous extraction {previous[0]} is complete and no longer counts" if previous and missing else ""
    if missing:
        latch = Signal(
            "postgres latch",
            Status.IN_PROGRESS,
            _count(latest.signals),
            f"{latest.version}: signalled {_signal_list(latest.signals)}; waiting on {', '.join(missing)}{stale}",
        )
    else:
        latch = Signal("postgres latch", Status.COMPLETE, _count(latest.signals), f"{latest.version}: all of {', '.join(DATA_TYPES)} signalled")

    if latest.refreshed:
        refresh = Signal("postgres refresh", Status.COMPLETE, "refreshed", f"{latest.version}: derived-relation refresh stamped")
    else:
        job = f" (refresh job: {latest.job_state})" if latest.job_state else ""
        error = f"; last error: {latest.job_error}" if latest.job_error else ""
        waiting = "waiting for the remaining signals" if missing else "derived-relation refresh not yet stamped"
        refresh = Signal("postgres refresh", Status.IN_PROGRESS, latest.job_state or "pending", f"{latest.version}: {waiting}{job}{error}")
    return latch, refresh, latest


def evaluate_graph(observation: Observation, latest: LatchRow | None) -> Signal:
    """Judge the Neo4j latch for the version PostgreSQL names as current."""
    name = "neo4j latch"
    if observation.graph is None:
        return Signal(name, Status.UNKNOWN, "unknown", observation.graph_error or "not queried")
    newest_graph = max((node.version for node in observation.graph if _ordered(node.version)), default=None)
    if latest is None:
        if observation.latch is None:
            return Signal(name, Status.UNKNOWN, "unmatched", "PostgreSQL could not name the current version to match against")
        seen = f"; graph has {newest_graph}" if newest_graph else ""
        return Signal(name, Status.IN_PROGRESS, "no extraction", f"no PostgreSQL extraction to match{seen}")
    if newest_graph is not None and _ordered(latest.version) and newest_graph > latest.version:
        return Signal(
            name,
            Status.IN_PROGRESS,
            f"version {newest_graph}",
            f"version mismatch: graph has started {newest_graph}, PostgreSQL's newest is {latest.version}",
        )
    node = next((node for node in observation.graph if node.version == latest.version), None)
    if node is None:
        seen = f" (newest graph version: {newest_graph})" if newest_graph else ""
        return Signal(name, Status.IN_PROGRESS, "absent", f"version mismatch: no ExtractionCompletion for {latest.version}{seen}")
    missing = _missing(node.signals)
    if missing:
        return Signal(
            name,
            Status.IN_PROGRESS,
            _count(node.signals),
            f"{node.version}: signalled {_signal_list(node.signals)}; waiting on {', '.join(missing)}",
        )
    return Signal(name, Status.COMPLETE, _count(node.signals), f"{node.version}: all of {', '.join(DATA_TYPES)} signalled")


def evaluate_queues(observation: Observation) -> tuple[Signal, tuple[QueueState, ...], tuple[str, ...]]:
    """Judge the consumer queues: drained, whether or not a consumer is still attached."""
    name = "rabbitmq queues"
    if observation.queues is None:
        return Signal(name, Status.UNKNOWN, "unknown", observation.queues_error or "not queried"), (), ()
    by_name = {queue.name: queue for queue in observation.queues}
    consumer_queues = tuple(by_name[queue] for queue in CONSUMER_QUEUES if queue in by_name)
    warnings = tuple(
        f"{by_name[queue].name} holds {by_name[queue].ready + by_name[queue].unacked} dead-lettered message(s); they will not be retried"
        for queue in DEAD_LETTER_QUEUES
        if queue in by_name and by_name[queue].ready + by_name[queue].unacked > 0
    )
    missing = [queue for queue in CONSUMER_QUEUES if queue not in by_name]
    if missing:
        return Signal(name, Status.UNKNOWN, "missing", f"not declared (consumer never started?): {', '.join(missing)}"), consumer_queues, warnings
    ready = sum(queue.ready for queue in consumer_queues)
    unacked = sum(queue.unacked for queue in consumer_queues)
    consumers = sum(queue.consumers for queue in consumer_queues)
    summary = f"ready={ready} unacked={unacked}"
    if ready or unacked:
        unattended = [queue.name for queue in consumer_queues if queue.ready + queue.unacked > 0 and queue.consumers == 0]
        stalled = f"; no consumer attached to {', '.join(unattended)} (idle consumers re-check hourly; is the service running?)" if unattended else ""
        return (
            Signal(name, Status.IN_PROGRESS, summary, f"{ready} ready, {unacked} unacknowledged across {len(consumer_queues)} queues{stalled}"),
            consumer_queues,
            warnings,
        )
    idle = " (consumers detached after going idle, as expected once drained)" if consumers == 0 else ""
    return (
        Signal(name, Status.COMPLETE, summary, f"all {len(consumer_queues)} queues drained, {consumers} consumer(s){idle}"),
        consumer_queues,
        warnings,
    )


def evaluate(observation: Observation) -> Report:
    """Combine every signal. Any unknown makes the whole verdict unknown, never complete."""
    latch, refresh, latest = evaluate_latch(observation)
    graph = evaluate_graph(observation, latest)
    queues, queue_states, warnings = evaluate_queues(observation)
    signals = (latch, refresh, graph, queues)
    if any(signal.status is Status.UNKNOWN for signal in signals):
        status = Status.UNKNOWN
    elif any(signal.status is Status.IN_PROGRESS for signal in signals):
        status = Status.IN_PROGRESS
    else:
        status = Status.COMPLETE
    return Report(status, latest.version if latest else None, signals, queue_states, warnings)


# --- reading store answers (pure) ------------------------------------------------------


# Python 3.10's fromisoformat takes a fraction of exactly 3 or 6 digits and no trailing Z, but
# PostgreSQL's JSON output drops trailing zeros ("...:43.89805+00:00").
_FRACTION = re.compile(r"(?<=:\d\d)\.(\d{1,6})(?=Z|[+-]\d\d:?\d\d$|$)")


def _pad_fraction(value: str) -> str:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return _FRACTION.sub(lambda match: "." + match[1].ljust(6, "0"), value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ProbeError(f"unreadable latch timestamp: {value!r}")
    try:
        return datetime.fromisoformat(_pad_fraction(value))
    except ValueError as error:
        raise ProbeError(f"unreadable latch timestamp: {value!r}") from error


def _strings(value: Any, what: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProbeError(f"unreadable {what}: {value!r}")
    return frozenset(value)


def parse_latch(output: str) -> tuple[LatchRow, ...]:
    """Read the JSON array `LATCH_SQL` returns."""
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as error:
        raise ProbeError(f"unreadable latch query output: {output.strip()[:200]!r}") from error
    if not isinstance(rows, list):
        raise ProbeError("latch query did not return a JSON array")
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("version"), str):
            raise ProbeError(f"unreadable latch row: {row!r}")
        job_state = row.get("job_state")
        job_error = row.get("job_error")
        parsed.append(
            LatchRow(
                version=row["version"],
                signals=_strings(row.get("signals"), "latch signals"),
                refreshed=row.get("refreshed_at") is not None,
                created_at=_timestamp(row.get("created_at")),
                job_state=str(job_state) if job_state is not None else None,
                job_error=str(job_error) if job_error is not None else None,
            )
        )
    return tuple(parsed)


def parse_relations(output: str) -> tuple[bool, bool]:
    """Read `RELATIONS_SQL`'s `t|f` row: (latch present, refresh job present)."""
    fields = output.strip().split("|")
    if len(fields) != 2 or any(field not in ("t", "f") for field in fields):
        raise ProbeError(f"unreadable relation probe output: {output.strip()[:200]!r}")
    return fields[0] == "t", fields[1] == "t"


def parse_graph(output: str) -> tuple[GraphCompletion, ...]:
    """Read cypher-shell's plain output for `GRAPH_CYPHER`.

    Every value is a string or a list of strings, which cypher-shell renders as JSON does, so
    each row read as a JSON array element pair. Anything else is refused rather than guessed.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ()
    if lines[0] != GRAPH_HEADER:
        raise ProbeError(f"unexpected cypher-shell header: {lines[0][:200]!r}")
    nodes = []
    for line in lines[1:]:
        try:
            row = json.loads(f"[{line}]")
        except json.JSONDecodeError as error:
            raise ProbeError(f"unreadable ExtractionCompletion row: {line[:200]!r}") from error
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str):
            raise ProbeError(f"unreadable ExtractionCompletion row: {line[:200]!r}")
        nodes.append(GraphCompletion(row[0], _strings(row[1], "graph signals")))
    return tuple(nodes)


def parse_queues(output: str) -> tuple[QueueState, ...]:
    """Read `rabbitmqctl list_queues --formatter json`."""
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as error:
        raise ProbeError(f"unreadable rabbitmqctl output: {output.strip()[:200]!r}") from error
    if not isinstance(rows, list):
        raise ProbeError("rabbitmqctl did not return a JSON array")
    queues = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ProbeError(f"unreadable queue row: {row!r}")
        counts = [row.get(key) for key in ("messages_ready", "messages_unacknowledged", "consumers")]
        usable = [count for count in counts if isinstance(count, int) and not isinstance(count, bool) and count >= 0]
        if len(usable) != len(counts):
            if row["name"] in CONSUMER_QUEUES:
                raise ProbeError(f"{row['name']} reported no usable depth (queue down or leaderless?): {row!r}")
            continue
        ready, unacked, consumers = usable
        queues.append(QueueState(row["name"], ready, unacked, consumers))
    return tuple(queues)


# --- probing the stack (impure) --------------------------------------------------------


class Compose:
    """The operator's stack, addressed exactly as `just config` / `just config-prod` do."""

    def __init__(
        self,
        files: Sequence[str] = (),
        env_file: str | None = None,
        project_name: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        configured: Any = None,
        store_error: str | None = None,
    ) -> None:
        self.files = list(files)
        self.env_file = env_file
        self.project_name = project_name
        self.runner = runner
        self.timeout = timeout
        # Where PostgreSQL lives (stores.Stores), or why that cannot be told; the latter makes every PostgreSQL probe unknown.
        self.stores = configured or stores.Stores()
        self.store_error = store_error

    def argv(self) -> list[str]:
        argv = ["docker", "compose"]
        if self.project_name is not None:
            argv += ["--project-name", self.project_name]
        if self.env_file is not None:
            argv += ["--env-file", self.env_file]
        for compose_file in self.files:
            argv += ["-f", compose_file]
        return argv

    def exec(self, service: str, command: Sequence[str]) -> str:
        """Run a read-only command in a running service container and return its stdout."""
        return self._run([*self.argv(), "exec", "-T", service, *command], service)

    def _run(self, argv: Sequence[str], service: str) -> str:
        try:
            completed = self.runner(argv, capture_output=True, text=True, timeout=self.timeout, check=False, cwd=ROOT)
        except FileNotFoundError as error:
            raise ProbeError("docker is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise ProbeError(f"{service} did not answer within {self.timeout:.0f}s") from error
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip().splitlines()
            reason = message[-1] if message else "no output"
            raise ProbeError(f"{service}: exit {completed.returncode}: {reason}")
        return completed.stdout

    def psql(self, sql: str) -> str:
        """One read-only query against the stack's PostgreSQL, wherever it lives (`stores.psql_argv`)."""
        if self.store_error is not None:
            raise ProbeError(self.store_error)
        service = stores.STORE_CHECK_SERVICES["postgres"] if self.stores.postgres == stores.EXTERNAL else "postgres"
        return self._run(stores.psql_argv(self.argv(), sql, self.stores), service)

    def cypher(self, query: str) -> str:
        return self.exec("neo4j", ["sh", "-c", CYPHER_SHELL, "sh", query])


def probe_latch(compose: Compose) -> tuple[LatchRow, ...]:
    latch_present, job_present = parse_relations(compose.psql(RELATIONS_SQL))
    if not latch_present:
        raise ProbeError("public.loader_extraction_latch does not exist (schema-init not run, or a database-schema release before the latch)")
    sql = LATCH_SQL.format(
        job_state="job.state" if job_present else "NULL",
        job_error="job.last_error" if job_present else "NULL",
        job_join=JOB_JOIN if job_present else "",
    )
    return parse_latch(compose.psql(sql))


def probe_graph(compose: Compose) -> tuple[GraphCompletion, ...]:
    return parse_graph(compose.cypher(GRAPH_CYPHER))


def probe_queues(compose: Compose) -> tuple[QueueState, ...]:
    return parse_queues(compose.exec("rabbitmq", RABBITMQCTL))


def observe(compose: Compose) -> Observation:
    """Ask every store, recording each failure in place of its answer."""
    answers: dict[str, Any] = {}
    for key, probe in (("latch", probe_latch), ("graph", probe_graph), ("queues", probe_queues)):
        try:
            answers[key] = probe(compose)
        except ProbeError as error:
            answers[f"{key}_error"] = str(error)
    return Observation(**answers)


# --- output ----------------------------------------------------------------------------


def render(report: Report) -> str:
    """The full per-signal report."""
    lines = [f"Discogs import: {report.status.label.upper()} (exit {int(report.status)})"]
    lines.append(f"  {'extraction version'.ljust(18)}  {report.version or '-'}")
    for signal in report.signals:
        lines.append(f"  {signal.name.ljust(18)}  {signal.status.label.ljust(16)}  {signal.detail}")
    if report.queues:
        width = max(len(queue.name) for queue in report.queues)
        lines.append("")
        lines.append(f"  {'queue'.ljust(width)}  {'ready':>8}  {'unacked':>8}  {'consumers':>9}")
        lines.extend(f"  {queue.name.ljust(width)}  {queue.ready:>8}  {queue.unacked:>8}  {queue.consumers:>9}" for queue in report.queues)
    for warning in report.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def summary_line(report: Report, now: datetime) -> str:
    """One line per poll."""
    signals = "; ".join(f"{signal.name} {signal.summary}" for signal in report.signals)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} {report.status.label} [{report.version or '-'}]: {signals}"


def as_json(report: Report) -> str:
    return json.dumps(
        {
            "status": report.status.label,
            "exit_code": int(report.status),
            "version": report.version,
            "signals": [{"name": s.name, "status": s.status.label, "summary": s.summary, "detail": s.detail} for s in report.signals],
            "queues": [{"name": q.name, "ready": q.ready, "unacked": q.unacked, "consumers": q.consumers} for q in report.queues],
            "warnings": list(report.warnings),
        },
        indent=2,
    )


# --- command line ----------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discogs-import-status",
        description="Report whether the Discogs import has finished (read-only). Exit 0 complete, 3 in progress, 2 cannot determine.",
    )
    parser.add_argument("--prod", action="store_true", help="address the production stack: -f docker-compose.yml -f docker-compose.prod.yml")
    parser.add_argument("-f", "--file", action="append", default=[], dest="files", help="compose file (repeatable; appended after --prod's)")
    parser.add_argument("--env-file", help="compose --env-file")
    parser.add_argument("-p", "--project-name", help="compose --project-name")
    parser.add_argument("--json", action="store_true", help="print the final report as JSON")
    parser.add_argument("--wait", action="store_true", help="poll until complete, one summary line per poll")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="seconds between polls (default 30)")
    parser.add_argument(
        "--timeout", type=float, default=0.0, help="with --wait, give up after this many seconds and exit with the last status (default: never)"
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.timeout < 0:
        parser.error("--timeout must not be negative")
    return args


def store_settings(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[Any, str | None]:
    """(stores.Stores, None), or (local stores, why PostgreSQL cannot be addressed) from the env file and the shell."""
    env_values = stores.read_env(Path(args.env_file) if args.env_file else ROOT / ".env")
    try:
        configured = stores.read_stores(env_values, environ)
    except stores.StoreConfigError as error:
        return stores.Stores(), str(error)
    if configured.postgres == stores.EXTERNAL and not args.prod:
        return configured, "PostgreSQL is external (GROOVEMAP_POSTGRES=external); address the production stack with --prod"
    return configured, None


def compose_files(args: argparse.Namespace, configured: Any = None) -> list[str]:
    """`--prod` names the files relative to the repository root, where the probes run, and adds the external-store overlays."""
    files = [*PROD_COMPOSE_FILES, *(configured or stores.Stores()).overlays] if args.prod else []
    return files + [str(Path(path).resolve()) for path in args.files]


def watch(
    observe_once: Callable[[], Observation],
    interval: float,
    timeout: float,
    out: TextIO,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Report:
    """Poll until complete or until `timeout` (0 = never) would be exceeded."""
    started = clock()
    while True:
        report = evaluate(observe_once())
        print(summary_line(report, now()), file=out, flush=True)
        if report.status is Status.COMPLETE:
            return report
        if timeout and clock() - started + interval > timeout:
            print(f"gave up after {timeout:.0f}s without completing", file=out, flush=True)
            return report
        sleep(interval)


def main(
    argv: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    configured, store_error = store_settings(args, os.environ if environ is None else environ)
    compose = Compose(
        compose_files(args, configured), args.env_file, args.project_name, runner=runner, configured=configured, store_error=store_error
    )
    try:
        report = watch(lambda: observe(compose), args.interval, args.timeout, sys.stderr) if args.wait else evaluate(observe(compose))
    except KeyboardInterrupt:
        print("interrupted; nothing was changed, re-run to resume", file=sys.stderr)
        return 130
    print(as_json(report) if args.json else render(report))
    return int(report.status)


if __name__ == "__main__":
    sys.exit(main())
