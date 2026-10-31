"""Regression tests for `just status` (scripts/status.py): a read-only summary of the deployment.

Nothing here starts a container. A fake `docker` answers `ps`, `inspect` and the Discogs gate's
`compose exec` probes, one scenario per state an operator meets: a fresh host, phase 1 importing,
phase 1 finished, phase 2, an unhealthy service and an unreadable gate or Docker.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.deploy.test_discogs_import_status import COMPLETE_GRAPH, COMPLETE_LATCH, IDLE_QUEUES, FakeDocker, graph_output, latch_output, latch_row


if TYPE_CHECKING:
    from collections.abc import Iterable


ROOT = Path(__file__).resolve().parents[2]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("status_script", ROOT / "scripts" / "status.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


status = _load()
policy = status.helpers.load_image_policy()
ALL = ["artists", "labels", "masters", "releases"]
IN_PROGRESS_LATCH = latch_output(latch_row("20261001", ["artists", "labels"], False, "2026-10-01"))
IN_PROGRESS_GRAPH = graph_output(("20261001", ["artists"]))
CORE = (
    "rabbitmq",
    "postgres",
    "neo4j",
    "valkey",
    "grafana",
    "schema-init",
    "api",
    "extractor-discogs",
    "graphinator",
    "tableinator",
    "dashboard",
    "explore",
    "insights",
)
MUSICBRAINZ = status.MUSICBRAINZ_SERVICES


def container(
    service: str, state: str = "running", health: str = "healthy", exit_code: int = 0, digest: str | None = None, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    """`docker inspect --format '{{json .}}'` for one container; a repository-owned image is pinned to its reviewed digest by default."""
    variable = status.SERVICES[service]
    reference = f"ghcr.io/example/{service}:1.0"
    if variable:
        reference += f"@sha256:{digest if digest is not None else policy.RELEASED_IMAGE_DIGESTS[variable]}"
    if variable and labels is None:
        labels = {status.LABEL_VERSION: policy.RELEASED_IMAGE_VERSIONS[variable], status.LABEL_REVISION: "0123456789abcdef0123456789abcdef01234567"}
    return {
        "Name": f"/groovemap-{service}",
        "State": {"Status": state, "ExitCode": exit_code, **({"Health": {"Status": health}} if health else {})},
        "Config": {"Image": reference, "Labels": labels or {}},
    }


def stack(*names: str, **overrides: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        overrides.get(name)
        or container(name, health="" if name in ("schema-init",) else "healthy", state="exited" if name == "schema-init" else "running")
        for name in names
    ]


class FakeEngine:
    """`docker ps`, `docker inspect`, and (through the gate's own fake) `docker compose ... exec`."""

    def __init__(
        self,
        containers: Iterable[dict[str, Any]] = (),
        *,
        latch: str = COMPLETE_LATCH,
        graph: str = COMPLETE_GRAPH,
        down: tuple[str, ...] = (),
        broken: str = "",
    ) -> None:
        self.containers = list(containers)
        self.broken = broken
        self.gate = FakeDocker(latch=latch, graph=graph, queues=IDLE_QUEUES, down=down)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.broken:
            return subprocess.CompletedProcess(argv, 1, "", self.broken + "\n")
        if argv[1] == "compose":
            # An external PostgreSQL is queried through the store-check one-shot; the gate's fake knows it as postgres.
            return self.gate(["postgres" if word == "store-check-postgres" else word for word in argv], **kwargs)
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, "".join(f"{c['Name'].lstrip('/')}\n" for c in self.containers), "")
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "".join(json.dumps(c) + "\n" for c in self.containers), "")
        raise AssertionError(f"status must only read; it ran {argv}")


def run(engine: FakeEngine, tmp_path: Path, env: str = "", environ: dict[str, str] | None = None, *args: str) -> tuple[int, str, str]:
    env_path = tmp_path / ".env"
    env_path.write_text(env)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = status.main(list(args), runner=engine, environ=environ or {}, env_path=env_path)
    return code, out.getvalue(), err.getvalue()


def line(output: str, service: str) -> str:
    return next(row for row in output.splitlines() if row[2:].startswith(service + " "))


# --- the states an operator meets ---------------------------------------------------------


def test_a_fresh_host_is_not_installed_and_exits_zero(tmp_path: Path) -> None:
    code, out, _ = run(FakeEngine(), tmp_path)
    assert code == 0
    assert "not installed: nothing of GrooveMap exists" in out
    assert "Discogs gate: NOT STARTED" in out
    assert "not installed" in line(out, "api")
    assert "not installed" in line(out, "extractor-musicbrainz")
    assert "waiting-on-gate" not in out
    assert "MusicBrainz sizing: unmeasured (gm-deployment-vc5s)" in out


