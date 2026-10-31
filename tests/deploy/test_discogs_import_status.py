"""Regression tests for the Discogs-import-complete gate (`just discogs-import-status`).

The gate decides when the MusicBrainz path may start, so the property that matters most is
that it never reports complete on uncertainty. Everything here runs without containers:
the decision logic is pure, and the probes run against a fake `docker compose` runner whose
answers are the exact shapes psql, cypher-shell, and rabbitmqctl produced when the queries
were run against the pinned images.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> Any:
    """Import the gate by path — `scripts/` is a script directory, not a package."""
    spec = importlib.util.spec_from_file_location("discogs_import_status", REPO_ROOT / "scripts" / "discogs_import_status.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()
Status = gate.Status

ALL = ["artists", "labels", "masters", "releases"]


def latch_row(
    version: str, signals: list[str], refreshed: bool, created: str, job_state: str | None = None, job_error: str | None = None
) -> dict[str, Any]:
    return {
        "version": version,
        "signals": signals,
        "refreshed_at": f"{created}T03:00:00.5+00:00" if refreshed else None,
        "created_at": f"{created}T01:25:43.898051+00:00",
        "job_state": job_state,
        "job_error": job_error,
    }


def latch_output(*rows: dict[str, Any]) -> str:
    # psql's json_agg rendering, as captured from postgres:18.
    return "[" + ", \n ".join(json.dumps(row, separators=(", ", " : ")) for row in rows) + "]\n" if rows else "[]\n"


def graph_output(*nodes: tuple[str, list[str]]) -> str:
    # cypher-shell --format plain, as captured from the pinned neo4j image; no rows prints nothing.
    if not nodes:
        return ""
    return "version, signals\n" + "".join(f'"{version}", [{", ".join(json.dumps(s) for s in signals)}]\n' for version, signals in nodes)


def queue_output(depths: dict[str, tuple[int, int, int]] | None = None, *, dlq: dict[str, int] | None = None, omit: tuple[str, ...] = ()) -> str:
    # rabbitmqctl --quiet --formatter json list_queues, as captured from rabbitmq:4-management.
    depths = depths or {}
    rows = []
    for name in gate.CONSUMER_QUEUES:
        if name in omit:
            continue
        ready, unacked, consumers = depths.get(name, (0, 0, 0))
        rows.append({"name": name, "messages_unacknowledged": unacked, "consumers": consumers, "messages_ready": ready})
    for name, count in (dlq or {}).items():
        rows.append({"name": name, "messages_unacknowledged": 0, "consumers": 0, "messages_ready": count})
    rows.append({"name": "groovemap-musicbrainz-brainztableinator-releases", "messages_unacknowledged": 0, "consumers": 1, "messages_ready": 9})
    return "[\n" + "\n,".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n]\n"


COMPLETE_LATCH = latch_output(latch_row("20261001", ALL, True, "2026-10-01", "completed"))
COMPLETE_GRAPH = graph_output(("20261001", ALL))
IDLE_QUEUES = queue_output()


class FakeDocker:
    """Answers `docker compose ... exec -T <service> ...` the way each container would."""

    def __init__(
        self,
        relations: str = "t|t\n",
        latch: str = COMPLETE_LATCH,
        graph: str = COMPLETE_GRAPH,
        queues: str = IDLE_QUEUES,
        down: tuple[str, ...] = (),
    ) -> None:
        self.relations = relations
        self.latch = latch
        self.graph = graph
        self.queues = queues
        self.down = down
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is False and kwargs["capture_output"] is True and kwargs["cwd"] == gate.ROOT
        self.calls.append(argv)
        service = argv[argv.index("-T") + 1]
        if service in self.down:
            return subprocess.CompletedProcess(argv, 1, "", f'service "{service}" is not running\n')
        if service == "postgres":
            sql = argv[-1]
            return subprocess.CompletedProcess(argv, 0, self.relations if "to_regclass" in sql else self.latch, "")
        if service == "neo4j":
            return subprocess.CompletedProcess(argv, 0, self.graph, "")
        if service == "rabbitmq":
            return subprocess.CompletedProcess(argv, 0, self.queues, "")
        raise AssertionError(f"unexpected service {service}")


def report_for(fake: FakeDocker) -> Any:
    return gate.evaluate(gate.observe(gate.Compose(runner=fake)))


def signal(report: Any, name: str) -> Any:
    return next(s for s in report.signals if s.name == name)


# --- the verdicts ------------------------------------------------------------------------


def test_every_signal_holding_for_one_version_is_complete() -> None:
    report = report_for(FakeDocker())
    assert report.status is Status.COMPLETE
    assert report.version == "20261001"
    assert all(s.status is Status.COMPLETE for s in report.signals)


def test_queues_idle_with_no_consumers_still_count_as_drained() -> None:
    # Both consumers cancel their consumers CONSUMER_CANCEL_DELAY after finishing, so a
    # finished import normally has none. The durable latches, not the consumer count, are
    # what rule out "idle because nothing ran".
    report = report_for(FakeDocker(queues=queue_output()))
    queues = signal(report, "rabbitmq queues")
    assert queues.status is Status.COMPLETE
    assert "0 consumer(s)" in queues.detail and "detached" in queues.detail
    assert report.status is Status.COMPLETE


def test_idle_queues_without_any_latch_are_not_complete() -> None:
    report = report_for(FakeDocker(latch=latch_output(), graph=graph_output()))
    assert signal(report, "rabbitmq queues").status is Status.COMPLETE
    assert report.status is Status.IN_PROGRESS
    assert report.version is None


def test_a_backlog_with_no_consumer_is_in_progress_and_called_out() -> None:
    stalled = "groovemap-discogs-tableinator-releases"
    report = report_for(FakeDocker(queues=queue_output({stalled: (1200, 0, 0), "groovemap-discogs-graphinator-releases": (5, 50, 1)})))
    queues = signal(report, "rabbitmq queues")
    assert queues.status is Status.IN_PROGRESS
    assert queues.summary == "ready=1205 unacked=50"
    assert f"no consumer attached to {stalled}" in queues.detail
    assert "graphinator-releases" not in queues.detail.split("no consumer attached to")[1]
    assert report.status is Status.IN_PROGRESS


def test_stale_complete_rows_from_a_previous_dump_do_not_count() -> None:
    latch = latch_output(
        latch_row("20261001", ALL, True, "2026-10-01", "completed"),
        latch_row("20261101", ["artists", "labels"], False, "2026-11-01"),
    )
    graph = graph_output(("20261001", ALL), ("20261101", ["artists"]))
    report = report_for(FakeDocker(latch=latch, graph=graph))
    assert report.status is Status.IN_PROGRESS
    assert report.version == "20261101"
    sql = signal(report, "postgres latch")
    assert sql.status is Status.IN_PROGRESS and sql.summary == "2/4"
    assert "waiting on masters, releases" in sql.detail
    assert "previous extraction 20261001 is complete and no longer counts" in sql.detail
    assert signal(report, "neo4j latch").summary == "1/4"


def test_latest_version_is_ordered_by_dump_version_not_arrival() -> None:
    # The durable loader can insert a superseded straggler's row after a newer dump started.
    latch = latch_output(
        latch_row("20261101", ALL, True, "2026-11-01", "completed"),
        latch_row("20261001", ["releases"], False, "2026-11-02"),
    )
    report = report_for(FakeDocker(latch=latch, graph=graph_output(("20261101", ALL))))
    assert report.version == "20261101"
    assert report.status is Status.COMPLETE


def test_legacy_versionless_rows_fall_back_to_creation_order() -> None:
    rows = gate.parse_latch(
        latch_output(
            latch_row("2026-09-01T00:00:00Z", ALL, True, "2026-09-01"),
            latch_row("2026-10-01T00:00:00Z", ["artists"], False, "2026-10-01"),
        )
    )
    assert gate.latest_latch(rows).version == "2026-10-01T00:00:00Z"
    assert gate.latest_latch(()) is None


def test_four_signals_without_the_refresh_stamp_are_in_progress() -> None:
    latch = latch_output(latch_row("20261001", ALL, False, "2026-10-01", "retry", "deadlock detected"))
    report = report_for(FakeDocker(latch=latch))
    assert signal(report, "postgres latch").status is Status.COMPLETE
    refresh = signal(report, "postgres refresh")
    assert refresh.status is Status.IN_PROGRESS
    assert refresh.summary == "retry"
    assert "refresh job: retry" in refresh.detail and "last error: deadlock detected" in refresh.detail
    assert report.status is Status.IN_PROGRESS


def test_without_the_refresh_job_relation_the_latch_alone_is_read() -> None:
    fake = FakeDocker(relations="t|f\n", latch=latch_output(latch_row("20261001", ALL, False, "2026-10-01")))
    report = report_for(fake)
    latch_query = fake.calls[1][-1]
    assert "loader_derived_refresh_job" not in latch_query
    assert signal(report, "postgres refresh").detail == "20261001: derived-relation refresh not yet stamped"
    assert report.status is Status.IN_PROGRESS


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        (graph_output(("20261001", ALL), ("20261101", ["artists"])), "version mismatch: graph has started 20261101, PostgreSQL's newest is 20261001"),
        (graph_output(("20260901", ALL)), "version mismatch: no ExtractionCompletion for 20261001 (newest graph version: 20260901)"),
        (graph_output(("unknown", ALL)), "version mismatch: no ExtractionCompletion for 20261001"),
        (graph_output(), "version mismatch: no ExtractionCompletion for 20261001"),
        (graph_output(("20261001", ["artists", "labels", "masters"])), "20261001: signalled artists, labels, masters; waiting on releases"),
    ],
)
def test_a_graph_latch_that_does_not_match_the_sql_version_is_in_progress(graph: str, expected: str) -> None:
    report = report_for(FakeDocker(graph=graph))
    neo4j = signal(report, "neo4j latch")
    assert neo4j.status is Status.IN_PROGRESS
    assert neo4j.detail == expected
    assert report.status is Status.IN_PROGRESS


def test_sql_ahead_of_the_graph_is_in_progress() -> None:
    latch = latch_output(latch_row("20261001", ALL, True, "2026-10-01"), latch_row("20261101", ALL, True, "2026-11-01"))
    report = report_for(FakeDocker(latch=latch, graph=graph_output(("20261001", ALL))))
    assert report.version == "20261101"
    assert signal(report, "neo4j latch").summary == "absent"
    assert report.status is Status.IN_PROGRESS


def test_a_missing_relation_cannot_be_determined() -> None:
    report = report_for(FakeDocker(relations="f|f\n"))
    assert report.status is Status.UNKNOWN
    assert "public.loader_extraction_latch does not exist" in signal(report, "postgres latch").detail
    assert signal(report, "neo4j latch").status is Status.UNKNOWN


@pytest.mark.parametrize("service", ["postgres", "neo4j", "rabbitmq"])
def test_any_service_down_cannot_be_determined(service: str) -> None:
    report = report_for(FakeDocker(down=(service,)))
    assert report.status is Status.UNKNOWN
    assert any(f'service "{service}" is not running' in s.detail for s in report.signals)


def test_unknown_outranks_a_definite_in_progress() -> None:
    latch = latch_output(latch_row("20261001", ["artists"], False, "2026-10-01"))
    report = report_for(FakeDocker(latch=latch, down=("rabbitmq",)))
    assert signal(report, "postgres latch").status is Status.IN_PROGRESS
    assert report.status is Status.UNKNOWN


def test_an_undeclared_consumer_queue_cannot_be_determined() -> None:
    report = report_for(FakeDocker(queues=queue_output(omit=("groovemap-discogs-graphinator-masters",))))
    queues = signal(report, "rabbitmq queues")
    assert queues.status is Status.UNKNOWN
    assert "groovemap-discogs-graphinator-masters" in queues.detail
    assert report.status is Status.UNKNOWN


def test_dead_lettered_messages_warn_but_do_not_gate() -> None:
    report = report_for(
        FakeDocker(queues=queue_output(dlq={"groovemap-discogs-tableinator-releases.dlq": 3, "groovemap-discogs-graphinator-labels.dlq": 0}))
    )
    assert report.status is Status.COMPLETE
    assert report.warnings == ("groovemap-discogs-tableinator-releases.dlq holds 3 dead-lettered message(s); they will not be retried",)


@pytest.mark.parametrize(
    ("field", "output"),
    [
        ("latch", "ERROR:  relation does not exist\n"),
        ("latch", '{"version": "20261001"}'),
        ("latch", '[{"version": "20261001", "signals": "artists", "refreshed_at": null, "created_at": "2026-10-01T00:00:00+00:00"}]'),
        ("latch", '[{"version": "20261001", "signals": [], "refreshed_at": null, "created_at": "yesterday"}]'),
        ("relations", "t\n"),
        ("graph", "m\n"),
        ("graph", 'version, signals\n"20261001", NULL\n'),
        ("graph", "version, signals\n20261001, [artists]\n"),
        ("queues", "Listing queues for vhost / ...\n"),
        ("queues", '{"name": "x"}'),
        ("queues", json.dumps([{"name": gate.CONSUMER_QUEUES[0], "messages_ready": "", "messages_unacknowledged": 0, "consumers": 0}])),
    ],
)
def test_unreadable_output_cannot_be_determined(field: str, output: str) -> None:
    fake = FakeDocker()
    setattr(fake, field, output)
    report = report_for(fake)
    assert report.status is Status.UNKNOWN


def test_a_non_consumer_queue_without_depth_is_ignored() -> None:
    rows = [*json.loads(IDLE_QUEUES), {"name": "some-classic-queue", "messages_ready": "", "messages_unacknowledged": "", "consumers": ""}]
    assert len(gate.parse_queues(json.dumps(rows))) == len(gate.CONSUMER_QUEUES) + 1


def test_uncertainty_never_reports_complete() -> None:
    complete = gate.observe(gate.Compose(runner=FakeDocker()))
    assert gate.evaluate(complete).status is Status.COMPLETE
    for store in ("latch", "graph", "queues"):
        broken = replace(complete, **{store: None, f"{store}_error": "boom"})
        assert gate.evaluate(broken).status is Status.UNKNOWN
    assert gate.evaluate(gate.Observation()).status is Status.UNKNOWN


# --- probing -----------------------------------------------------------------------------


def test_probes_only_exec_into_running_containers() -> None:
    fake = FakeDocker()
    report_for(fake)
    assert [call[:4] for call in fake.calls] == [["docker", "compose", "exec", "-T"]] * 4
    assert [call[4] for call in fake.calls] == ["postgres", "postgres", "neo4j", "rabbitmq"]
    assert fake.calls[3][5:] == list(gate.RABBITMQCTL)
    for call in fake.calls:
        assert not {"up", "run", "start", "restart", "down", "stop"} & set(call[:5])


def test_compose_file_selection_matches_config_prod() -> None:
    args = gate.parse_args(["--prod", "-f", "extra.yml", "--env-file", "prod.env", "-p", "groovemap"])
    files = gate.compose_files(args)
    assert files[:2] == ["docker-compose.yml", "docker-compose.prod.yml"]
    assert files[2] == str(Path("extra.yml").resolve())
    fake = FakeDocker()
    gate.observe(gate.Compose(files, args.env_file, args.project_name, runner=fake))
    assert fake.calls[0][:12] == [
        "docker",
        "compose",
        "--project-name",
        "groovemap",
        "--env-file",
        "prod.env",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.prod.yml",
        "-f",
        str(Path("extra.yml").resolve()),
    ]
    assert gate.compose_files(gate.parse_args([])) == []


def test_justfile_config_prod_composes_the_same_files() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    assert "docker compose " + " ".join(f"-f {name}" for name in gate.PROD_COMPOSE_FILES) + " config" in justfile


def test_docker_missing_or_hanging_cannot_be_determined() -> None:
    def missing(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    def hanging(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    for runner in (missing, hanging):
        report = gate.evaluate(gate.observe(gate.Compose(runner=runner)))
        assert report.status is Status.UNKNOWN
    assert "docker is not installed" in signal(gate.evaluate(gate.observe(gate.Compose(runner=missing))), "postgres latch").detail


def _fake_bin(tmp_path: Path, name: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\necho "NEO4J_PASSWORD=${NEO4J_PASSWORD:-}"\n')
    tool.chmod(0o755)
    return bin_dir


def _run_shell(script: str, query: str, env: dict[str, str], bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", script, "sh", query],
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_psql_reads_the_base_user_or_the_production_secret_file(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, "psql")
    base = _run_shell(gate.PSQL_SHELL, "SELECT 1", {"POSTGRES_USER": "groovemap", "POSTGRES_DB": "groovemap"}, bin_dir)
    assert base.returncode == 0
    assert base.stdout.splitlines()[:9] == ["-X", "-U", "groovemap", "-d", "groovemap", "-v", "ON_ERROR_STOP=1", "-tAq", "-c"]
    assert base.stdout.splitlines()[9] == "SELECT 1"

    secret = tmp_path / "postgres_username"
    secret.write_text("prod_admin")
    prod = _run_shell(gate.PSQL_SHELL, "SELECT 1", {"POSTGRES_USER": "", "POSTGRES_USER_FILE": str(secret)}, bin_dir)
    assert prod.returncode == 0
    assert prod.stdout.splitlines()[2] == "prod_admin"

    neither = _run_shell(gate.PSQL_SHELL, "SELECT 1", {}, bin_dir)
    assert neither.returncode == 2
    assert "no PostgreSQL user" in neither.stderr


def test_cypher_shell_reads_the_secret_file_before_neo4j_auth_and_keeps_it_off_argv(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, "cypher-shell")
    base = _run_shell(gate.CYPHER_SHELL, gate.GRAPH_CYPHER, {"NEO4J_AUTH": "neo4j/groovemap"}, bin_dir)
    assert base.returncode == 0
    assert base.stdout.splitlines() == ["-u", "neo4j", "-d", "neo4j", "--format", "plain", gate.GRAPH_CYPHER, "NEO4J_PASSWORD=groovemap"]

    secret = tmp_path / "neo4j_password"
    secret.write_text("s3cret-prod")
    script = gate.CYPHER_SHELL.replace("/run/secrets/neo4j_password", str(secret))
    assert script != gate.CYPHER_SHELL
    prod = _run_shell(script, gate.GRAPH_CYPHER, {"NEO4J_AUTH": "neo4j/groovemap"}, bin_dir)
    assert prod.stdout.splitlines()[-1] == "NEO4J_PASSWORD=s3cret-prod"
    assert "s3cret-prod" not in prod.stdout.splitlines()[:-1]


# --- output and the command line ---------------------------------------------------------


def test_report_renders_every_signal_and_queue() -> None:
    report = report_for(FakeDocker(queues=queue_output(dlq={"groovemap-discogs-graphinator-releases.dlq": 1})))
    text = gate.render(report)
    assert text.splitlines()[0] == "Discogs import: COMPLETE (exit 0)"
    for name in ("extraction version", "postgres latch", "postgres refresh", "neo4j latch", "rabbitmq queues", *gate.CONSUMER_QUEUES):
        assert name in text
    assert "warning: groovemap-discogs-graphinator-releases.dlq" in text

    payload = json.loads(gate.as_json(report))
    assert payload["status"] == "complete" and payload["exit_code"] == 0 and payload["version"] == "20261001"
    assert [s["name"] for s in payload["signals"]] == ["postgres latch", "postgres refresh", "neo4j latch", "rabbitmq queues"]
    assert len(payload["queues"]) == len(gate.CONSUMER_QUEUES)


def test_summary_line_is_one_line() -> None:
    latch = latch_output(latch_row("20261001", ["artists"], False, "2026-10-01"))
    line = gate.summary_line(report_for(FakeDocker(latch=latch)), datetime(2026, 10, 30, 18, 0, tzinfo=UTC))
    assert "\n" not in line
    assert line.startswith("2026-10-30T18:00:00Z in progress [20261001]: postgres latch 1/4; postgres refresh pending; neo4j latch 4/4")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_polls_until_complete() -> None:
    observations = iter(
        [
            gate.observe(gate.Compose(runner=FakeDocker(down=("neo4j",)))),
            gate.observe(gate.Compose(runner=FakeDocker(latch=latch_output(latch_row("20261001", ALL, False, "2026-10-01"))))),
            gate.observe(gate.Compose(runner=FakeDocker())),
        ]
    )
    clock = Clock()
    out = io.StringIO()
    report = gate.watch(lambda: next(observations), 10, 0, out, clock=clock, sleep=clock.sleep)
    assert report.status is Status.COMPLETE
    assert clock.sleeps == [10, 10]
    lines = out.getvalue().splitlines()
    assert [line.split(" [")[0].split(" ", 1)[1] for line in lines] == ["cannot determine", "in progress", "complete"]


def test_wait_gives_up_at_the_timeout_with_the_last_status() -> None:
    clock = Clock()
    out = io.StringIO()
    in_progress = gate.observe(gate.Compose(runner=FakeDocker(latch=latch_output())))
    report = gate.watch(lambda: in_progress, 30, 100, out, clock=clock, sleep=clock.sleep)
    assert report.status is Status.IN_PROGRESS
    assert clock.sleeps == [30, 30, 30]
    assert out.getvalue().splitlines()[-1] == "gave up after 100s without completing"


@pytest.mark.parametrize(
    ("fake", "code"),
    [
        (FakeDocker(), 0),
        (FakeDocker(latch=latch_output(latch_row("20261101", ["artists"], False, "2026-11-01"))), 3),
        (FakeDocker(down=("postgres", "neo4j", "rabbitmq")), 2),
        (FakeDocker(relations="f|f\n"), 2),
    ],
)
def test_main_exit_codes(fake: FakeDocker, code: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main([], runner=fake) == code
    assert capsys.readouterr().out.startswith("Discogs import: ")


def test_main_json_and_wait(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--wait", "--interval", "1", "--json"], runner=FakeDocker()) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "complete"
    assert " complete [20261001]: " in captured.err


def test_interrupting_wait_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    def interrupted(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    assert gate.main(["--wait"], runner=interrupted) == 130
    assert "re-run to resume" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--interval", "0"], ["--timeout", "-1"]])
def test_bad_arguments_never_exit_zero(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        gate.parse_args(argv)
    assert raised.value.code == 2


def test_recipe_exists_and_documents_the_exit_codes() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    assert '[positional-arguments]\ndiscogs-import-status *args:\n    uv run python scripts/discogs_import_status.py "$@"' in justfile
    maintenance = (REPO_ROOT / "docs" / "maintenance.md").read_text()
    assert "just discogs-import-status --prod --wait" in maintenance
    assert "exit 0 complete, 3 in progress, 2 cannot determine" in maintenance
