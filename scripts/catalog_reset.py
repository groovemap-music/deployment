#!/usr/bin/env python3
"""Reset GrooveMap's imported catalog and re-ingest once: `just catalog-reset <stage>`.

The runbook is docs/catalog-reset.md. This tool is its only executor, so the runbook's list of
destructive commands and what runs can be compared line by line (tests/deploy/test_catalog_reset.py
does that). It runs from the deployment checkout, where `.env` selects the Compose files.

Without --apply every stage prints what it would do and changes nothing. Nothing here runs on its
own, and CI never runs it against a live environment.

What keeps the shared PostgreSQL server safe:

- Every SQL statement is a literal in this file. No database name is ever formatted into SQL or
  taken from an argument, the environment or a Compose render.
- `psql` can only be pointed at `postgres` (the maintenance database, for the database-level DDL)
  or `groovemap` (`Stores.psql` refuses anything else).
- The DDL's targets are exactly groovemap, groovemap_pre_reset and groovemap_reset_failed.
- There is no FORCE, no pg_terminate_backend: a session still on `groovemap` stops the rename.
- Before and after each destructive action, every other database and role and PostgreSQL's start
  time are compared with the capture. That detects a change and stops the run; it cannot undo one.

Progress is recorded in the state directory as each stage completes. Rollback reads those records,
so it restores the graph only from a copy this reset verified, and a rollback that stops part way
can simply be run again.

Standard library only (python3 >= 3.10). It never prints a secret, and it never writes a Compose
render to disk (a render holds every secret in clear text). The state directory holds password
hashes: 0700, outside the checkout, removed by the operator after `finish`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc  # noqa: UP017 - datetime.UTC needs 3.11; operators run stock python3 3.10

# --- targets. Everything destructive below names these literals. ---------------------------

DATABASE = "groovemap"
RETAINED_DATABASE = "groovemap_pre_reset"
FAILED_DATABASE = "groovemap_reset_failed"
MAINTENANCE_DATABASE = "postgres"
OWN_DATABASES = (DATABASE, RETAINED_DATABASE, FAILED_DATABASE)
RETAINED_VOLUME_SUFFIX = "_pre_reset"
RETAINED_VOLUME_LABEL = "groovemap.catalog-reset=retained"
UTILITY_CONTAINER = "groovemap-catalog-reset-copy"

# Lux's shared server: `docker exec postgres psql -U datum`. Container and role only, never a database.
DEFAULT_ADMIN_CONTAINER = "postgres"
DEFAULT_ADMIN_USER = "datum"

# A pre-flight older than this says little about the stack it checked.
PREFLIGHT_MAX_AGE_SECONDS = 6 * 3600
EVIDENCE_LOG_LINES = "20000"

# The nightly pg-backup and groovemap-ops dumps run in 08:00-10:00 UTC. The margin keeps a stage
# from starting just before the window and running into it.
BLOCKED_FROM_MINUTE = 7 * 60 + 30
BLOCKED_UNTIL_MINUTE = 10 * 60

# In the order they stop. The extractor first, so nothing new is published; then the consumers;
# then everything else holding a PostgreSQL session. `insights` stays down until the gate exits 0,
# because its rarity transaction blocks vacuum for the whole database (runbook, step 14).
STOP_ORDER = (
    ("extractor-discogs", "extractor-musicbrainz"),
    ("tableinator", "brainztableinator", "graphinator", "brainzgraphinator"),
    ("insights", "embeddings"),
    ("api", "dashboard", "explore"),
)
CONSUMERS = ("tableinator", "graphinator")
APPLICATIONS = ("api", "dashboard", "explore")
EXTRACTOR = "extractor-discogs"
ANALYTICS = "insights"

SYSTEM_ACTOR_ID = "6d3c1f0a-5e1b-4c7a-9a4f-1d0e1d3a7a11"
# catalog-api's watcher reads `identity.reattach.auto` with target discogs:<version> as "handled".
# Restoring it would stop the watcher re-attaching and projecting the re-ingest of that same version.
AUDIT_HANDLED_MARKER_PREFIX = "identity.reattach.auto"

# What the reset keeps. Every user-owned table not listed here must be empty, or preflight stops:
# user rows reference catalog ids, and the reset re-mints those ids.
KEEP_USERS_SQL = "COPY (SELECT * FROM public.users ORDER BY created_at, id) TO STDOUT WITH (FORMAT csv, HEADER true)"
KEEP_AUDIT_SQL = (
    "COPY (SELECT * FROM public.admin_audit_log WHERE action NOT LIKE 'identity.reattach.auto%' ORDER BY created_at, id) "
    "TO STDOUT WITH (FORMAT csv, HEADER true)"
)
MUST_BE_EMPTY = (
    "public.app_config",
    "public.artifacts",
    "public.catalog_item_supersessions",
    "public.identity_link_events",
    "activity.erasures",
    "activity.event_idempotency",
    "activity.events",
    "activity.impressions",
)
# Together with every table with a foreign key to public.users, read from the catalog at preflight so
# a new table cannot be missed. These two reference users and are handled: the first is restored, the
# second is the history of admin-triggered runs, which ends with the old catalog.
REBUILT_USER_CHILDREN = frozenset({"public.admin_audit_log", "public.extraction_history"})
# Rebuilt by the re-ingest, so the reset must leave them empty.
REBUILT_TABLES = (
    "public.artists",
    "public.labels",
    "public.masters",
    "public.releases",
    "public.catalog_items",
    "public.provider_aliases",
    "public.loader_extraction_latch",
    "graph.person",
    "insights.computation_log",
)

# Deny-direction session state: it must outlive the flush. Everything else in the dedicated Valkey is
# a cache or a short-lived token, and caches hold catalog ids this reset re-mints.
VALKEY_KEEP = re.compile(r"^(groovemap:)?(revoked:jti:|password_changed:|legacy-keys:)")
QUEUE = re.compile(r"^groovemap-(discogs|musicbrainz)-[a-z-]+(\.dlq)?$")

# --- literal SQL. The review surface: tests enumerate these. --------------------------------

SQL_OTHER_DATABASES = (
    "SELECT datname, oid, pg_get_userbyid(datdba), encoding, datcollate, datctype, datallowconn, datconnlimit FROM pg_database "
    "WHERE datname NOT IN ('groovemap', 'groovemap_pre_reset', 'groovemap_reset_failed') ORDER BY datname"
)
SQL_ROLES = "SELECT rolname, oid, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolconnlimit FROM pg_roles ORDER BY rolname"
SQL_POSTMASTER_START = "SELECT pg_postmaster_start_time()"
SQL_IS_SUPERUSER = "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
SQL_OWN_DATABASES = (
    "SELECT datname, pg_get_userbyid(datdba), datconnlimit, datallowconn, coalesce(datacl::text, '') FROM pg_database "
    "WHERE datname IN ('groovemap', 'groovemap_pre_reset', 'groovemap_reset_failed') ORDER BY datname"
)
SQL_SESSIONS = "SELECT count(*) FROM pg_stat_activity WHERE datname = 'groovemap' AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
SQL_SESSION_DETAIL = (
    "SELECT usename, application_name, state, count(*) FROM pg_stat_activity "
    "WHERE datname = 'groovemap' AND backend_type = 'client backend' AND pid <> pg_backend_pid() GROUP BY 1, 2, 3 ORDER BY 4 DESC"
)
SQL_DUMPS = (
    "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
    "AND (query ILIKE '%pg_dump%' OR application_name ILIKE '%dump%' OR application_name ILIKE '%backup%')"
)
SQL_DATABASE_BYTES = "SELECT pg_database_size('groovemap')"
SQL_USER_CHILD_TABLES = "SELECT DISTINCT conrelid::regclass::text FROM pg_constraint WHERE contype = 'f' AND confrelid = 'public.users'::regclass"
SQL_CATALOG_ROWS = (
    "SELECT n.nspname || '.' || c.relname, c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND n.nspname IN ('public', 'graph') AND c.reltuples > 0 ORDER BY 1"
)

# One session: CREATE DATABASE cannot run in a transaction. A rename fails, and so stops everything
# after it, while any session is on `groovemap` or if a retained database already exists.
SQL_SWAP = """\
ALTER DATABASE groovemap RENAME TO groovemap_pre_reset;
ALTER DATABASE groovemap_pre_reset WITH ALLOW_CONNECTIONS false;
CREATE DATABASE groovemap OWNER groovemap TEMPLATE template0 ENCODING 'UTF8' LOCALE 'en_US.UTF-8' CONNECTION LIMIT 60;
REVOKE ALL ON DATABASE groovemap FROM PUBLIC;
"""
# In the new `groovemap`: what docs/homelab.md's provisioning does inside the database.
SQL_PROVISION = """\
CREATE EXTENSION pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO groovemap;
"""
# Rollback in two parts: a reset that failed between the rename and CREATE DATABASE has no new
# `groovemap` to set aside.
SQL_ROLLBACK_SET_ASIDE = """\
ALTER DATABASE groovemap RENAME TO groovemap_reset_failed;
ALTER DATABASE groovemap_reset_failed WITH ALLOW_CONNECTIONS false;
"""
SQL_ROLLBACK_RESTORE = """\
ALTER DATABASE groovemap_pre_reset WITH ALLOW_CONNECTIONS true;
ALTER DATABASE groovemap_pre_reset RENAME TO groovemap;
"""
SQL_FINISH = """\
DROP DATABASE IF EXISTS groovemap_pre_reset;
DROP DATABASE IF EXISTS groovemap_reset_failed;
"""
SQL_MARKER_COUNT = "SELECT count(*) FROM public.admin_audit_log WHERE action LIKE 'identity.reattach.auto%'"
SQL_LOAD_COMPLETE = (
    "SELECT count(*) FROM public.loader_extraction_latch WHERE loader = 'discogs' "
    "AND signals @> ARRAY['artists', 'labels', 'masters', 'releases']::text[] AND refreshed_at IS NOT NULL"
)

CYPHER_NODES = "MATCH (n) RETURN count(n) AS nodes"
CYPHER_COMPLETE = "MATCH (c:ExtractionCompletion) WHERE size(c.signals) = 4 RETURN count(c) AS complete"
CYPHER_SHELL = 'cypher-shell -u neo4j -p "$(cat /run/secrets/neo4j_password)" --format plain "$1"'
VALKEY_AUTH = 'VALKEYCLI_AUTH="$(cat /run/secrets/valkey_password 2>/dev/null)"; export VALKEYCLI_AUTH; '


# Emptied first, so a re-run after an interrupted copy leaves no stale files. `&&`: never copy into a
# volume that could not be emptied.
COPY_VOLUME = "find /to -mindepth 1 -delete && cp -a /from/. /to/"
TALLY_VOLUME = "find /v -type f -printf '%s\\n' | awk '{n++; s+=$1} END {print n+0, s+0}'"

# Each service, the image variable that pins it, and the floor the reset needs: (version, inclusive).
# An exclusive floor is the pin that lacks the fix, because the release that has it is not out yet.
SERVICE_IMAGES = {
    "schema-init": "DATABASE_SCHEMA_IMAGE",
    "api": "CATALOG_API_IMAGE",
    "tableinator": "DISCOGS_SQL_LOADER_IMAGE",
    "graphinator": "DISCOGS_GRAPH_ENRICHER_IMAGE",
    "extractor-discogs": "DISCOGS_INGESTION_IMAGE",
    "insights": "ANALYTICS_ENGINE_IMAGE",
}
RELEASE_FLOOR = {
    "DATABASE_SCHEMA_IMAGE": ("0.7.0", True),
    "DISCOGS_SQL_LOADER_IMAGE": ("0.4.4", False),
    "DISCOGS_GRAPH_ENRICHER_IMAGE": ("0.6.0", False),
    "CATALOG_API_IMAGE": ("0.6.0", False),
    "ANALYTICS_ENGINE_IMAGE": ("0.5.0", False),
}


class ResetError(RuntimeError):
    """A check failed or a command failed: stop here, nothing further runs."""


def blocked_window(now: dt.datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return BLOCKED_FROM_MINUTE <= minute < BLOCKED_UNTIL_MINUTE


def split_keep(keys: list[str]) -> tuple[list[str], list[str]]:
    """(kept, removed) for the Valkey key names in `keys`."""
    kept = [key for key in keys if VALKEY_KEEP.match(key)]
    removed = [key for key in keys if not VALKEY_KEEP.match(key)]
    return kept, removed


def reset_queues(names: list[str]) -> tuple[list[str], list[str]]:
    """(purged, left alone): only GrooveMap's own catalog queues and their dead-letter queues are purged."""
    return [name for name in names if QUEUE.match(name)], [name for name in names if not QUEUE.match(name)]