def test_phase_one_importing_discogs_with_musicbrainz_waiting_on_the_gate(tmp_path: Path) -> None:
    engine = FakeEngine(stack(*CORE), latch=IN_PROGRESS_LATCH, graph=IN_PROGRESS_GRAPH)
    code, out, _ = run(engine, tmp_path)
    assert code == 0
    assert "phase 1: importing Discogs; MusicBrainz waits on the gate" in out
    assert "Discogs gate: IN PROGRESS (exit 3)" in out
    for service in status.DISCOGS_SERVICES:
        assert "installed, healthy, importing" in line(out, service)
    assert "installed, healthy" in line(out, "api")
    assert "importing" not in line(out, "api")
    for service in MUSICBRAINZ:
        assert "waiting-on-gate" in line(out, service)
    assert "installed, completed" in line(out, "schema-init")
    assert "!" not in "".join(row[:1] for row in out.splitlines() if row.startswith(" !"))  # no problem rows


def test_a_complete_gate_with_musicbrainz_not_yet_enabled_says_to_run_deploy(tmp_path: Path) -> None:
    code, out, _ = run(FakeEngine(stack(*CORE)), tmp_path)
    assert code == 0
    assert "Discogs gate: COMPLETE (exit 0)" in out
    assert "phase 1 finished" in out
    assert "not enabled" in line(out, "brainzgraphinator") and "run `just deploy`" in line(out, "brainzgraphinator")
    assert "installed, healthy" in line(out, "extractor-discogs") and "importing" not in line(out, "extractor-discogs")


def test_phase_two_has_the_musicbrainz_services_importing(tmp_path: Path) -> None:
    engine = FakeEngine([*stack(*CORE), *[container(name) for name in MUSICBRAINZ]])
    code, out, _ = run(engine, tmp_path, "COMPOSE_PROFILES=musicbrainz\n")
    assert code == 0
    assert "phase 2: Discogs import complete, MusicBrainz enabled" in out
    for service in MUSICBRAINZ:
        assert "installed, healthy, importing" in line(out, service)
    assert "waiting-on-gate" not in out


def test_an_unhealthy_service_is_flagged_and_status_still_exits_zero(tmp_path: Path) -> None:
    engine = FakeEngine(
        stack(*CORE, api=container("api", health="unhealthy"), grafana=container("grafana", state="exited", exit_code=137, health=""))
    )
    code, out, _ = run(engine, tmp_path)
    assert code == 0
    assert line(out, "api").startswith(" !") and "installed, unhealthy" in line(out, "api")
    assert "installed, exited (137)" in line(out, "grafana")
    assert "Attention: grafana (exited (137)), api (unhealthy)" in out


def test_a_starting_container_and_one_without_a_healthcheck(tmp_path: Path) -> None:
    engine = FakeEngine(stack(*CORE, explore=container("explore", health="starting"), valkey=container("valkey", health="")))
    _, out, _ = run(engine, tmp_path)
    assert "installed, starting" in line(out, "explore") and line(out, "explore").startswith("  ")
    assert "installed, running (no healthcheck)" in line(out, "valkey")


# --- exit codes: 2 when the state could not be determined ----------------------------------


def test_docker_that_cannot_be_asked_exits_two(tmp_path: Path) -> None:
    code, out, err = run(FakeEngine(broken="Cannot connect to the Docker daemon"), tmp_path)
    assert code == 2 and out == ""
    assert "cannot determine: Docker could not be asked: Cannot connect to the Docker daemon" in err


