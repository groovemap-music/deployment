"""The guards in scripts/catalog_reset.py and how docs/catalog-reset.md stays in step with it.

The live proof is `just rehearse-catalog-reset`. These tests pin what must stay true of every
future edit: the statements that can run on the shared server, and nothing but those.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
UTC = dt.UTC
SCRIPT = ROOT / "scripts" / "catalog_reset.py"
RUNBOOK = ROOT / "docs" / "catalog-reset.md"

spec = importlib.util.spec_from_file_location("catalog_reset", SCRIPT)
assert spec is not None and spec.loader is not None
reset = importlib.util.module_from_spec(spec)
sys.modules["catalog_reset"] = reset
spec.loader.exec_module(reset)

ALLOWED_TARGETS = {"groovemap", "groovemap_pre_reset", "groovemap_reset_failed"}
DESTRUCTIVE_SQL = {
    "SQL_SWAP": reset.SQL_SWAP,
    "SQL_ROLLBACK_SET_ASIDE": reset.SQL_ROLLBACK_SET_ASIDE,
    "SQL_ROLLBACK_RESTORE": reset.SQL_ROLLBACK_RESTORE,
    "SQL_FINISH": reset.SQL_FINISH,
}


def statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def module_tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text())


# --- the shared server ----------------------------------------------------------------------


@pytest.mark.parametrize("name", DESTRUCTIVE_SQL)
def test_database_level_ddl_names_only_groovemap_databases(name: str) -> None:
    for statement in statements(DESTRUCTIVE_SQL[name]):
        match = re.match(r"(?:ALTER|CREATE|DROP) DATABASE (?:IF EXISTS )?(\w+)(?: RENAME TO (\w+))?", statement)
        if statement.startswith("REVOKE"):
            assert statement == "REVOKE ALL ON DATABASE groovemap FROM PUBLIC"
            continue
        assert match, statement
        assert {group for group in match.groups() if group} <= ALLOWED_TARGETS


def string_constants() -> list[str]:
    constants = (node.value for node in ast.walk(module_tree()) if isinstance(node, ast.Constant))
    return [value for value in constants if isinstance(value, str)]


def test_the_new_database_is_the_only_database_ever_created() -> None:
    assert [name for text in string_constants() for name in re.findall(r"^CREATE DATABASE (\w+)", text, re.MULTILINE)] == ["groovemap"]


def test_only_the_retained_copies_are_ever_dropped() -> None:
    dropped = [name for text in string_constants() for name in re.findall(r"^DROP DATABASE (?:IF EXISTS )?(\w+)", text, re.MULTILINE)]
    assert dropped == ["groovemap_pre_reset", "groovemap_reset_failed"]


def test_no_formatted_string_holds_ddl() -> None:
    """A statement built with an f-string is how a database name would get in."""
    words = re.compile(r"\b(DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|RENAME)\b")
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.JoinedStr):
            literal = "".join(str(part.value) for part in node.values if isinstance(part, ast.Constant))
            assert not words.search(literal) or not literal.lstrip().startswith(("DROP", "ALTER", "CREATE", "GRANT", "REVOKE", "TRUNCATE")), literal


def test_every_statement_that_changes_the_server_is_in_the_runbook() -> None:
    text = RUNBOOK.read_text()
    for sql in (reset.SQL_SWAP, reset.SQL_PROVISION, reset.SQL_ROLLBACK_SET_ASIDE, reset.SQL_ROLLBACK_RESTORE, reset.SQL_FINISH):
        for statement in statements(sql):
            assert statement in text, statement
    ddl = [item for constant in string_constants() for item in statements(constant) if re.match(r"(ALTER|CREATE|DROP|GRANT|REVOKE) ", item)]
    assert sorted(ddl) == sorted(
        item
        for sql in (reset.SQL_SWAP, reset.SQL_PROVISION, reset.SQL_ROLLBACK_SET_ASIDE, reset.SQL_ROLLBACK_RESTORE, reset.SQL_FINISH)
        for item in statements(sql)
    )


def test_only_the_named_volumes_are_removed_and_the_runbook_names_every_command() -> None:
    text = RUNBOOK.read_text()
    for command in (
        "docker volume rm groovemap_neo4j_data",
        "docker volume rm groovemap_neo4j_data_pre_reset",
        "docker compose rm --force",
        "rabbitmqctl -q purge_queue",
        "UNLINK",
    ):
        assert command in text


@pytest.mark.parametrize(
    "word",
    [
        "pg_terminate_backend",
        "pg_cancel_backend",
        "WITH (FORCE)",
        "TRUNCATE",
        "DROP SCHEMA",
        "DROP ROLE",
        "ALTER ROLE",
        "ALTER SYSTEM",
        "FLUSHALL",
        "FLUSHDB",
    ],
)
def test_no_statement_can_reach_past_the_database(word: str) -> None:
    source = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#") and "no pg_terminate_backend" not in line
    )
    assert word not in source


def test_no_sql_constant_is_built_from_a_variable() -> None:
    """The literals are the review surface, so a formatted database name must not get in."""
    for node in module_tree().body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("SQL_"):
            assert not any(isinstance(child, ast.FormattedValue | ast.BinOp) for child in ast.walk(node.value)), node.targets[0].id


@pytest.mark.parametrize("database", ["phaze", "synapse", "template1", "groovemap_pre_reset", "groovemap; DROP DATABASE phaze", ""])
def test_psql_refuses_every_other_database(database: str) -> None:
    stores = reset.Stores(False, "postgres", "datum")
    with pytest.raises(reset.ResetError, match="may connect only"):
        stores.psql_argv(database)


def test_psql_connects_to_the_two_databases_it_needs() -> None:
    stores = reset.Stores(False, "postgres", "datum")
    assert stores.psql_argv("postgres")[-1] == "postgres"
    assert stores.psql_argv("groovemap")[-1] == "groovemap"


def test_the_dump_check_cannot_match_itself() -> None:
    """Its text contains pg_dump, so without this every preflight reports a running dump."""
    assert "pid <> pg_backend_pid()" in reset.SQL_DUMPS


def test_the_swap_renames_before_it_creates_and_keeps_the_old_data() -> None:
    swap = statements(reset.SQL_SWAP)
    assert swap[0] == "ALTER DATABASE groovemap RENAME TO groovemap_pre_reset"
    assert swap[1] == "ALTER DATABASE groovemap_pre_reset WITH ALLOW_CONNECTIONS false"
    assert swap[2].startswith("CREATE DATABASE groovemap OWNER groovemap TEMPLATE template0")
    assert "DROP" not in reset.SQL_SWAP


def test_the_new_database_matches_what_homelab_provisions() -> None:
    assert "CONNECTION LIMIT 60" in reset.SQL_SWAP
    assert "CREATE EXTENSION pg_trgm" in reset.SQL_PROVISION
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in reset.SQL_PROVISION
    assert "GRANT USAGE, CREATE ON SCHEMA public TO groovemap" in reset.SQL_PROVISION


# --- the window -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "blocked"),
    [(7, 29, False), (7, 30, True), (8, 0, True), (9, 59, True), (10, 0, False), (23, 0, False)],
)
def test_the_dump_window_is_refused(hour: int, minute: int, blocked: bool) -> None:
    assert reset.blocked_window(dt.datetime(2026, 11, 5, hour, minute, tzinfo=UTC)) is blocked


def test_the_window_check_refuses_inside_the_window() -> None:
    with pytest.raises(reset.ResetError, match="dump window"):
        reset.require_window(dt.datetime(2026, 11, 5, 8, 30, tzinfo=UTC))
    reset.require_window(dt.datetime(2026, 11, 5, 12, 0, tzinfo=UTC))


def test_every_stage_that_writes_checks_the_window_or_is_a_recovery() -> None:
    source = SCRIPT.read_text()
    stages = ("stop", "reset_postgres", "reset_neo4j", "reset_rabbitmq", "reset_valkey", "bootstrap", "start", "finish")
    for stage in stages:
        body = source.split(f"def stage_{stage}(")[1].split("\ndef ")[0]
        assert "require_window()" in body, stage


def test_autovacuum_does_not_count_as_a_session() -> None:
    assert "backend_type = 'client backend'" in reset.SQL_SESSIONS


def _body(stage: str) -> str:
    return SCRIPT.read_text().split(f"def {stage}(")[1].split("\ndef ")[0]


def test_schema_init_reaches_the_database_through_depends_on() -> None:
    assert 'stores.compose("up", "--exit-code-from", "schema-init", "schema-init")' in _body("stage_bootstrap")
    assert '"--no-deps"' not in _body("stage_start")


def test_consumers_and_applications_start_in_one_call_before_the_extractor() -> None:
    body = _body("stage_start")
    assert body.index("*CONSUMERS, *names") < body.index("EXTRACTOR")
    assert "fail(" in body
    assert "ANALYTICS" in body


def test_only_rollback_starts_a_schema_client_without_schema_init() -> None:
    """The old database keeps its schema, so no schema-init may run for it."""
    assert '"--no-deps"' in _body("stage_rollback")
    assert "schema-init" not in _body("stage_rollback")


# --- what is kept ---------------------------------------------------------------------------


def test_the_handled_marker_is_never_carried_across() -> None:
    """catalog-api reads it as 'this version is done', and the re-ingest is that same version."""
    assert "action NOT LIKE 'identity.reattach.auto%'" in reset.KEEP_AUDIT_SQL
    assert reset.AUDIT_HANDLED_MARKER_PREFIX == "identity.reattach.auto"
    assert reset.KEEP_USERS_SQL.startswith("COPY (SELECT * FROM public.users")


def test_restore_uses_the_capture_header_as_the_column_list() -> None:
    sql = reset.copy_restore_sql("public.users", 'id,email\n1,"a@b"\n')
    assert sql.splitlines()[0] == "COPY public.users (id, email) FROM STDIN WITH (FORMAT csv, HEADER true);"
    assert sql.endswith("\\.\n")


@pytest.mark.parametrize("table", ["public.releases", "pg_catalog.pg_authid", "users"])
def test_restore_refuses_any_other_table(table: str) -> None:
    with pytest.raises(reset.ResetError):
        reset.copy_restore_sql(table, "id\n1\n")


def test_restore_refuses_a_header_that_is_not_column_names() -> None:
    with pytest.raises(reset.ResetError):
        reset.copy_restore_sql("public.users", "id); DROP TABLE users; --\n1\n")


def test_user_owned_tables_are_either_kept_or_must_be_empty() -> None:
    for table in ("public.identity_link_events", "public.catalog_item_supersessions", "public.app_config", "activity.erasures", "activity.events"):
        assert table in reset.MUST_BE_EMPTY
    assert {"public.admin_audit_log", "public.extraction_history"} == reset.REBUILT_USER_CHILDREN


def test_valkey_keeps_session_revocations_and_removes_everything_else() -> None:
    kept, removed = reset.split_keep(
        [
            "groovemap:revoked:jti:a",
            "groovemap:password_changed:b",
            "groovemap:legacy-keys:cutover",
            "groovemap:explore:artist:x",
            "groovemap:insights:g3:y",
            "groovemap:snapshot:t",
            "groovemap:reset:t",
            "groovemap:genre-tree:v1",
            "something:else",
        ]
    )
    assert len(kept) == 3
    assert len(removed) == 6


def test_only_groovemap_catalog_queues_are_purged() -> None:
    purged, left = reset.reset_queues(
        [
            "groovemap-discogs-tableinator-releases",
            "groovemap-discogs-tableinator-releases.dlq",
            "groovemap-musicbrainz-graphinator-artists",
            "other-app",
            "groovemap-sync",
        ]
    )
    assert len(purged) == 3
    assert left == ["other-app", "groovemap-sync"]


def test_no_volume_command_can_widen_past_the_named_volume() -> None:
    source = SCRIPT.read_text()
    for word in ("volume prune", "system prune", '"down"', "--volumes"):
        assert word not in source
    assert re.findall(r'\["docker", "volume", "rm", ([\w.]+)\]', source) == ["stack.neo4j_volume", "stack.retained_volume"]


def test_the_stop_order_starts_with_the_extractor_and_ends_with_the_last_clients() -> None:
    assert reset.STOP_ORDER[0][0] == "extractor-discogs"
    assert "tableinator" in reset.STOP_ORDER[1]
    assert "graphinator" in reset.STOP_ORDER[1]
    assert set(reset.STOP_ORDER[-1]) == {"api", "dashboard", "explore"}
    assert "insights" in reset.STOP_ORDER[2]


def test_the_copy_never_runs_after_a_failed_empty() -> None:
    assert " && cp -a " in reset.COPY_VOLUME


def test_valkey_quoting_survives_non_ascii_and_quotes() -> None:
    assert reset.valkey_unlink(["groovemap:explore:Bj\u00f6rk", 'a"b']) == 'UNLINK "groovemap:explore:Bj\\xc3\\xb6rk" "a\\x22b"\n'


def test_the_floor_needs_the_new_schema_and_newer_than_the_pins_lacking_the_fixes() -> None:
    assert (
        len(
            reset.below_floor(
                {
                    "DATABASE_SCHEMA_IMAGE": "0.6.3",
                    "DISCOGS_SQL_LOADER_IMAGE": "0.4.4",
                    "DISCOGS_GRAPH_ENRICHER_IMAGE": "0.6.0",
                    "CATALOG_API_IMAGE": "0.6.0",
                    "ANALYTICS_ENGINE_IMAGE": "0.5.0",
                }
            )
        )
        == 5
    )
    assert (
        reset.below_floor(
            {
                "DATABASE_SCHEMA_IMAGE": "0.7.0",
                "DISCOGS_SQL_LOADER_IMAGE": "0.4.5",
                "DISCOGS_GRAPH_ENRICHER_IMAGE": "0.6.1",
                "CATALOG_API_IMAGE": "0.7.0",
                "ANALYTICS_ENGINE_IMAGE": "0.6.0",
            }
        )
        == []
    )


def test_a_neighbours_new_database_is_a_note_and_a_changed_one_is_a_stop() -> None:
    before = {"other_databases": "phaze|1\n", "roles": "datum|2\n", "postmaster_start": "t"}
    assert reset.compare_fingerprint(before, {**before, "other_databases": "phaze|1\nnew|3\n"}) == ([], ["other_databases: new"])
    assert reset.compare_fingerprint(before, {**before, "other_databases": "phaze|9\n"})[0] == ["other_databases: phaze"]
    assert reset.compare_fingerprint(before, {**before, "postmaster_start": "u"})[0] == ["postmaster_start"]


# --- dry runs -------------------------------------------------------------------------------


def test_mutate_without_apply_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    stores = reset.Stores(False, "postgres", "datum")
    assert stores.mutate(["false"]) == ""
    assert "would run: false" in capsys.readouterr().out


def test_a_timeout_is_a_failure() -> None:
    stores = reset.Stores(True, "postgres", "datum")
    with pytest.raises(reset.ResetError, match="timed out"):
        stores.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)


def test_state_dir_must_be_private_and_outside_the_checkout(tmp_path: Path) -> None:
    with pytest.raises(reset.ResetError, match="outside the checkout"):
        reset.state_dir(str(ROOT / "docs"))
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(reset.ResetError, match="0700"):
        reset.state_dir(str(open_dir))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    assert reset.state_dir(str(private)) == private.resolve()


def test_written_state_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "users.csv"
    reset.write_private(path, "secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_dump_check_names_a_file_a_forced_run_would_download() -> None:
    listing = [f"discogs_20261101_{kind}.xml.gz" for kind in ("artists", "labels", "masters", "releases")]
    metadata: dict[str, Any] = {name: {"checksum": "x"} for name in listing}
    reset.require_dump(listing, metadata, "20261101")
    del metadata["discogs_20261101_masters.xml.gz"]
    with pytest.raises(reset.ResetError, match="would download"):
        reset.require_dump(listing, metadata, "20261101")


def test_the_newest_marker_wins() -> None:
    assert reset.newest_marker([".extraction_status_20261001.json", ".extraction_status_20261101.json", "x"]) == ".extraction_status_20261101.json"
    with pytest.raises(reset.ResetError):
        reset.newest_marker(["discogs_20261101_artists.xml.gz"])


@pytest.mark.parametrize(
    "render",
    [
        {"name": "p", "services": {}, "volumes": {"neo4j_data": {}, "discogs_data": {}}},
        {
            "name": "p",
            "services": {"tableinator": {"environment": {"POSTGRES_DATABASE": "phaze"}}, "data-volume-init": {"image": "i"}},
            "volumes": {"neo4j_data": {}, "discogs_data": {}},
        },
        {"name": "p", "services": {"tableinator": {}}, "volumes": {}},
    ],
)
def test_it_refuses_a_stack_that_is_not_groovemap(render: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    stores = reset.Stores(False, "postgres", "datum")
    monkeypatch.setattr(stores, "render", lambda: render)
    with pytest.raises(reset.ResetError):
        reset.load_stack(stores)


# --- the stages, against a scripted host ----------------------------------------------------


ACL = "{groovemap=CTc/groovemap}"
FINGERPRINT = {"other_databases": "phaze|1|datum\n", "roles": "datum|10\n", "postmaster_start": "t0"}


class Host(reset.Stores):  # type: ignore[name-defined]
    """Docker and psql as a table of answers. Records every command, and runs none."""

    def __init__(self, *, apply: bool = True) -> None:
        super().__init__(apply, "postgres", "datum")
        self.calls: list[list[str]] = []
        self.sessions = "0"
        self.databases = {"groovemap": f"groovemap|groovemap|60|t|{ACL}"}
        self.fingerprint = dict(FINGERPRINT)
        self.tallies = {"v": "9 900", "v_pre_reset": "9 900"}
        self.volumes = {"v", "d"}
        self.queues = "q\t0\t0\t0\n"
        self.valkey = ""
        self.nodes = "0"
        self.complete = "1"
        self.poll = "false"
        self.counts: dict[str, str] = {}
        self.render_images: dict[str, Any] = {}

    def text(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)

    def render(self) -> dict[str, Any]:
        return {"services": self.render_images}

    def run(self, argv: list[str], *, timeout: int = 120, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:  # noqa: ARG002
        self.calls.append([*argv, *([stdin] if stdin else [])])
        return subprocess.CompletedProcess(argv, self.code(argv), self.answer(argv), "")

    def code(self, argv: list[str]) -> int:
        return 0 if argv[:3] != ["docker", "volume", "inspect"] or argv[3] in self.volumes else 1

    def answer(self, argv: list[str], *_: Any) -> str:
        if "-c" in argv and "psql" in argv:
            return self.sql(argv[argv.index("-c") + 1])
        joined = " ".join(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return "abc123\n"
        if "printenv" in joined:
            return f"{self.poll}\n"
        if "list_queues" in joined:
            return self.queues
        if "--scan" in joined:
            return self.valkey
        if "cypher-shell" in joined:
            return f"result\n{self.complete if 'ExtractionCompletion' in joined else self.nodes}\n"
        if "find /v -type f" in joined:
            return self.tallies["v" if ":/v:ro" in joined and "_pre_reset:" not in joined else "v_pre_reset"]
        if "ls -A" in joined:
            return ".extraction_status_20261101.json\n.discogs_metadata.json\n"
        if "cat /v/" in joined:
            return "{}"
        return ""

    def sql(self, sql: str) -> str:
        answers = {
            reset.SQL_SESSIONS: self.sessions,
            reset.SQL_OWN_DATABASES: "\n".join(self.databases.values()) + "\n",
            reset.SQL_OTHER_DATABASES: self.fingerprint["other_databases"],
            reset.SQL_ROLES: self.fingerprint["roles"],
            reset.SQL_POSTMASTER_START: self.fingerprint["postmaster_start"],
            reset.SQL_MARKER_COUNT: self.counts.get("marker", "0"),
            reset.SQL_LOAD_COMPLETE: self.counts.get("load", "1"),
            reset.SQL_USER_CHILD_TABLES: "oauth_tokens\nadmin_audit_log\nextraction_history\n",
            reset.SQL_DATABASE_BYTES: "1",
            reset.KEEP_USERS_SQL: self.counts.get("users_csv", "id\n1\n"),
            reset.KEEP_AUDIT_SQL: "id\n",
        }
        if sql in answers:
            return answers[sql]
        for name, value in self.counts.items():
            if name in sql:
                return value
        return "f" if "EXISTS" in sql else "0"


@pytest.fixture
def stack() -> Any:
    return reset.Stack("groovemap", {"api", "tableinator", "graphinator", "extractor-discogs"}, "v", "d", "image", False)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(reset, "require_window", lambda: None)
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    capture = {
        "datacl": ACL,
        "extraction_marker": ".extraction_status_20261101.json",
        "extraction_markers": [".extraction_status_20261101.json"],
        "fingerprint": FINGERPRINT,
        "keep_audit_rows": 0,
        "keep_user_rows": 1,
        "metadata_sha256": reset.sha256_text("{}"),
    }
    (directory / "capture.json").write_text(json.dumps(capture))
    (directory / "extraction_status_20261101.json").write_text("{}")
    (directory / "users.csv").write_text("id\n1\n")
    (directory / "admin_audit_log.csv").write_text("id\n")
    (directory / "done-preflight").write_text(json.dumps({"images": {}}))
    return directory


def arguments(directory: Path | None) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(directory) if directory else None,
        schema_timeout=60,
        min_free_gb=1,
        rehearsal_skip_release_floor=True,
        rehearsal_report=None,
        accept_shared_change=False,
    )


def run_stage(stage: Any, host: Host, stack: Any, directory: Path) -> None:
    stage(host, stack, arguments(directory))


def swapped(host: Host) -> None:
    host.databases = {
        "groovemap": f"groovemap|groovemap|60|t|{ACL}",
        "groovemap_pre_reset": f"groovemap_pre_reset|groovemap|60|f|{ACL}",
    }


def test_a_session_on_the_database_stops_the_swap(state: Path, stack: Any) -> None:
    host = Host()
    host.sessions = "1"
    with pytest.raises(reset.ResetError, match="still on groovemap"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_an_existing_retained_database_stops_the_swap(state: Path, stack: Any) -> None:
    host = Host()
    swapped(host)
    with pytest.raises(reset.ResetError, match="already exists"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_an_empty_capture_stops_the_swap(state: Path, stack: Any) -> None:
    capture = json.loads((state / "capture.json").read_text())
    (state / "capture.json").write_text(json.dumps({**capture, "keep_user_rows": 0}))
    host = Host()
    with pytest.raises(reset.ResetError, match="no users"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_a_changed_neighbour_stops_before_the_swap_not_after(state: Path, stack: Any) -> None:
    host = Host()
    host.fingerprint["other_databases"] = "phaze|2|datum\n"
    with pytest.raises(reset.ResetError, match="SHARED SERVER CHANGED"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_a_copy_that_differs_leaves_the_graph_and_records_nothing(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    host.tallies["v_pre_reset"] = "3 300"
    with pytest.raises(reset.ResetError, match="copy differs"):
        run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "volume rm" not in host.text()
    assert not (state / "done-neo4j-copied").exists()


def test_an_empty_source_is_never_taken_for_a_good_copy(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    host.tallies = {"v": "0 0", "v_pre_reset": "0 0"}
    with pytest.raises(reset.ResetError, match="copy differs"):
        run_stage(reset.stage_reset_neo4j, host, stack, state)


def test_a_missing_source_volume_is_not_created_by_docker(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    host.volumes = set()
    with pytest.raises(reset.ResetError, match="does not exist"):
        run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "docker run" not in host.text()


def test_a_second_reset_neo4j_changes_nothing(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "volume rm v" in host.text()
    assert (state / "done-neo4j-copied").read_text() == "9 900"
    again = Host()
    run_stage(reset.stage_reset_neo4j, again, stack, state)
    assert "volume rm" not in again.text()
    assert "docker run" not in again.text()


def test_a_reset_neo4j_interrupted_after_the_copy_only_removes(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "cp -a" not in host.text()
    assert "volume rm v" in host.text()


def test_rollback_leaves_the_graph_alone_without_a_verified_copy(state: Path, stack: Any) -> None:
    host = Host()
    swapped(host)
    run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME TO groovemap_reset_failed" in host.text()
    assert "cp -a" not in host.text()
    assert "find /to" not in host.text()


def test_rollback_stops_before_any_rename_when_the_retained_graph_no_longer_matches(state: Path, stack: Any) -> None:
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    swapped(host)
    host.volumes.add("v_pre_reset")
    host.tallies["v_pre_reset"] = "1 10"
    with pytest.raises(reset.ResetError, match="not the verified copy"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()
    assert "compose stop" not in host.text()
    assert not (state / "done-rollback").exists()


def test_rollback_stops_before_any_rename_when_the_retained_volume_is_gone(state: Path, stack: Any) -> None:
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    swapped(host)
    with pytest.raises(reset.ResetError, match="not the verified copy"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()


def test_rollback_with_no_copy_and_no_live_graph_changes_nothing(state: Path, stack: Any) -> None:
    host = Host()
    swapped(host)
    host.volumes = {"d"}
    with pytest.raises(reset.ResetError, match="Nothing was changed"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()


def test_rollback_stops_neo4j_before_the_copy_and_clears_queues_and_caches(state: Path, stack: Any) -> None:
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    swapped(host)
    host.volumes.add("v_pre_reset")
    host.queues = "groovemap-discogs-tableinator-releases\t1\t0\t0\n"
    run_stage(reset.stage_rollback, host, stack, state)
    text = host.text()
    assert text.index("compose stop --timeout 300 neo4j") < text.index("cp -a /from/. /to/")
    assert "purge_queue groovemap-discogs-tableinator-releases" in text


def test_rollback_clears_stale_caches(state: Path, stack: Any) -> None:
    host = Host()
    swapped(host)
    host.valkey = "groovemap:explore:x\n"
    with pytest.raises(reset.ResetError, match="survived"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "UNLINK" in host.text()


def test_rollback_stops_when_the_restored_graph_does_not_match(state: Path, stack: Any) -> None:
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    swapped(host)
    host.volumes.add("v_pre_reset")
    host.tallies["v"] = "1 10"
    with pytest.raises(reset.ResetError, match="after the restore"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert not (state / "done-rollback").exists()


def test_rollback_stops_on_a_session_before_any_rename(state: Path, stack: Any) -> None:
    host = Host()
    swapped(host)
    host.sessions = "1"
    with pytest.raises(reset.ResetError, match="still on groovemap"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()


def test_rollback_refuses_new_pins_on_the_old_schema(state: Path, stack: Any) -> None:
    (state / "done-preflight").write_text(json.dumps({"images": {"api": "old"}}))
    host = Host()
    swapped(host)
    host.render_images = {"api": {"image": "new"}}
    with pytest.raises(reset.ResetError, match="Put the old"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()


def test_a_changed_neighbour_never_blocks_a_rollback(state: Path, stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    host = Host()
    swapped(host)
    host.fingerprint["postmaster_start"] = "rebooted"
    run_stage(reset.stage_rollback, host, stack, state)
    assert (state / "done-rollback").exists()
    assert "WARNING: the shared server changed" in capsys.readouterr().out


def test_an_accepted_shared_change_lets_finish_go_on(state: Path, stack: Any) -> None:
    host = Host()
    host.fingerprint["postmaster_start"] = "rebooted"
    arguments_ = arguments(state)
    arguments_.accept_shared_change = True
    reset.stage_finish(host, stack, arguments_)
    assert "DROP DATABASE" in host.text()


def test_rollback_warns_about_a_newer_dump_every_time(
    state: Path, stack: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host()
    swapped(host)
    monkeypatch.setattr(reset, "volume_listing", lambda *_: [".extraction_status_20261101.json", ".extraction_status_20261201.json"])
    run_stage(reset.stage_rollback, host, stack, state)
    assert "20261201" in capsys.readouterr().out
    run_stage(reset.stage_rollback, Host(), stack, state)
    assert "20261201" in capsys.readouterr().out


def test_rollback_after_finish_is_refused(state: Path, stack: Any) -> None:
    (state / "done-finish").write_text("")
    with pytest.raises(reset.ResetError, match="restore from the dump"):
        run_stage(reset.stage_rollback, Host(), stack, state)


def test_a_finish_that_ran_is_recorded(state: Path, stack: Any) -> None:
    run_stage(reset.stage_finish, Host(), stack, state)
    assert (state / "done-finish").exists()


def test_rollback_restores_a_verified_graph_and_every_marker(state: Path, stack: Any) -> None:
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    swapped(host)
    host.volumes.add("v_pre_reset")
    run_stage(reset.stage_rollback, host, stack, state)
    assert "cp -a /from/. /to/" in host.text()
    assert ".extraction_status_20261101.json" in host.text().split("cp -a")[1]
    assert (state / "done-rollback").exists()


def test_a_rollback_that_stopped_after_the_rename_runs_again(state: Path, stack: Any) -> None:
    (state / "done-rollback-started").write_text("")
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    host.volumes.add("v_pre_reset")
    run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()
    assert "cp -a" in host.text()


def test_rollback_after_only_stop_and_capture_changes_no_database(state: Path, stack: Any) -> None:
    host = Host()
    run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME" not in host.text()
    assert "cp -a" not in host.text()


def test_rollback_with_neither_database_stops(state: Path, stack: Any) -> None:
    host = Host()
    host.databases = {}
    with pytest.raises(reset.ResetError, match="restore from the dump"):
        run_stage(reset.stage_rollback, host, stack, state)


def test_rollback_after_the_rename_with_no_new_database_only_restores(state: Path, stack: Any) -> None:
    host = Host()
    host.databases = {"groovemap_pre_reset": f"groovemap_pre_reset|groovemap|60|f|{ACL}"}
    run_stage(reset.stage_rollback, host, stack, state)
    assert "RENAME TO groovemap_reset_failed" not in host.text()
    assert "groovemap_pre_reset RENAME TO groovemap" in host.text()


@pytest.mark.parametrize("load", ["0"])
def test_finish_refuses_without_a_complete_load(state: Path, stack: Any, load: str) -> None:
    host = Host()
    host.counts["load"] = load
    with pytest.raises(reset.ResetError, match="no complete Discogs load"):
        run_stage(reset.stage_finish, host, stack, state)
    assert "DROP" not in host.text()


def test_finish_refuses_a_graph_with_no_complete_extraction(state: Path, stack: Any) -> None:
    host = Host()
    host.complete = "0"
    with pytest.raises(reset.ResetError, match="no complete extraction"):
        run_stage(reset.stage_finish, host, stack, state)
    assert "DROP" not in host.text()


def test_finish_refuses_an_unfinished_rollback(state: Path, stack: Any) -> None:
    (state / "done-rollback-started").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="did not finish"):
        run_stage(reset.stage_finish, host, stack, state)
    assert "DROP" not in host.text()


def test_finish_checks_the_neighbours_before_it_drops(state: Path, stack: Any) -> None:
    host = Host()
    host.fingerprint["roles"] = "datum|11\n"
    with pytest.raises(reset.ResetError, match="SHARED SERVER CHANGED"):
        run_stage(reset.stage_finish, host, stack, state)
    assert "DROP" not in host.text()


def test_finish_drops_only_the_retained_copies(state: Path, stack: Any) -> None:
    host = Host()
    host.volumes = {"v", "d", "v_pre_reset"}
    run_stage(reset.stage_finish, host, stack, state)
    assert "DROP DATABASE IF EXISTS groovemap_pre_reset" in host.text()
    assert "volume rm v_pre_reset" in host.text()
    assert "volume rm v\n" not in host.text()


def test_stop_removes_the_writers_so_a_daemon_restart_cannot_revive_them(state: Path, stack: Any) -> None:
    host = Host()
    reset.stage_stop(host, stack, arguments(state))
    assert "compose rm --force api" in host.text()
    assert "compose rm --force tableinator graphinator" in host.text()
    assert "compose rm --force extractor-discogs" in host.text()


def test_stop_needs_a_fresh_preflight(state: Path, stack: Any) -> None:
    (state / "done-preflight").unlink()
    with pytest.raises(reset.ResetError, match="no pre-flight"):
        reset.stage_stop(Host(), stack, arguments(state))


def test_stop_refuses_a_stale_preflight(state: Path, stack: Any) -> None:
    os.utime(state / "done-preflight", (0, 0))
    host = Host()
    with pytest.raises(reset.ResetError, match="hours old"):
        reset.stage_stop(host, stack, arguments(state))
    assert "compose stop" not in host.text()


def test_stop_keeps_the_consumers_evidence_before_removing_them(state: Path, stack: Any) -> None:
    host = Host()
    reset.stage_stop(host, stack, arguments(state))
    assert (state / "evidence-stop-logs.txt").exists()
    assert (state / "evidence-stop-state.txt").exists()
    assert host.text().index("compose logs") < host.text().index("compose rm")


def test_stop_stops_on_a_session_left_behind(state: Path, stack: Any) -> None:
    host = Host()
    host.sessions = "1"
    with pytest.raises(reset.ResetError, match="still on groovemap"):
        reset.stage_stop(host, stack, arguments(state))


def test_capture_stops_on_a_session(tmp_path: Path, stack: Any) -> None:
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    host = Host()
    host.sessions = "1"
    with pytest.raises(reset.ResetError, match="still on groovemap"):
        run_stage(reset.stage_capture, host, stack, directory)
    assert list(directory.iterdir()) == []


def test_reset_postgres_refuses_a_used_state_directory(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="new --state-dir"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_reset_postgres_stops_when_the_new_database_differs(state: Path, stack: Any) -> None:
    host = Host()
    original = host.mutate

    def swap(*args: Any, **kwargs: Any) -> str:
        host.databases = {
            "groovemap": "groovemap|groovemap|60|t|{other}",
            "groovemap_pre_reset": f"groovemap_pre_reset|groovemap|60|f|{ACL}",
        }
        return str(original(*args, **kwargs))

    host.mutate = swap
    with pytest.raises(reset.ResetError, match="does not match the old one"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert not (state / "done-reset-postgres").exists()


def test_reset_neo4j_needs_reset_postgres(state: Path, stack: Any) -> None:
    host = Host()
    with pytest.raises(reset.ResetError, match="have not completed: reset-postgres"):
        run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "docker run" not in host.text()


def test_reset_neo4j_stops_neo4j_before_copying(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert host.text().index("compose stop --timeout 300 neo4j") < host.text().index("cp -a /from/. /to/")


def test_reset_neo4j_survives_a_crash_after_the_volume_was_removed(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    host.volumes = {"d"}
    run_stage(reset.stage_reset_neo4j, host, stack, state)
    assert "volume rm" not in host.text()
    assert (state / "done-reset-neo4j").exists()


def test_bootstrap_does_not_run_twice(state: Path, stack: Any) -> None:
    (state / "done-bootstrap").write_text("")
    host = Host()
    run_stage(reset.stage_bootstrap, host, stack, state)
    assert "schema-init" not in host.text()


def test_start_does_not_run_twice(state: Path, stack: Any) -> None:
    (state / "done-bootstrap").write_text("")
    (state / "done-start").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="already started"):
        run_stage(reset.stage_start, host, stack, state)
    assert "compose up" not in host.text()


# --- pre-flight, one guard at a time ---


GOOD_RENDER = {
    "tableinator": {"image": "x@sha256:b", "mem_limit": "32212254720"},
    "schema-init": {"image": "x@sha256:a"},
    "api": {"image": "x@sha256:c"},
    "graphinator": {"image": "x@sha256:d"},
    "extractor-discogs": {"image": "x@sha256:e"},
    "insights": {"image": "x@sha256:f"},
}
DIGESTS = {
    "DATABASE_SCHEMA_IMAGE": "a",
    "DISCOGS_SQL_LOADER_IMAGE": "b",
    "CATALOG_API_IMAGE": "c",
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "d",
    "DISCOGS_INGESTION_IMAGE": "e",
    "ANALYTICS_ENGINE_IMAGE": "f",
}
GOOD_VERSIONS = {
    "DATABASE_SCHEMA_IMAGE": "0.7.0",
    "DISCOGS_SQL_LOADER_IMAGE": "0.4.5",
    "DISCOGS_GRAPH_ENRICHER_IMAGE": "0.6.1",
    "CATALOG_API_IMAGE": "0.7.0",
    "ANALYTICS_ENGINE_IMAGE": "0.6.0",
}


class PreflightHost(Host):
    """A stack that passes every check, for tests that break exactly one."""

    def __init__(self) -> None:
        super().__init__(apply=False)
        self.sessions = "12"
        self.render_images = json.loads(json.dumps(GOOD_RENDER))
        self.counts["SELECT count(*) FROM public.users"] = "2"
        self.free = "2147483648\n"
        self.listing = "".join(f"discogs_20261101_{kind}.xml.gz\n" for kind in ("artists", "labels", "masters", "releases"))
        self.metadata = json.dumps({f"discogs_20261101_{kind}.xml.gz": {} for kind in ("artists", "labels", "masters", "releases")})

    def answer(self, argv: list[str], *rest: Any) -> str:
        joined = " ".join(argv)
        if "df -Pk" in joined:
            return self.free
        if "ls -A" in joined:
            return ".extraction_status_20261101.json\n.discogs_metadata.json\n" + self.listing
        if "cat /v/.discogs_metadata.json" in joined:
            return self.metadata
        return str(super().answer(argv, *rest))

    def sql(self, sql: str) -> str:
        if sql == reset.SQL_IS_SUPERUSER:
            return self.counts.get("superuser", "t")
        if sql == reset.SQL_DUMPS:
            return self.counts.get("dumps", "0")
        return str(super().sql(sql))


@pytest.fixture
def flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(reset, "require_window", lambda: None)
    monkeypatch.setattr(reset, "reviewed_images", lambda: (DIGESTS, GOOD_VERSIONS))
    directory = tmp_path / "flight"
    directory.mkdir(mode=0o700)
    host = PreflightHost()
    good = reset.Stack("groovemap", {"store-check-postgres", "tableinator", "api"}, "v", "d", "image", False)

    def go(**overrides: Any) -> None:
        namespace = arguments(directory)
        namespace.rehearsal_skip_release_floor = False
        report = directory.parent / "REPORT.json"
        images = {service: host.render_images.get(service, {}).get("image") for service in reset.REPORT_SERVICES}
        report.write_text(json.dumps({"result": "PASS", "dirty": False, "commit": "abc123", "images": images}))
        namespace.rehearsal_report = str(report)
        for key, value in overrides.items():
            setattr(namespace, key, value)
        reset.stage_preflight(host, good, namespace)

    return host, go, directory, good, monkeypatch


def test_preflight_passes_a_good_stack_and_records_itself(flight: Any) -> None:
    host, go, directory, *_ = flight
    go()
    assert (directory / "done-preflight").exists()
    assert not host.text().count("compose stop")


def test_preflight_needs_the_external_file_set(flight: Any) -> None:
    host, _go, directory, good, _ = flight
    bare = reset.Stack("groovemap", good.services - {"store-check-postgres"}, "v", "d", "image", False)
    with pytest.raises(reset.ResetError, match="external-PostgreSQL"):
        reset.stage_preflight(host, bare, arguments(directory))


def test_preflight_needs_a_superuser(flight: Any) -> None:
    host, go, *_ = flight
    host.counts["superuser"] = "f"
    with pytest.raises(reset.ResetError, match="not a superuser"):
        go()


def test_preflight_refuses_a_retained_database(flight: Any) -> None:
    host, go, *_ = flight
    swapped(host)
    with pytest.raises(reset.ResetError, match="has not finished"):
        go()


def test_preflight_refuses_another_owner(flight: Any) -> None:
    host, go, *_ = flight
    host.databases["groovemap"] = f"groovemap|someone|60|t|{ACL}"
    with pytest.raises(reset.ResetError, match="is owned by someone"):
        go()


def test_preflight_proves_the_stack_uses_this_server(flight: Any) -> None:
    host, go, *_ = flight
    host.sessions = "0"
    with pytest.raises(reset.ResetError, match="may not be the server"):
        go()


def test_preflight_waits_for_a_dump(flight: Any) -> None:
    host, go, *_ = flight
    host.counts["dumps"] = "1"
    with pytest.raises(reset.ResetError, match="dump session"):
        go()


def test_preflight_refuses_an_unreviewed_digest(flight: Any) -> None:
    host, go, *_ = flight
    host.render_images["api"]["image"] = "x@sha256:other"
    with pytest.raises(reset.ResetError, match="not pinned to the reviewed"):
        go()


def test_preflight_refuses_a_set_below_the_floor(flight: Any) -> None:
    _host, go, _directory, _good, monkeypatch = flight
    monkeypatch.setattr(reset, "reviewed_images", lambda: (DIGESTS, {**GOOD_VERSIONS, "DATABASE_SCHEMA_IMAGE": "0.6.3"}))
    with pytest.raises(reset.ResetError, match="not ready"):
        go()


@pytest.mark.parametrize("limit", [None, "0", 0])
def test_preflight_refuses_an_uncapped_loader(flight: Any, limit: Any) -> None:
    host, go, *_ = flight
    host.render_images["tableinator"]["mem_limit"] = limit
    with pytest.raises(reset.ResetError, match="no memory limit"):
        go()


def test_preflight_reads_a_deploy_style_cap(flight: Any) -> None:
    host, go, *_ = flight
    del host.render_images["tableinator"]["mem_limit"]
    host.render_images["tableinator"]["deploy"] = {"resources": {"limits": {"memory": "32212254720"}}}
    go()


def test_the_rehearsal_flag_is_refused_on_the_real_project(flight: Any) -> None:
    _host, go, *_ = flight
    with pytest.raises(reset.ResetError, match="disposable stack"):
        go(rehearsal_skip_release_floor=True)


def test_preflight_needs_users(flight: Any) -> None:
    host, go, *_ = flight
    host.counts["SELECT count(*) FROM public.users"] = "0"
    with pytest.raises(reset.ResetError, match="nothing to keep"):
        go()


def test_preflight_checks_the_user_tables(flight: Any) -> None:
    host, go, *_ = flight
    host.counts["public.app_config"] = "1"
    with pytest.raises(reset.ResetError, match="app_config"):
        go()


def test_preflight_checks_the_disk(flight: Any) -> None:
    host, go, *_ = flight
    host.free = "1024\n"
    with pytest.raises(reset.ResetError, match="GB free"):
        go()


def test_preflight_refuses_a_leftover_retained_volume(flight: Any) -> None:
    host, go, *_ = flight
    host.volumes.add("v_pre_reset")
    with pytest.raises(reset.ResetError, match="already exists"):
        go()


def test_preflight_records_what_each_service_runs(flight: Any) -> None:
    host, go, directory, _good, monkeypatch = flight
    original = host.answer
    monkeypatch.setattr(
        host, "answer", lambda argv, *rest: "id1\n" if "ps" in argv else "old-image\n" if "inspect" in argv else original(argv, *rest)
    )
    go()
    assert json.loads((directory / "done-preflight").read_text())["images"]["api"] == "old-image"


def test_preflight_needs_a_rehearsal_report(flight: Any) -> None:
    _host, go, *_ = flight
    with pytest.raises(reset.ResetError, match="--rehearsal-report is required"):
        go(rehearsal_report=None)


def test_preflight_needs_the_report_to_exist(flight: Any, tmp_path: Path) -> None:
    _host, go, *_ = flight
    with pytest.raises(reset.ResetError, match="no rehearsal report"):
        go(rehearsal_report=str(tmp_path / "missing.json"))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"result": "FAIL"}, "result is FAIL"),
        ({"dirty": True}, "uncommitted"),
        ({"dirty": None}, "uncommitted"),
        ({"commit": "other"}, "this checkout is abc123"),
        ({"images": {}}, "differs from the image"),
    ],
)
def test_preflight_refuses_a_report_that_does_not_cover_this_set(flight: Any, tmp_path: Path, change: dict[str, Any], reason: str) -> None:
    host, go, *_ = flight
    images = {service: host.render_images.get(service, {}).get("image") for service in reset.REPORT_SERVICES}
    report = {"result": "PASS", "dirty": False, "commit": "abc123", "images": images, **change}
    path = tmp_path / "other.json"
    path.write_text(json.dumps(report))
    with pytest.raises(reset.ResetError, match=reason):
        go(rehearsal_report=str(path))


def test_preflight_refuses_a_report_for_one_moved_image(flight: Any, tmp_path: Path) -> None:
    host, go, *_ = flight
    images = {service: host.render_images.get(service, {}).get("image") for service in reset.REPORT_SERVICES}
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"result": "PASS", "dirty": False, "commit": "abc123", "images": {**images, "api": "x@sha256:moved"}}))
    with pytest.raises(reset.ResetError, match="api differs"):
        go(rehearsal_report=str(path))


def test_preflight_warns_when_this_commit_moves_a_store(flight: Any, capsys: pytest.CaptureFixture[str]) -> None:
    host, go, _directory, _good, monkeypatch = flight
    host.render_images["neo4j"] = {"image": "neo4j@sha256:new"}
    original = host.answer

    def answer(argv: list[str], *rest: Any) -> str:
        if "ps" in argv and argv[-1] == "neo4j":
            return "id\n"
        if "inspect" in argv:
            return "neo4j@sha256:old\n"
        return str(original(argv, *rest))

    monkeypatch.setattr(host, "answer", answer)
    go()
    assert "moves the neo4j digest" in capsys.readouterr().out


# --- after a rollback, and after a finish that died ---


@pytest.mark.parametrize(
    "stage",
    ["stage_reset_neo4j", "stage_reset_rabbitmq", "stage_reset_valkey", "stage_bootstrap", "stage_start", "stage_after_load"],
)
def test_no_forward_stage_runs_once_a_rollback_has_started(state: Path, stack: Any, stage: str) -> None:
    for step in ("reset-postgres", "reset-neo4j", "reset-rabbitmq", "reset-valkey", "bootstrap", "start", "rollback-started"):
        (state / f"done-{step}").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="rolled back"):
        run_stage(getattr(reset, stage), host, stack, state)
    assert "compose up" not in host.text()
    assert "volume rm" not in host.text()


def test_reset_postgres_cannot_swap_a_rolled_back_database_out_again(state: Path, stack: Any) -> None:
    (state / "done-rollback-started").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="rolled back"):
        run_stage(reset.stage_reset_postgres, host, stack, state)
    assert "RENAME" not in host.text()


def test_after_load_starts_insights_without_schema_init(state: Path, stack: Any) -> None:
    """The loaders are up, so `depends_on` must not run schema-init beside them."""
    host = after_load_host(state)
    run_stage(reset.stage_after_load, host, stack, state)
    insights = next(call for call in host.calls if call[-1] == "insights" and "up" in call)
    assert "--no-deps" in insights


def test_rollback_after_a_finish_that_died_says_run_finish_again(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    (state / "done-neo4j-copied").write_text("9 900")
    host = Host()
    host.volumes.add("v_pre_reset")
    with pytest.raises(reset.ResetError, match="Run finish again"):
        run_stage(reset.stage_rollback, host, stack, state)
    assert "cp -a" not in host.text()
    assert "compose stop" not in host.text()
    assert not (state / "done-rollback").exists()


def test_a_resumed_rollback_is_not_mistaken_for_a_dead_finish(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    (state / "done-rollback-started").write_text("")
    run_stage(reset.stage_rollback, Host(), stack, state)
    assert (state / "done-rollback").exists()


def test_capture_checks_the_user_tables_again_after_the_stop(tmp_path: Path, stack: Any) -> None:
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    host = Host()
    host.counts["oauth_tokens"] = "1"
    with pytest.raises(reset.ResetError, match="oauth_tokens"):
        run_stage(reset.stage_capture, host, stack, directory)
    assert list(directory.iterdir()) == []


# --- the ledger poll ---


def started(state: Path) -> None:
    (state / "done-bootstrap").write_text("")


def test_start_turns_the_poll_off_for_the_up_and_leaves_env_alone(state: Path, stack: Any) -> None:
    started(state)
    host = Host()
    run_stage(reset.stage_start, host, stack, state)
    up = next(call for call in host.calls if "tableinator" in call and "up" in call)
    assert up[:2] == ["env", "IDENTITY_LEDGER_REFRESH_ENABLED=false"]
    assert (state / "done-start").exists()


def test_start_stops_if_the_poll_is_still_on(state: Path, stack: Any) -> None:
    started(state)
    host = Host()
    host.poll = "true"
    with pytest.raises(reset.ResetError, match="partial catalog"):
        run_stage(reset.stage_start, host, stack, state)
    assert not (state / "done-start").exists()


def after_load_host(state: Path) -> Host:
    (state / "done-start").write_text("")
    host = Host()
    host.poll = "true"
    host.counts["action = 'identity.reattach.auto'"] = "1"
    return host


def test_after_load_recreates_api_with_the_poll_on_and_then_starts_insights(state: Path, stack: Any) -> None:
    host = after_load_host(state)
    run_stage(reset.stage_after_load, host, stack, state)
    text = host.text()
    assert text.index("--force-recreate api") < text.index("--wait-timeout 600 insights")
    assert not [call for call in host.calls if call[0] == "env"]


def test_after_load_stops_if_the_poll_did_not_come_back(state: Path, stack: Any) -> None:
    host = after_load_host(state)
    host.poll = "false"
    with pytest.raises(reset.ResetError, match="did not come back"):
        run_stage(reset.stage_after_load, host, stack, state)


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [("load", "0", "no complete Discogs load"), ("complete", "0", "no complete extraction"), ("handled", "0", "has not finished")],
)
def test_after_load_waits_for_the_load_the_graph_and_the_watcher(state: Path, stack: Any, name: str, value: str, match: str) -> None:
    host = after_load_host(state)
    if name == "complete":
        host.complete = value
    elif name == "handled":
        host.counts["action = 'identity.reattach.auto'"] = value
    else:
        host.counts[name] = value
    with pytest.raises(reset.ResetError, match=match):
        run_stage(reset.stage_after_load, host, stack, state)
    assert "compose up" not in host.text()


def test_after_load_needs_start(state: Path, stack: Any) -> None:
    with pytest.raises(reset.ResetError, match="have not completed: start"):
        run_stage(reset.stage_after_load, Host(), stack, state)


# --- durable state ---


def test_state_is_replaced_whole_and_leaves_no_temporary(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    reset.write_private(path, "one")
    reset.write_private(path, "two")
    assert path.read_text() == "two"
    assert [item.name for item in tmp_path.iterdir()] == ["capture.json"]


def test_state_is_synced_before_it_replaces_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(reset.os, "fsync", synced.append)
    reset.write_private(tmp_path / "capture.json", "x")
    assert len(synced) == 2


def test_status_reports_free_disk(stack: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host()
    original = host.answer
    monkeypatch.setattr(host, "answer", lambda argv, *rest: "2147483648\n" if "df -Pk" in " ".join(argv) else original(argv, *rest))
    namespace = arguments(None)
    namespace.duration = 0
    namespace.interval = 1
    reset.stage_status(host, stack, namespace)
    assert "free=2048GB" in capsys.readouterr().out


# --- bootstrap verification ---


def verified_host() -> Host:
    host = Host()
    host.counts["public.users"] = "1"
    return host


def test_bootstrap_verification_stops_on_a_wrong_row_count(state: Path, stack: Any) -> None:
    host = Host()
    host.counts["public.users"] = "5"
    with pytest.raises(reset.ResetError, match="expected 1 and 0"):
        reset.verify_bootstrap(host, stack, load_capture(state))


def test_bootstrap_verification_stops_on_queued_messages(state: Path, stack: Any) -> None:
    host = verified_host()
    host.queues = "groovemap-discogs-tableinator-releases\t3\t0\t0\n"
    with pytest.raises(reset.ResetError, match="catalog queues"):
        reset.verify_bootstrap(host, stack, load_capture(state))


def test_bootstrap_verification_stops_on_changed_download_metadata(state: Path, stack: Any) -> None:
    capture = {**load_capture(state), "metadata_sha256": "other"}
    with pytest.raises(reset.ResetError, match="discogs_metadata"):
        reset.verify_bootstrap(verified_host(), stack, capture)


def test_a_second_capture_is_refused(state: Path, stack: Any) -> None:
    with pytest.raises(reset.ResetError, match="already holds a capture"):
        run_stage(reset.stage_capture, Host(), stack, state)


def test_capture_is_refused_once_the_reset_has_started(tmp_path: Path, stack: Any) -> None:
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    host = Host()
    swapped(host)
    with pytest.raises(reset.ResetError, match="the reset has started"):
        run_stage(reset.stage_capture, host, stack, directory)


def test_capture_refuses_an_empty_users_table(tmp_path: Path, stack: Any) -> None:
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    host = Host()
    host.counts["users_csv"] = "id\n"
    with pytest.raises(reset.ResetError, match="refusing to capture nothing"):
        run_stage(reset.stage_capture, host, stack, directory)


def test_capture_without_apply_writes_nothing(tmp_path: Path, stack: Any) -> None:
    directory = tmp_path / "s"
    directory.mkdir(mode=0o700)
    run_stage(reset.stage_capture, Host(apply=False), stack, directory)
    assert list(directory.iterdir()) == []


def test_a_consumer_still_attached_stops_the_purge() -> None:
    host = Host()
    host.queues = "groovemap-discogs-tableinator-releases\t5\t0\t1\n"
    with pytest.raises(reset.ResetError, match="consumer"):
        reset.purge_queues(host)
    assert "purge_queue" not in host.text()


def test_an_external_valkey_is_refused() -> None:
    with pytest.raises(reset.ResetError, match="external"):
        reset.clear_valkey(Host(), reset.Stack("g", set(), "v", "d", "i", True))


def test_valkey_keys_that_survive_unlink_stop_the_stage(stack: Any) -> None:
    host = Host()
    host.valkey = "groovemap:explore:x\n"
    with pytest.raises(reset.ResetError, match="survived"):
        reset.clear_valkey(host, stack)


def test_a_row_in_a_user_table_found_in_the_catalog_stops_preflight() -> None:
    host = Host()
    host.counts["oauth_tokens"] = "1"
    with pytest.raises(reset.ResetError, match="oauth_tokens"):
        reset.require_empty_user_tables(host)


def test_the_kept_and_rebuilt_user_tables_do_not_stop_preflight() -> None:
    host = Host()
    host.counts.update({"admin_audit_log": "9", "extraction_history": "9"})
    reset.require_empty_user_tables(host)


@pytest.mark.parametrize(("name", "value", "match"), [("marker", "1", "handled marker"), ("nodes", "7", "Neo4j holds")])
def test_bootstrap_verification_stops_on_leftovers(state: Path, stack: Any, name: str, value: str, match: str) -> None:
    host = Host()
    host.counts["public.users"] = "1"
    if name == "marker":
        host.counts["marker"] = value
    else:
        host.nodes = value
    with pytest.raises(reset.ResetError, match=match):
        reset.verify_bootstrap(host, stack, load_capture(state))


def load_capture(directory: Path) -> dict[str, Any]:
    return dict(json.loads((directory / "capture.json").read_text()))


def test_bootstrap_verification_stops_on_a_catalog_table_with_rows(state: Path, stack: Any) -> None:
    host = Host()
    host.counts.update({"public.users": "1", "public.releases": "t"})
    with pytest.raises(reset.ResetError, match=r"public\.releases is not empty"):
        reset.verify_bootstrap(host, stack, load_capture(state))


def test_bootstrap_needs_every_reset_stage(state: Path, stack: Any) -> None:
    (state / "done-reset-postgres").write_text("")
    host = Host()
    with pytest.raises(reset.ResetError, match="have not completed: reset-neo4j"):
        run_stage(reset.stage_bootstrap, host, stack, state)
    assert "schema-init" not in host.text()


def test_start_needs_bootstrap(state: Path, stack: Any) -> None:
    host = Host()
    with pytest.raises(reset.ResetError, match="have not completed: bootstrap"):
        run_stage(reset.stage_start, host, stack, state)
    assert "compose up" not in host.text()


def test_start_refuses_a_stray_insights_container(state: Path, stack: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / "done-bootstrap").write_text("")
    host = Host()
    original = host.answer
    monkeypatch.setattr(host, "answer", lambda argv, *rest: "abc\n" if "ps" in argv else original(argv, *rest))
    with pytest.raises(reset.ResetError, match="insights"):
        run_stage(reset.stage_start, host, stack, state)


def test_a_timed_out_copy_removes_its_container(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host()

    def refuse(*_: Any, **__: Any) -> str:
        raise reset.ResetError("timed out")

    monkeypatch.setattr(host, "mutate", refuse)
    with pytest.raises(reset.ResetError):
        host.utility("x", timeout=1, image="i", mutate=True)
    assert "rm --force groovemap-catalog-reset-copy" in host.text()


# --- the runbook ----------------------------------------------------------------------------


def test_the_runbook_names_every_destructive_statement_verbatim() -> None:
    text = RUNBOOK.read_text()
    for name, sql in DESTRUCTIVE_SQL.items():
        for statement in statements(sql):
            assert statement in text, f"{name}: {statement}"


def test_the_runbook_covers_every_stage_in_order() -> None:
    text = RUNBOOK.read_text()
    positions = [
        text.index(f"catalog-reset {stage}")
        for stage in (
            "preflight",
            "stop",
            "capture",
            "reset-postgres",
            "reset-neo4j",
            "reset-rabbitmq",
            "reset-valkey",
            "bootstrap",
            "start",
            "status",
            "after-load",
            "finish",
        )
    ]
    assert positions == sorted(positions)
    assert "catalog-reset rollback" in text


def test_the_runbook_is_linked_from_the_index_and_maintenance() -> None:
    assert "catalog-reset.md" in (ROOT / "docs" / "README.md").read_text()
    assert "catalog-reset.md" in (ROOT / "docs" / "maintenance.md").read_text()


# --- the rehearsal's dump -------------------------------------------------------------------


def _rehearsal() -> Any:
    name = "rehearse_catalog_reset"
    path = ROOT / "scripts" / f"{name}.py"
    module_spec = importlib.util.spec_from_file_location(name, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


def test_the_dump_is_a_complete_month_with_published_checksums() -> None:
    files = _rehearsal().dump_files()
    assert len(files) == 5
    checksums = dict(reversed(line.split("  ")) for line in files["discogs_20261101_CHECKSUM.txt"].decode().splitlines())
    assert set(checksums) == {name for name in files if "CHECKSUM" not in name}
    for name, expected in checksums.items():
        assert hashlib.sha256(files[name]).hexdigest() == expected
        assert gzip.decompress(files[name]).startswith(b"<")


def test_the_dump_is_deterministic() -> None:
    module = _rehearsal()
    assert module.dump_files() == module.dump_files()


def test_the_rehearsal_override_publishes_nothing() -> None:
    text = _rehearsal().override("net", "172.31.0.0/16", "/rehearsal/tls")
    assert "ports: !override []" in text
    assert "data.discogs.com" in text


def test_the_tool_needs_no_third_party_module() -> None:
    imports = {node.names[0].name.split(".")[0] for node in ast.walk(module_tree()) if isinstance(node, ast.Import)}
    froms = {node.module.split(".")[0] for node in ast.walk(module_tree()) if isinstance(node, ast.ImportFrom) and node.module}
    assert (imports | froms) <= set(sys.stdlib_module_names)


def test_the_help_runs_without_docker_state() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=True, timeout=30)
    assert "preflight" in result.stdout