def copy_restore_sql(table: str, csv_text: str) -> str:
    """A COPY of `csv_text` into `table`, using the capture's own header as the column list.

    `table` is one of two literals in this file. A release that drops a captured column fails the
    COPY, and the restore stops rather than losing that column silently.
    """
    if table not in ("public.users", "public.admin_audit_log"):
        raise ResetError(f"refusing to restore into {table}")
    header = csv_text.split("\n", 1)[0].strip()
    columns = header.split(",")
    if not all(re.fullmatch(r"[a-z_][a-z0-9_]*", column) for column in columns):
        raise ResetError(f"unexpected column list in the capture of {table}")
    body = csv_text if csv_text.endswith("\n") else csv_text + "\n"
    return f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, HEADER true);\n{body}\\.\n"


def valkey_unlink(keys: list[str]) -> str:
    """One UNLINK with each name quoted the way valkey-cli reads it. `\\xHH` covers every non-ASCII byte."""
    quoted = []
    for key in keys:
        body = "".join(chr(byte) if 32 <= byte < 127 and chr(byte) not in '"\\' else f"\\x{byte:02x}" for byte in key.encode())
        quoted.append(f'"{body}"')
    return "UNLINK " + " ".join(quoted) + "\n"


def parse_rows(text: str) -> list[list[str]]:
    return [line.split("|") for line in text.splitlines() if line]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Stores:
    """Every external command, each with a deadline. A timeout is a failure."""

    apply: bool
    admin_container: str
    admin_user: str

    def run(self, argv: list[str], *, timeout: int = 120, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(argv, cwd=ROOT, input=stdin, text=True, capture_output=True, timeout=timeout, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise ResetError(f"timed out after {timeout}s: {shlex.join(argv[:6])} ...") from error
        if check and result.returncode != 0:
            raise ResetError(f"exit {result.returncode}: {shlex.join(argv[:8])}\n{(result.stderr or result.stdout).strip()[-1500:]}")
        return result

    def mutate(self, argv: list[str], *, timeout: int = 120, stdin: str | None = None, show: str | None = None) -> str:
        print(f"{'+' if self.apply else 'would run:'} {show or shlex.join(argv)}", flush=True)
        if not self.apply:
            return ""
        return self.run(argv, timeout=timeout, stdin=stdin).stdout

    def psql_argv(self, database: str, *flags: str) -> list[str]:
        if database not in (MAINTENANCE_DATABASE, DATABASE):
            raise ResetError(f"psql may connect only to {MAINTENANCE_DATABASE} or {DATABASE}, not {database}")
        return [
            *("docker", "exec", "-i", "-e", "PGAPPNAME=groovemap-catalog-reset", self.admin_container),
            *("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", self.admin_user, "-d", database, *flags),
        ]

    def query(self, database: str, sql: str, *, timeout: int = 120) -> str:
        return self.run(self.psql_argv(database, "-At", "-c", sql), timeout=timeout).stdout

    def scalar(self, database: str, sql: str) -> str:
        return self.query(database, sql).strip()

    def psql_script(self, database: str, sql: str, *, timeout: int = 300, show: str | None = None) -> None:
        self.mutate(self.psql_argv(database, "-f", "-"), timeout=timeout, stdin=sql, show=show or f"psql -d {database} <<SQL\n{sql}SQL")

    def compose(self, *args: str) -> list[str]:
        return ["docker", "compose", *args]

    def render(self) -> dict[str, Any]:
        """Compose's render, in memory only. It holds every secret: never print or store it."""
        render: dict[str, Any] = json.loads(self.run(self.compose("config", "--format", "json")).stdout)
        return render

    def docker_exec(self, service: str, script: str, *args: str, timeout: int = 120, stdin: str | None = None) -> str:
        argv = self.compose("exec", "-T", service, "sh", "-c", script, "sh", *args)
        return self.run(argv, timeout=timeout, stdin=stdin).stdout

    def rabbitmq_queues(self) -> dict[str, tuple[int, int, int]]:
        argv = self.compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "-q",
            "list_queues",
            "--no-table-headers",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "consumers",
        )
        queues = {}
        for line in self.run(argv, timeout=60).stdout.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[1].isdigit():
                queues[parts[0]] = (int(parts[1]), int(parts[2]), int(parts[3]))
        return queues

    def utility(
        self, script: str, *mounts: str, timeout: int, image: str, stdin: str | None = None, mutate: bool = False, show: str | None = None
    ) -> str:
        """A throwaway root shell in the stack's pinned utility image, with no network.

        Named when it writes: a timeout kills only the client, so the container is removed too, and a
        second one cannot start beside a copy still running.
        """
        argv = ["docker", "run", "--rm", "-i", "--network", "none", "--user", "0:0", "--entrypoint", "sh"]
        if mutate:
            argv += ["--name", UTILITY_CONTAINER]
        for mount in mounts:
            argv += ["-v", mount]
        argv += [image, "-c", script]
        if not mutate:
            return self.run(argv, timeout=timeout, stdin=stdin).stdout
        try:
            return self.mutate(argv, timeout=timeout, stdin=stdin, show=show)
        except ResetError:
            self.run(["docker", "rm", "--force", UTILITY_CONTAINER], check=False, timeout=60)
            raise


@dataclass
class Stack:
    project: str
    services: set[str]
    neo4j_volume: str
    discogs_volume: str
    utility_image: str
    valkey_external: bool

    @property
    def retained_volume(self) -> str:
        return self.neo4j_volume + RETAINED_VOLUME_SUFFIX


def load_stack(stores: Stores) -> Stack:
    render = stores.render()
    services = render.get("services", {})
    volumes = render.get("volumes", {})
    project = render.get("name", "")
    for key in ("neo4j_data", "discogs_data"):
        if key not in volumes:
            raise ResetError(f"Compose declares no {key} volume; run this from the deployment checkout")
    if "tableinator" not in services or "data-volume-init" not in services:
        raise ResetError("this is not GrooveMap's Compose project; run this from the deployment checkout")
    database = services["tableinator"].get("environment", {}).get("POSTGRES_DATABASE")
    if database != DATABASE:
        raise ResetError(f"the loader writes to database {database!r}, not {DATABASE!r}; this tool handles only {DATABASE}")
    neo4j_volume = volumes["neo4j_data"].get("name", f"{project}_neo4j_data")
    discogs_volume = volumes["discogs_data"].get("name", f"{project}_discogs_data")
    return Stack(project, set(services), neo4j_volume, discogs_volume, services["data-volume-init"]["image"], "valkey" not in services)


# --- checks ---------------------------------------------------------------------------------


def ok(message: str) -> None:
    print(f"ok    {message}", flush=True)


def fail(message: str) -> NoReturn:
    raise ResetError(message)


def require_window(now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(UTC)
    if blocked_window(now):
        fail(f"{now:%H:%M} UTC is inside the nightly dump window (07:30-10:00 UTC, with its margin); wait until 10:00 UTC")
    ok(f"{now:%H:%M} UTC is outside the nightly dump window")


def fingerprint(stores: Stores) -> dict[str, str]:
    return {
        "other_databases": stores.query(MAINTENANCE_DATABASE, SQL_OTHER_DATABASES),
        "roles": stores.query(MAINTENANCE_DATABASE, SQL_ROLES),
        "postmaster_start": stores.scalar(MAINTENANCE_DATABASE, SQL_POSTMASTER_START),
    }


def compare_fingerprint(before: dict[str, str], now: dict[str, str]) -> tuple[list[str], list[str]]:
    """(changed, added). A row from the capture that changed or went is a stop; a neighbour's new database is not."""
    changed: list[str] = []
    added: list[str] = []
    for key in ("other_databases", "roles"):
        old = set(before[key].splitlines())
        new = set(now[key].splitlines())
        changed += [f"{key}: {row.split('|')[0]}" for row in sorted(old - new)]
        added += [f"{key}: {row.split('|')[0]}" for row in sorted(new - old)]
    if before["postmaster_start"] != now["postmaster_start"]:
        changed.append("postmaster_start")
    return changed, added


def require_untouched(stores: Stores, state: dict[str, Any], *, accept: bool = False) -> None:
    """Stops on a change unless `accept`: the operator has looked and passed --accept-shared-change, or this is a rollback."""
    changed, added = compare_fingerprint(state["fingerprint"], fingerprint(stores))
    if changed and not accept:
        fail(f"SHARED SERVER CHANGED since the capture: {', '.join(changed)}. Look, then pass --accept-shared-change if it is not this run's doing")
    if changed:
        print(f"WARNING: the shared server changed since the capture: {', '.join(changed)}")
    if added:
        print(f"note  new on the server since the capture, and not ours: {', '.join(added)}")
    if not changed:
        ok(f"all {len(parse_rows(state['fingerprint']['other_databases']))} other databases, every role and the server start time match the capture")


def require_no_sessions(stores: Stores) -> None:
    count = int(stores.scalar(MAINTENANCE_DATABASE, SQL_SESSIONS))
    if count:
        detail = stores.query(MAINTENANCE_DATABASE, SQL_SESSION_DETAIL).strip()
        fail(f"{count} session(s) still on {DATABASE}; stop the client (nothing here terminates a session):\n{detail}")
    ok(f"no session on {DATABASE}")


def own_databases(stores: Stores) -> dict[str, list[str]]:
    return {row[0]: row for row in parse_rows(stores.query(MAINTENANCE_DATABASE, SQL_OWN_DATABASES))}


def state_dir(path: str | None) -> Path:
    if not path:
        fail("--state-dir is required")
    directory = Path(str(path)).resolve()
    if directory == ROOT or ROOT in directory.parents:
        fail(f"--state-dir must be outside the checkout (it will hold password hashes): {directory}")
    if not directory.is_dir():
        fail(f"--state-dir does not exist: {directory}")
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        fail(f"--state-dir must be mode 0700: {directory}")
    return directory


def load_state(directory: Path) -> dict[str, Any]:
    path = directory / "capture.json"
    if not path.is_file():
        fail(f"no capture in {directory}: run the capture stage first")
    state: dict[str, Any] = json.loads(path.read_text())
    return state


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def done(directory: Path, step: str) -> bool:
    return (directory / f"done-{step}").exists()


def record(stores: Stores, directory: Path, step: str, value: str = "") -> None:
    """Durable progress: rollback, re-runs and later stages read these instead of guessing."""
    if stores.apply:
        write_private(directory / f"done-{step}", value)


def require_steps(directory: Path, *steps: str) -> None:
    missing = [step for step in steps if not done(directory, step)]
    if missing:
        fail(f"these stages have not completed: {', '.join(missing)}")


def load_preflight(directory: Path) -> dict[str, Any]:
    path = directory / "done-preflight"
    if not path.is_file():
        fail(f"no pre-flight in {directory}: run the preflight stage with this --state-dir first")
    record_: dict[str, Any] = json.loads(path.read_text())
    return record_


def require_fresh_preflight(directory: Path) -> None:
    load_preflight(directory)
    age = time.time() - (directory / "done-preflight").stat().st_mtime
    if age > PREFLIGHT_MAX_AGE_SECONDS:
        fail(f"the pre-flight is {age / 3600:.0f} hours old; run it again")


def running_images(stores: Stores) -> dict[str, str]:
    """What each service runs before anything stops: rollback's only record of the old pins."""
    images = {}
    for service in SERVICE_IMAGES:
        containers = stores.run(stores.compose("ps", "--all", "--quiet", service), timeout=60).stdout.split()
        if containers:
            images[service] = stores.run(["docker", "inspect", "--format", "{{.Config.Image}}", containers[0]], timeout=60).stdout.strip()
    return images


def require_old_pins(stores: Stores, directory: Path) -> None:
    """A new image on the old schema is the failure the release order exists to prevent."""
    render = stores.render()["services"]
    for service, image in load_preflight(directory)["images"].items():
        pinned = render.get(service, {}).get("image")
        if pinned != image:
            fail(f"{service} is pinned to {pinned}, but ran {image} before the reset. Put the old *_IMAGE lines back in .env first")
    ok("the image pins are the ones the stack ran before the reset")


def save_evidence(stores: Stores, directory: Path, name: str) -> None:
    """Removing a container loses its logs and OOM state, and those are the leak proof's evidence."""
    if not stores.apply:
        return
    logs = stores.run(stores.compose("logs", "--no-color", "--tail", EVIDENCE_LOG_LINES, *CONSUMERS), check=False, timeout=300)
    write_private(directory / f"evidence-{name}-logs.txt", logs.stdout)
    states = []
    for service in CONSUMERS:
        states.append(
            stores.run(
                ["docker", "inspect", "--format", "{{.Name}} restarts={{.RestartCount}} {{json .State}}", f"groovemap-{service}"],
                check=False,
                timeout=60,
            ).stdout
        )
    write_private(directory / f"evidence-{name}-state.txt", "".join(states))


def volume_exists(stores: Stores, name: str) -> bool:
    return stores.run(["docker", "volume", "inspect", name], check=False).returncode == 0


def volume_listing(stores: Stores, stack: Stack) -> list[str]:
    return stores.utility("ls -A /v", f"{stack.discogs_volume}:/v:ro", timeout=60, image=stack.utility_image).split()


def read_volume_file(stores: Stores, stack: Stack, name: str) -> str:
    if not re.fullmatch(r"\.[a-z_]+(_\d{8})?\.json", name):
        fail(f"unexpected state file name {name}")
    return stores.utility(f"cat /v/{name}", f"{stack.discogs_volume}:/v:ro", timeout=60, image=stack.utility_image)


def markers(listing: list[str]) -> list[str]:
    return sorted(name for name in listing if re.fullmatch(r"\.extraction_status_\d{8}\.json", name))


def newest_marker(listing: list[str]) -> str:
    found = markers(listing)
    if not found:
        fail("no .extraction_status_<version>.json in the Discogs data volume")
    return found[-1]


def require_dump(listing: list[str], metadata: dict[str, Any], version: str) -> None:
    for entity in ("artists", "labels", "masters", "releases"):
        name = f"discogs_{version}_{entity}.xml.gz"
        if name not in listing:
            fail(f"{name} is not in the Discogs data volume; a forced run would download it")
        if name not in metadata:
            fail(f"{name} has no entry in .discogs_metadata.json; a forced run would download it again")
    ok(f"dump {version}: all four files on the volume with checksums in .discogs_metadata.json, so a forced run does not download them")


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def below_floor(versions: dict[str, str]) -> list[str]:
    """The pins that do not meet RELEASE_FLOOR. Inclusive floors accept the version itself."""
    problems = []
    for variable, (floor, inclusive) in RELEASE_FLOOR.items():
        version = versions.get(variable, "0")
        if version_tuple(version) < version_tuple(floor) or (not inclusive and version_tuple(version) == version_tuple(floor)):
            problems.append(f"{variable} is {version}, needs {'at least' if inclusive else 'newer than'} {floor}")
    return problems


def reviewed_images() -> tuple[dict[str, str], dict[str, str]]:
    module = runpy.run_path(str(ROOT / "scripts" / "check-images.py"))
    return dict(module["RELEASED_IMAGE_DIGESTS"]), dict(module["RELEASED_IMAGE_VERSIONS"])


def require_release_set(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    render = stores.render()["services"]
    digests, versions = reviewed_images()
    for service, variable in SERVICE_IMAGES.items():
        image = str(render.get(service, {}).get("image", ""))
        if not image.endswith(f"@sha256:{digests[variable]}"):
            fail(f"{service} is not pinned to the reviewed {variable}; run env-init (Before the day)")
    ok(f"{len(SERVICE_IMAGES)} services are pinned to the reviewed digests")
    if args.rehearsal_skip_release_floor:
        if stack.project == "groovemap":
            fail("--rehearsal-skip-release-floor is for a disposable stack, and this is the groovemap project")
        print("note  the release floor and the memory cap are NOT checked: rehearsal only")
        return
    problems = below_floor(versions)
    if problems:
        fail("the promoted release set is not ready: " + "; ".join(problems))
    limit = render["tableinator"].get("mem_limit") or render["tableinator"].get("deploy", {}).get("resources", {}).get("limits", {}).get("memory")
    if not limit or str(limit) == "0":
        fail("tableinator has no memory limit in this render; the leak proof must run under the cap (gm-deployment-3qs7)")
    ok(f"the release floor is met and tableinator is capped at {limit}")


def require_empty_user_tables(stores: Stores) -> None:
    children = [line for line in stores.query(DATABASE, SQL_USER_CHILD_TABLES).splitlines() if line]
    tables = sorted({*MUST_BE_EMPTY, *(name if "." in name else f"public.{name}" for name in children)} - REBUILT_USER_CHILDREN)
    for table in tables:
        if not re.fullmatch(r"[a-z_]+\.[a-z_0-9]+", table):
            fail(f"unexpected table name {table}")
        count = int(stores.scalar(DATABASE, f"SELECT count(*) FROM {table}"))  # noqa: S608 - names from the catalog, checked above
        if count:
            fail(f"{table} holds {count} row(s). This tool does not carry them across the reset; extend the runbook first")
    ok(f"{len(tables)} user-owned and identity tables are empty, so no kept row references a catalog id")


# --- stages ---------------------------------------------------------------------------------


def stage_preflight(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    """Read-only, with the stack still running. Its record is what lets `stop` go ahead."""
    directory = state_dir(args.state_dir)
    require_window()
    if "store-check-postgres" not in stack.services:
        fail("Compose has no store-check-postgres: .env does not select the external-PostgreSQL file set")
    if stores.scalar(MAINTENANCE_DATABASE, SQL_IS_SUPERUSER) != "t":
        fail(f"{stores.admin_user} is not a superuser on the server in container {stores.admin_container}")
    ok(f"admin session as {stores.admin_user} in container {stores.admin_container}")
    databases = own_databases(stores)
    if set(databases) != {DATABASE}:
        fail(f"expected only {DATABASE} of {OWN_DATABASES}, found {sorted(databases)}; a previous reset has not finished")
    row = databases[DATABASE]
    if row[1] != DATABASE:
        fail(f"{DATABASE} is owned by {row[1]}, not {DATABASE}")
    ok(f"{DATABASE} is owned by {row[1]}; no retained database exists")
    if int(stores.scalar(MAINTENANCE_DATABASE, SQL_SESSIONS)) < 1:
        fail(f"no session on {DATABASE} here with the stack up: {stores.admin_container} may not be the server this stack uses")
    ok("this server is the one the running stack is connected to")
    others = parse_rows(stores.query(MAINTENANCE_DATABASE, SQL_OTHER_DATABASES))
    ok(f"{len(others)} other databases on this server, none of which any statement here names: {', '.join(row[0] for row in others)}")
    dumps = int(stores.scalar(MAINTENANCE_DATABASE, SQL_DUMPS))
    if dumps:
        fail(f"{dumps} dump session(s) running; wait for them")
    ok("no dump running")
    require_release_set(stores, stack, args)
    users = int(stores.scalar(DATABASE, "SELECT count(*) FROM public.users"))
    if users < 1:
        fail("no rows in public.users; nothing to keep is suspicious on this deployment")
    ok(f"{users} rows in public.users to keep")
    require_empty_user_tables(stores)
    listing = volume_listing(stores, stack)
    marker = newest_marker(listing)
    version = marker.removeprefix(".extraction_status_").removesuffix(".json")
    metadata = json.loads(read_volume_file(stores, stack, ".discogs_metadata.json"))
    require_dump(listing, metadata, version)
    free_script = "df -Pk /v | awk 'NR==2 {print $4}'"
    free_kb = int(stores.utility(free_script, f"{stack.neo4j_volume}:/v:ro", timeout=60, image=stack.utility_image).strip())
    free_gb = free_kb // (1024 * 1024)
    if free_gb < args.min_free_gb:
        fail(f"{free_gb} GB free on the Docker volume disk, below the {args.min_free_gb} GB the runbook's estimate needs")
    ok(f"{free_gb} GB free on the Docker volume disk (at least {args.min_free_gb} GB needed)")
    if volume_exists(stores, stack.retained_volume):
        fail(f"{stack.retained_volume} already exists; a previous reset has not finished")
    ok(f"targets: database {DATABASE}, volume {stack.neo4j_volume}, Compose project {stack.project}")
    write_private(directory / "done-preflight", json.dumps({"images": running_images(stores)}, indent=2, sort_keys=True) + "\n")


def stop_writers(stores: Stores, stack: Stack, directory: Path, evidence: str) -> None:
    """Stopped, then removed: `restart: always` brings a stopped container back when the daemon restarts."""
    groups = [[service for service in group if service in stack.services] for group in STOP_ORDER]
    for names in groups:
        if names:
            stores.mutate(stores.compose("stop", "--timeout", "120", *names), timeout=600)
    save_evidence(stores, directory, evidence)
    for names in groups:
        if names:
            stores.mutate(stores.compose("rm", "--force", *names), timeout=120)


def stage_stop(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    require_window()
    require_fresh_preflight(directory)
    stop_writers(stores, stack, directory, "stop")
    if stores.apply:
        require_no_sessions(stores)


def stage_capture(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    """After `stop`, so nothing can change between the copy and the reset."""
    directory = state_dir(args.state_dir)
    if not stores.apply:
        print(f"would capture users, audit rows, the extractor's markers and the shared-server fingerprint into {directory}")
        return
    if (directory / "capture.json").exists():
        fail(f"{directory} already holds a capture. A second one could replace it with state from after the reset")
    if RETAINED_DATABASE in own_databases(stores):
        fail(f"{RETAINED_DATABASE} exists: the reset has started, so {DATABASE} no longer holds what must be captured")
    require_no_sessions(stores)
    users = stores.query(DATABASE, KEEP_USERS_SQL)
    audit = stores.query(DATABASE, KEEP_AUDIT_SQL)
    user_rows = max(0, len(users.splitlines()) - 1)
    if user_rows < 1:
        fail("public.users is empty; refusing to capture nothing")
    write_private(directory / "users.csv", users)
    write_private(directory / "admin_audit_log.csv", audit)
    listing = volume_listing(stores, stack)
    for name in markers(listing):
        write_private(directory / name.lstrip("."), read_volume_file(stores, stack, name))
    metadata = read_volume_file(stores, stack, ".discogs_metadata.json")
    nodes = stores.docker_exec("neo4j", CYPHER_SHELL, CYPHER_NODES).split()
    state = {
        "captured_at": dt.datetime.now(UTC).isoformat(),
        "catalog_rows": stores.query(DATABASE, SQL_CATALOG_ROWS),
        "database_bytes": int(stores.scalar(MAINTENANCE_DATABASE, SQL_DATABASE_BYTES)),
        "datacl": own_databases(stores)[DATABASE][4],
        "extraction_marker": newest_marker(listing),
        "extraction_markers": markers(listing),
        "fingerprint": fingerprint(stores),
        "handled_markers_excluded": int(stores.scalar(DATABASE, SQL_MARKER_COUNT)),
        "keep_audit_rows": max(0, len(audit.splitlines()) - 1),
        "keep_user_rows": user_rows,
        "metadata_sha256": sha256_text(metadata),
        "neo4j_nodes": nodes[-1] if nodes else "",
    }
    write_private(directory / "capture.json", json.dumps(state, indent=2, sort_keys=True) + "\n")
    ok(f"captured {user_rows} users and {state['keep_audit_rows']} audit rows; left out {state['handled_markers_excluded']} handled marker(s)")
    ok(f"{directory}: treat it as a secret, it holds password hashes")


def stage_reset_postgres(stores: Stores, _stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    if state["keep_user_rows"] < 1:
        fail("the capture holds no users; refusing to reset")
    if done(directory, "reset-postgres"):
        fail(f"{directory} belongs to a reset that already ran; use a new --state-dir for a new reset")
    if RETAINED_DATABASE in own_databases(stores):
        fail(f"{RETAINED_DATABASE} already exists; a previous reset has not finished")
    require_no_sessions(stores)
    require_untouched(stores, state, accept=args.accept_shared_change)
    stores.psql_script(MAINTENANCE_DATABASE, SQL_SWAP)
    stores.psql_script(DATABASE, SQL_PROVISION)
    if not stores.apply:
        return
    after = own_databases(stores)
    if after[DATABASE][1] != DATABASE or after[DATABASE][2] != "60" or after[DATABASE][4] != state["datacl"]:
        fail(f"the new {DATABASE} does not match the old one's owner, limit or access list: {after[DATABASE][1:]}, expected {state['datacl']}")
    if after[RETAINED_DATABASE][3] != "f":
        fail(f"{RETAINED_DATABASE} still accepts connections")
    ok(f"{DATABASE} is new and empty, with the same owner, connection limit and access list; {RETAINED_DATABASE} is closed to connections")
    record(stores, directory, "reset-postgres")
    require_untouched(stores, state, accept=args.accept_shared_change)


def stage_reset_neo4j(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    require_steps(directory, "reset-postgres")
    require_untouched(stores, state, accept=args.accept_shared_change)
    if done(directory, "reset-neo4j"):
        ok(f"{stack.neo4j_volume} was already removed by this reset; nothing to do")
        return
    image = stack.utility_image
    if not done(directory, "neo4j-copied"):
        if not volume_exists(stores, stack.neo4j_volume):
            fail(f"{stack.neo4j_volume} does not exist and no verified copy was recorded. Stop: do not let Docker create an empty one")
        stores.mutate(stores.compose("stop", "--timeout", "300", "neo4j"), timeout=600)
        stores.mutate(["docker", "volume", "create", "--label", RETAINED_VOLUME_LABEL, stack.retained_volume], timeout=60)
        # Emptied first, so a re-run after an interrupted copy cannot leave stale files in it.
        stores.utility(
            COPY_VOLUME,
            f"{stack.neo4j_volume}:/from:ro",
            f"{stack.retained_volume}:/to",
            timeout=7200,
            image=image,
            mutate=True,
            show=f"copy every file of {stack.neo4j_volume} to {stack.retained_volume}",
        )
        if stores.apply:
            source = volume_tally(stores, stack.neo4j_volume, image)
            copied = volume_tally(stores, stack.retained_volume, image)
            if source != copied or source.startswith("0 "):
                fail(f"the copy differs from {stack.neo4j_volume}: {copied} against {source}; the graph is still in place")
            ok(f"{stack.retained_volume} holds {source} (files, bytes), the same as {stack.neo4j_volume}")
            record(stores, directory, "neo4j-copied", source)
    # Only with a verified copy recorded above.
    stores.mutate(stores.compose("rm", "--force", "--stop", "neo4j"), timeout=120)
    if volume_exists(stores, stack.neo4j_volume):
        stores.mutate(["docker", "volume", "rm", stack.neo4j_volume], timeout=120)
    record(stores, directory, "reset-neo4j")
    if stores.apply:
        require_untouched(stores, state, accept=args.accept_shared_change)


def volume_tally(stores: Stores, volume: str, image: str) -> str:
    return stores.utility(TALLY_VOLUME, f"{volume}:/v:ro", timeout=1800, image=image).strip()


def stage_reset_rabbitmq(stores: Stores, _stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    require_untouched(stores, state, accept=args.accept_shared_change)
    purge_queues(stores)
    record(stores, directory, "reset-rabbitmq")


def purge_queues(stores: Stores) -> None:
    queues = stores.rabbitmq_queues()
    for name, (_ready, _unacked, consumers) in queues.items():
        if consumers and QUEUE.match(name):
            fail(f"{name} still has {consumers} consumer(s); stop the consumers first")
    purged, left = reset_queues(sorted(queues))
    print(f"{len(purged)} queues to purge, holding {sum(queues[name][0] + queues[name][1] for name in purged)} message(s)")
    for name in purged:
        stores.mutate(stores.compose("exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "purge_queue", name), timeout=120)
    if left:
        print(f"left alone, not GrooveMap catalog queues: {', '.join(left)}")


def stage_reset_valkey(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    require_untouched(stores, state, accept=args.accept_shared_change)
    clear_valkey(stores, stack)
    record(stores, directory, "reset-valkey")


def valkey_keys(stores: Stores) -> list[str]:
    return [key for key in stores.docker_exec("valkey", VALKEY_AUTH + "exec valkey-cli --no-auth-warning --scan", timeout=600).split("\n") if key]


def clear_valkey(stores: Stores, stack: Stack) -> None:
    if stack.valkey_external:
        fail("GROOVEMAP_VALKEY=external: a shared Valkey is out of scope; the runbook covers only a dedicated one")
    kept, removed = split_keep(valkey_keys(stores))
    print(f"Valkey: keeping {len(kept)} session key(s), removing {len(removed)} key(s)")
    for start in range(0, len(removed), 500):
        batch = removed[start : start + 500]
        print(f"{'+' if stores.apply else 'would run:'} UNLINK {len(batch)} key(s)", flush=True)
        if stores.apply:
            stores.docker_exec("valkey", VALKEY_AUTH + "exec valkey-cli --no-auth-warning", timeout=300, stdin=valkey_unlink(batch))
    if stores.apply:
        _kept, left = split_keep(valkey_keys(stores))
        if left:
            fail(f"{len(left)} key(s) survived UNLINK, for example {left[0]!r}")


def stage_bootstrap(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    if done(directory, "bootstrap"):
        ok("bootstrap already completed; not running schema-init again")
        return
    require_steps(directory, "reset-postgres", "reset-neo4j", "reset-rabbitmq", "reset-valkey")
    stores.mutate(stores.compose("up", "--detach", "--no-deps", "--wait", "--wait-timeout", "600", "neo4j"), timeout=660)
    # Without --no-deps: schema-init reaches the new database through its depends_on, store-check first.
    started = time.monotonic()
    stores.mutate(stores.compose("up", "--exit-code-from", "schema-init", "schema-init"), timeout=args.schema_timeout)
    ok(f"store-check-postgres and schema-init: {time.monotonic() - started:.0f} s")
    if not stores.apply:
        return
    users_restored = int(stores.scalar(DATABASE, "SELECT count(*) FROM public.users"))
    if users_restored == 0:
        users = copy_restore_sql("public.users", (directory / "users.csv").read_text())
        audit = copy_restore_sql("public.admin_audit_log", (directory / "admin_audit_log.csv").read_text())
        stores.mutate(
            stores.psql_argv(DATABASE, "--single-transaction", "-f", "-"),
            stdin=users + audit,
            show=f"restore {state['keep_user_rows']} users and {state['keep_audit_rows']} audit rows into {DATABASE}",
        )
    verify_bootstrap(stores, stack, state, accept=args.accept_shared_change)
    record(stores, directory, "bootstrap")


def verify_bootstrap(stores: Stores, stack: Stack, state: dict[str, Any], *, accept: bool = False) -> None:
    users = int(stores.scalar(DATABASE, "SELECT count(*) FROM public.users"))
    audit = int(stores.scalar(DATABASE, "SELECT count(*) FROM public.admin_audit_log"))
    if users != state["keep_user_rows"] or audit != state["keep_audit_rows"]:
        fail(f"restored {users} users and {audit} audit rows, expected {state['keep_user_rows']} and {state['keep_audit_rows']}")
    if int(stores.scalar(DATABASE, SQL_MARKER_COUNT)):
        fail("a handled marker reached the new database; the identity watcher would skip this re-ingest")
    for table in REBUILT_TABLES:
        if stores.scalar(DATABASE, f"SELECT EXISTS (SELECT 1 FROM {table})") != "f":  # noqa: S608 - literal constants above
            fail(f"{table} is not empty after the reset")
    ok(f"{users} users and {audit} audit rows restored; no handled marker; {len(REBUILT_TABLES)} catalog tables are empty")
    nodes = stores.docker_exec("neo4j", CYPHER_SHELL, CYPHER_NODES).split()
    if not nodes or nodes[-1] != "0":
        fail(f"Neo4j holds {nodes[-1] if nodes else 'an unknown number of'} nodes after the reset; it must be empty")
    queued = sum(ready + unacked for name, (ready, unacked, _consumers) in stores.rabbitmq_queues().items() if QUEUE.match(name))
    if queued:
        fail(f"{queued} message(s) are in the catalog queues after the reset")
    if sha256_text(read_volume_file(stores, stack, ".discogs_metadata.json")) != state["metadata_sha256"]:
        fail(".discogs_metadata.json changed since the capture; a forced run could download the dump again")
    ok("Neo4j and the catalog queues are empty, and .discogs_metadata.json is unchanged")
    require_untouched(stores, state, accept=accept)


def stage_start(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    require_window()
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_steps(directory, "bootstrap")
    if done(directory, "start"):
        fail("the stack was already started for this reset; a second `up` would run schema-init during the load")
    # One call without --no-deps, so depends_on holds every schema client behind schema-init, and no
    # client of the schema starts first. The extractor stays out of it: consumers must bind first.
    names = [service for service in APPLICATIONS if service in stack.services]
    stores.mutate(stores.compose("up", "--detach", "--wait", "--wait-timeout", "900", *CONSUMERS, *names), timeout=960)
    stores.mutate(stores.compose("up", "--detach", "--wait", "--wait-timeout", "600", EXTRACTOR), timeout=660)
    if stores.apply:
        if stores.run(stores.compose("ps", "--all", "--quiet", ANALYTICS)).stdout.strip():
            fail(f"a {ANALYTICS} container exists; remove it now, its rarity transaction must not overlap the load")
        record(stores, directory, "start")
        require_untouched(stores, state, accept=args.accept_shared_change)
    print(f"{ANALYTICS} stays stopped until the gate exits 0. Now trigger the forced run from the console (runbook, step 12).")


def stage_status(stores: Stores, _stack: Stack, args: argparse.Namespace) -> None:
    """Read-only samples of what the runbook watches, with a hard deadline."""
    deadline = time.monotonic() + args.duration
    previous: dict[str, tuple[int, int, int]] | None = None
    previous_at = 0.0
    while True:
        queues = stores.rabbitmq_queues()
        at = time.monotonic()
        stats = stores.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}} {{.MemUsage}} pids={{.PIDs}}",
                "groovemap-tableinator",
                "groovemap-graphinator",
                "groovemap-neo4j",
            ],
            check=False,
            timeout=60,
        ).stdout
        consumer = {name: value for name, value in queues.items() if QUEUE.match(name) and not name.endswith(".dlq")}
        dead = {name: value for name, value in queues.items() if name.endswith(".dlq")}
        depth = sum(value[0] + value[1] for value in consumer.values())
        unacked = sum(value[1] for value in consumer.values())
        rate = ""
        if previous is not None and at > previous_at:
            before = sum(value[0] + value[1] for name, value in previous.items() if name in consumer)
            rate = f" drain={(before - depth) / (at - previous_at):.0f}/s"
        sessions = stores.scalar(MAINTENANCE_DATABASE, SQL_SESSIONS)
        size = stores.scalar(MAINTENANCE_DATABASE, "SELECT pg_size_pretty(pg_database_size('groovemap'))")
        print(
            f"{dt.datetime.now(UTC):%H:%M:%S} queued={depth} unacked={unacked}{rate} consumers={sum(value[2] for value in consumer.values())} "
            f"dlq={sum(value[0] + value[1] for value in dead.values())} sessions={sessions} db={size}"
        )
        print(stats.rstrip(), flush=True)
        previous, previous_at = queues, at
        if time.monotonic() + args.interval >= deadline:
            return
        time.sleep(args.interval)


def require_verified_copy(stores: Stores, stack: Stack, directory: Path) -> None:
    """Before the first rename, so a bad copy stops with the reset's state whole."""
    if done(directory, "neo4j-copied") and stores.apply:
        recorded = (directory / "done-neo4j-copied").read_text()
        tally = volume_tally(stores, stack.retained_volume, stack.utility_image) if volume_exists(stores, stack.retained_volume) else "none"
        if tally != recorded:
            fail(f"{stack.retained_volume} is {tally}, not the verified copy ({recorded}). Nothing was changed. This is a restore, not a rollback")
    if not done(directory, "neo4j-copied") and not volume_exists(stores, stack.neo4j_volume):
        fail(f"no verified copy is recorded and {stack.neo4j_volume} is gone. Nothing was changed. This is a restore, not a rollback")


def restore_graph(stores: Stores, stack: Stack, directory: Path) -> None:
    """Only from a copy this reset verified. Without that record the live volume was never removed."""
    if not done(directory, "neo4j-copied"):
        print(f"no verified copy was recorded: {stack.neo4j_volume} was never removed, so it stays as it is")
        return
    recorded = (directory / "done-neo4j-copied").read_text()
    # Compose owns the volume's labels, so it creates the volume if the reset already removed it.
    stores.mutate(stores.compose("up", "--no-start", "--no-deps", "neo4j"), timeout=300)
    stores.utility(
        COPY_VOLUME,
        f"{stack.retained_volume}:/from:ro",
        f"{stack.neo4j_volume}:/to",
        timeout=7200,
        image=stack.utility_image,
        mutate=True,
        show=f"replace every file of {stack.neo4j_volume} with {stack.retained_volume}",
    )
    if stores.apply and volume_tally(stores, stack.neo4j_volume, stack.utility_image) != recorded:
        fail(f"{stack.neo4j_volume} does not match the verified copy after the restore. Run rollback again")


def stage_rollback(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    """Back to the retained stores, before `finish`. Safe to run again if it stops part way."""
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    if done(directory, "finish"):
        fail("`finish` has run: the retained copies are gone. This is a restore from the dump")
    if done(directory, "rollback"):
        ok("this reset is already rolled back")
        warn_newer_markers((directory / "done-rollback").read_text())
        return
    databases = own_databases(stores)
    if DATABASE not in databases and RETAINED_DATABASE not in databases:
        fail(f"neither {DATABASE} nor {RETAINED_DATABASE} exists. This is a restore from the dump")
    require_old_pins(stores, directory)
    require_verified_copy(stores, stack, directory)
    stop_writers(stores, stack, directory, "rollback")
    stores.mutate(stores.compose("stop", "--timeout", "300", "neo4j"), timeout=600)
    if stores.apply:
        require_no_sessions(stores)
    record(stores, directory, "rollback-started")
    if RETAINED_DATABASE in databases:
        if DATABASE in databases:
            stores.psql_script(MAINTENANCE_DATABASE, SQL_ROLLBACK_SET_ASIDE)
        stores.psql_script(MAINTENANCE_DATABASE, SQL_ROLLBACK_RESTORE)
    restore_graph(stores, stack, directory)
    # The forced run replaced the extractor's marker. Left alone, a restart would resume that run into the old database.
    for marker in state["extraction_markers"]:
        restore = f"cat >/v/.reset-marker && chown --reference=/v /v/.reset-marker && mv /v/.reset-marker /v/{marker}"
        stores.utility(
            restore,
            f"{stack.discogs_volume}:/v",
            timeout=60,
            image=stack.utility_image,
            stdin=(directory / marker.lstrip(".")).read_text(),
            mutate=True,
            show=f"restore {marker} in {stack.discogs_volume}",
        )
    newer = " ".join(name for name in markers(volume_listing(stores, stack)) if name not in state["extraction_markers"])
    purge_queues(stores)
    clear_valkey(stores, stack)
    stores.mutate(stores.compose("up", "--detach", "--no-deps", "--wait", "--wait-timeout", "600", "neo4j"), timeout=660)
    record(stores, directory, "rollback", newer)
    if stores.apply:
        # Reported, never blocking: a neighbour's change must not stand between the operator and a recovery.
        require_untouched(stores, state, accept=True)
    warn_newer_markers(newer)
    print("Retained stores are live again. Start the services (runbook, Rollback).")


def warn_newer_markers(newer: str) -> None:
    if newer:
        print(f"WARNING: {newer} appeared after the capture. Do NOT start {EXTRACTOR} until you decide what to do with that dump (runbook, Rollback)")


def stage_finish(stores: Stores, stack: Stack, args: argparse.Namespace) -> None:
    """After the proof. This is the only point where the old data stops existing."""
    directory = state_dir(args.state_dir)
    state = load_state(directory)
    require_window()
    if done(directory, "rollback-started") and not done(directory, "rollback"):
        fail("a rollback started and did not finish. Run rollback again: the retained graph may be the only good one")
    if int(stores.scalar(DATABASE, SQL_LOAD_COMPLETE)) < 1:
        fail(f"{DATABASE} has no complete Discogs load. Refusing to drop the retained copy")
    complete = stores.docker_exec("neo4j", CYPHER_SHELL, CYPHER_COMPLETE).split()
    if not complete or not complete[-1].isdigit() or int(complete[-1]) == 0:
        fail("Neo4j has no complete extraction. Refusing to drop the retained copy")
    require_untouched(stores, state, accept=args.accept_shared_change)
    stores.psql_script(MAINTENANCE_DATABASE, SQL_FINISH)
    if volume_exists(stores, stack.retained_volume):
        stores.mutate(["docker", "volume", "rm", stack.retained_volume], timeout=120)
    record(stores, directory, "finish")


STAGES = {
    "preflight": stage_preflight,
    "stop": stage_stop,
    "capture": stage_capture,
    "reset-postgres": stage_reset_postgres,
    "reset-neo4j": stage_reset_neo4j,
    "reset-rabbitmq": stage_reset_rabbitmq,
    "reset-valkey": stage_reset_valkey,
    "bootstrap": stage_bootstrap,
    "start": stage_start,
    "status": stage_status,
    "rollback": stage_rollback,
    "finish": stage_finish,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog-reset", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--apply", action="store_true", help="make the changes; without it the stage only prints them")
    parser.add_argument("--state-dir", help="private directory (0700, outside the checkout) for the capture")
    parser.add_argument("--admin-container", default=os.environ.get("CATALOG_RESET_ADMIN_CONTAINER", DEFAULT_ADMIN_CONTAINER))
    parser.add_argument("--admin-user", default=os.environ.get("CATALOG_RESET_ADMIN_USER", DEFAULT_ADMIN_USER))
    parser.add_argument(
        "--rehearsal-skip-release-floor", action="store_true", help="preflight on a disposable stack only; refused on the groovemap project"
    )
    parser.add_argument(
        "--accept-shared-change", action="store_true", help="carry on past a change on the shared server, after you have looked at it"
    )
    parser.add_argument("--min-free-gb", type=int, default=400, help="preflight: free disk the re-ingest needs beside the retained copies")
    parser.add_argument("--schema-timeout", type=int, default=1800, help="deadline in seconds for store-check and schema-init")
    parser.add_argument("--interval", type=int, default=60, help="status: seconds between samples")
    parser.add_argument("--duration", type=int, default=3600, help="status: stop after this many seconds")
    args = parser.parse_args(argv)
    if not shutil.which("docker"):
        print("catalog-reset: docker is not on PATH", file=sys.stderr)
        return 2
    stores = Stores(args.apply, args.admin_container, args.admin_user)
    try:
        STAGES[args.stage](stores, load_stack(stores), args)
    except ResetError as error:
        print(f"catalog-reset: STOPPED in {args.stage}: {error}", file=sys.stderr)
        return 1
    print(f"catalog-reset: {args.stage} {'done' if args.apply or args.stage in ('preflight', 'status') else 'dry run done, nothing changed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