def test_a_missing_docker_binary_exits_two(tmp_path: Path) -> None:
    def missing(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    code, _, err = run(missing, tmp_path)  # type: ignore[arg-type]
    assert code == 2 and "not installed or not on PATH" in err


def test_a_gate_that_cannot_be_read_on_an_installed_stack_exits_two_but_still_reports(tmp_path: Path) -> None:
    code, out, _ = run(FakeEngine(stack(*CORE), down=("neo4j",)), tmp_path)
    assert code == 2
    assert "Discogs gate: CANNOT DETERMINE (exit 2)" in out
    assert "the phase cannot be told" in out
    assert "installed, healthy" in line(out, "api")


def test_a_stopped_stack_container_means_the_gate_cannot_be_read(tmp_path: Path) -> None:
    code, out, _ = run(FakeEngine(stack(*CORE, postgres=container("postgres", state="exited", exit_code=1, health=""))), tmp_path)
    assert code == 2
    assert "postgres is exited, so the gate cannot be read" in out


def test_an_unusable_store_setting_exits_two(tmp_path: Path) -> None:
    code, _, err = run(FakeEngine(), tmp_path, "GROOVEMAP_POSTGRES=sometimes\n")
    assert code == 2 and "GROOVEMAP_POSTGRES" in err


# --- decisions, images, and the promise to change nothing ----------------------------------


def test_recorded_decisions_are_reported(tmp_path: Path) -> None:
    _, out, _ = run(FakeEngine(), tmp_path, "GROOVEMAP_VALKEY=local\nGROOVEMAP_MONITORING=shared\n")
    assert "PostgreSQL   local (default)" in out
    assert "Valkey       local (.env)" in out
    assert "Monitoring   shared (.env)" in out


def test_an_external_postgres_is_not_reported_as_a_missing_container(tmp_path: Path) -> None:
    containers = stack(*(name for name in CORE if name != "postgres"))
    _, out, _ = run(FakeEngine(containers), tmp_path, "GROOVEMAP_POSTGRES=external\n")
    assert "PostgreSQL   external (.env)" in out
    assert "external  - PostgreSQL is not a container of this stack" in line(out, "postgres-exporter")


def test_an_external_stores_completed_check_is_a_one_shot_not_a_problem(tmp_path: Path) -> None:
    def check(exit_code: int) -> dict[str, Any]:
        return {
            "Name": "/groovemap-store-check-postgres",
            "State": {"Status": "exited", "ExitCode": exit_code},
            "Config": {"Image": "postgres:18-alpine"},
        }

    containers = stack(*(name for name in CORE if name != "postgres"))
    _, out, _ = run(FakeEngine([*containers, check(0)]), tmp_path, "GROOVEMAP_POSTGRES=external\n")
    assert line(out, "store-check-postgres").startswith("  store-check-postgres") and "installed, completed" in line(out, "store-check-postgres")
    assert "Attention" not in out
    _, out, _ = run(FakeEngine([*containers, check(1)]), tmp_path, "GROOVEMAP_POSTGRES=external\n")
    assert line(out, "store-check-postgres").startswith(" !") and "installed, exited (1)" in line(out, "store-check-postgres")


def test_reviewed_images_show_their_release_and_oci_labels(tmp_path: Path) -> None:
    _, out, _ = run(FakeEngine(stack(*CORE)), tmp_path)
    version = policy.RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]
    digest = policy.RELEASED_IMAGE_DIGESTS["CATALOG_API_IMAGE"][:12]
    assert f"catalog-api: reviewed v{version} (sha256:{digest}); label version {version}, source revision 0123456789ab" in out
    assert not any(row.startswith(" !") for row in out.splitlines())
    assert "Source merged to a repository after its release is not deployed until that release is promoted" in out


def test_an_image_that_is_not_the_reviewed_digest_is_flagged_with_its_labels(tmp_path: Path) -> None:
    newer = "f" * 64
    api = container("api", digest=newer, labels={status.LABEL_VERSION: "0.7.0", status.LABEL_REVISION: "abcdef0123456789"})
    _, out, _ = run(FakeEngine(stack(*CORE, api=api)), tmp_path)
    text = next(row for row in out.splitlines() if row.startswith(" !api") and "catalog-api" in row)
    assert f"NOT the reviewed release v{policy.RELEASED_IMAGE_VERSIONS['CATALOG_API_IMAGE']}: running sha256:{'f' * 12}" in text
    assert "label version 0.7.0, source revision abcdef012345" in text
    assert "Attention: not the reviewed release: api" in out


def test_a_digest_that_is_another_services_release_is_called_out(tmp_path: Path) -> None:
    swapped = container("api", digest=policy.RELEASED_IMAGE_DIGESTS["GRAPH_EXPLORER_IMAGE"])
    _, out, _ = run(FakeEngine(stack(*CORE, api=swapped)), tmp_path)
    assert "that digest is the reviewed graph-explorer, not this service's image" in out


def test_an_image_without_a_pinned_digest_is_flagged(tmp_path: Path) -> None:
    unpinned = container("api")
    unpinned["Config"]["Image"] = "ghcr.io/example/api:latest"
    _, out, _ = run(FakeEngine(stack(*CORE, api=unpinned)), tmp_path)
    assert "catalog-api: NOT a pinned digest (ghcr.io/example/api:latest)" in out


def test_status_only_reads(tmp_path: Path) -> None:
    engine = FakeEngine(stack(*CORE), latch=IN_PROGRESS_LATCH, graph=IN_PROGRESS_GRAPH)
    run(engine, tmp_path, "GROOVEMAP_POSTGRES=local\n")
    run(engine, tmp_path, "GROOVEMAP_POSTGRES=external\n")
    verbs = {
        call[1] if call[1] != "compose" else f"compose {call[call.index('exec') if 'exec' in call else call.index('run')]}" for call in engine.calls
    }
    assert verbs <= {"ps", "inspect", "compose exec", "compose run"}, verbs
    assert not any(word in call for call in engine.calls for word in ("pull", "up", "start", "stop", "rm", "down"))
