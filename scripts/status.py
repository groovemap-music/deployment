"""Say where this GrooveMap deployment stands, without changing anything (`just status`).

For each service: installed (its container exists), healthy (its health status), importing (the
Discogs services while the Discogs gate reports in progress, the MusicBrainz services while they
run) or waiting-on-gate (MusicBrainz is not enabled yet and the Discogs gate is not complete).
Then the Discogs gate (scripts/discogs_import_status.py, same 0/3/2 semantics), the decisions
recorded in .env, and for every running repository-owned image the release it is: its digest
mapped to the reviewed release table (RELEASED_IMAGE_VERSIONS in scripts/check-images.py) beside
the OCI labels (org.opencontainers.image.version / .revision) read with `docker inspect`. An
image that is not the reviewed digest is flagged. Source merged to a repository after its release
is not deployed until the release is promoted into that table, which the output says.

Read-only: `docker ps`, `docker inspect`, and the gate's own read-only probes. It never pulls an
image, starts or stops a container, or writes .env.

Exit codes: 0 the state was determined (an unhealthy service or a gate still in progress is a
finding, not a failure), 2 it could not be: Docker cannot be asked, or the gate cannot be read on
an installed stack. Stdlib only and Python 3.10, like the other operator scripts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent


def _load(name: str, filename: str) -> Any:
    """A sibling script by path: these run without the repository on sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stores = _load("groovemap_stores", "stores.py")
gate = _load("groovemap_discogs_import_status", "discogs_import_status.py")
helpers = _load("groovemap_deploy_helpers", "deploy_helpers.py")

# Compose service -> the image variable that promotes it (None: a third-party image GrooveMap does not release).
SERVICES: dict[str, str | None] = {
    "rabbitmq": None,
    "rabbitmq-dlq-policy-init": None,
    "postgres": None,
    "neo4j": None,
    "valkey": None,
    "postgres-exporter": None,
    "valkey-exporter": None,
    "cadvisor": None,
    "node-exporter": None,
    "victoria-metrics": None,
    "victoria-traces": None,
    "otel-collector": None,
    "grafana": None,
    "schema-init": "DATABASE_SCHEMA_IMAGE",
    "api": "CATALOG_API_IMAGE",
    "extractor-discogs": "DISCOGS_INGESTION_IMAGE",
    "graphinator": "DISCOGS_GRAPH_ENRICHER_IMAGE",
    "tableinator": "DISCOGS_SQL_LOADER_IMAGE",
    "dashboard": "OPERATIONS_CONSOLE_IMAGE",
    "explore": "GRAPH_EXPLORER_IMAGE",
    "insights": "ANALYTICS_ENGINE_IMAGE",
    "extractor-musicbrainz": "MUSICBRAINZ_INGESTION_IMAGE",
    "brainzgraphinator": "MUSICBRAINZ_GRAPH_ENRICHER_IMAGE",
    "brainztableinator": "MUSICBRAINZ_SQL_LOADER_IMAGE",
}
DISCOGS_SERVICES = ("extractor-discogs", "graphinator", "tableinator")
MUSICBRAINZ_SERVICES = ("extractor-musicbrainz", "brainzgraphinator", "brainztableinator")
# Services that run once and exit 0 by design, including an external store's check, which every
# `docker compose up` runs again first (it leaves an exited container, listed after SERVICES).
ONE_SHOTS = ("rabbitmq-dlq-policy-init", "schema-init", *stores.STORE_CHECK_SERVICES.values())
LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"
DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\b")

MUSICBRAINZ_SIZING = "unmeasured (gm-deployment-vc5s); no disk or memory figure is claimed until it records real numbers"
UNRELEASED_NOTE = (
    "Source merged to a repository after its release is not deployed until that release is promoted into "
    "RELEASED_IMAGE_VERSIONS (scripts/check-images.py) and `just env-init` moves .env to it."
)


@dataclass(frozen=True)
class Container:
    """What `docker inspect` says about one groovemap-* container."""

    service: str
    state: str  # running, exited, created, restarting, ...
    health: str  # healthy, unhealthy, starting, or "" without a healthcheck
    exit_code: int
    image_ref: str
    labels: Mapping[str, str]

    @property
    def digest(self) -> str:
        match = DIGEST.search(self.image_ref)
        return match[1] if match else ""


@dataclass(frozen=True)
class ServiceRow:
    service: str
    state: str  # the word shown
    problem: bool = False
    note: str = ""


@dataclass(frozen=True)
class ImageRow:
    service: str
    verdict: str  # reviewed, other, unpinned
    text: str


@dataclass
class Gate:
    """The Discogs gate: complete (0), in progress (3), cannot determine (2), or not started (stack not installed)."""

    label: str
    code: int | None
    detail: str = ""
    report: Any = None


@dataclass
class Snapshot:
    containers: dict[str, Container] = field(default_factory=dict)
    error: str = ""


class StatusError(RuntimeError):
    """Docker could not be asked."""


# --- reading Docker ---------------------------------------------------------------------


def _docker(runner: Callable[..., subprocess.CompletedProcess[str]], *args: str) -> str:
    try:
        completed = runner(["docker", *args], capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError as error:
        raise StatusError("the docker command is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise StatusError(f"docker did not answer: {error}") from error
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise StatusError(lines[-1] if lines else f"docker {args[0]} failed with exit {completed.returncode}")
    return completed.stdout


def read_containers(runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Container]:
    """Every groovemap-* container, by service name; raises StatusError when Docker cannot say."""
    names = [
        line.strip() for line in _docker(runner, "ps", "-a", "--filter", f"name=^{helpers.CONTAINER_PREFIX}", "--format", "{{.Names}}").splitlines()
    ]
    names = [name for name in names if name]
    if not names:
        return {}
    found: dict[str, Container] = {}
    for line in _docker(runner, "inspect", "--format", "{{json .}}", *names).splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            raise StatusError(f"unreadable docker inspect output: {error}") from error
        state = data.get("State") or {}
        service = str(data.get("Name", "")).lstrip("/").removeprefix(helpers.CONTAINER_PREFIX)
        found[service] = Container(
            service=service,
            state=str(state.get("Status", "unknown")),
            health=str((state.get("Health") or {}).get("Status", "")),
            exit_code=int(state.get("ExitCode") or 0),
            image_ref=str((data.get("Config") or {}).get("Image", "")),
            labels=(data.get("Config") or {}).get("Labels") or {},
        )
    return found


# --- classification (pure) --------------------------------------------------------------


def gate_state(code: int | None) -> str:
    return {0: "complete", 3: "in progress", 2: "cannot determine"}.get(code if code is not None else -1, "not started")


def classify(service: str, container: Container | None, gate_code: int | None, musicbrainz_enabled: bool, external: Sequence[str]) -> ServiceRow:
    """installed / healthy / importing / waiting-on-gate for one service. `gate_code` is the Discogs gate's exit code."""
    if container is None:
        if service in MUSICBRAINZ_SERVICES and not musicbrainz_enabled:
            if gate_code is None:
                return ServiceRow(service, "not installed")
            if gate_code == 3:
                return ServiceRow(service, "waiting-on-gate", note="MusicBrainz is not enabled until the Discogs import completes")
            if gate_code == 0:
                return ServiceRow(service, "not enabled", note="the Discogs gate is complete: run `just deploy` to enable MusicBrainz")
            return ServiceRow(service, "not enabled", note="the Discogs gate cannot be read, so whether to enable MusicBrainz is unknown")
        if service in stores.LOCAL_STORE_SERVICES["postgres"] and "postgres" in external:
            return ServiceRow(service, "external", note="PostgreSQL is not a container of this stack")
        if service in stores.LOCAL_STORE_SERVICES["valkey"] and "valkey" in external:
            return ServiceRow(service, "external", note="Valkey is not a container of this stack")
        return ServiceRow(service, "not installed")
    if container.state == "running":
        if container.health == "unhealthy":
            return ServiceRow(service, "installed, unhealthy", problem=True)
        if container.health == "starting":
            return ServiceRow(service, "installed, starting")
        importing = (service in DISCOGS_SERVICES and gate_code == 3) or service in MUSICBRAINZ_SERVICES
        health = "healthy" if container.health == "healthy" else "running (no healthcheck)"
        return ServiceRow(service, f"installed, {health}" + (", importing" if importing else ""))
    if container.state == "exited" and container.exit_code == 0:
        return ServiceRow(service, "installed, completed" if service in ONE_SHOTS else "installed, exited (0)", problem=service not in ONE_SHOTS)
    if container.state == "exited":
        return ServiceRow(service, f"installed, exited ({container.exit_code})", problem=True)
    return ServiceRow(service, f"installed, {container.state}", problem=container.state not in ("created",))


def reviewed_releases(policy: Any) -> dict[str, tuple[str, str]]:
    """Image variable -> (reviewed version, reviewed digest)."""
    return {
        variable: (policy.RELEASED_IMAGE_VERSIONS[variable], policy.RELEASED_IMAGE_DIGESTS[variable]) for variable in policy.RELEASED_IMAGE_DIGESTS
    }


def image_row(container: Container, variable: str, policy: Any) -> ImageRow:
    """Map a running image's digest to the reviewed release, with the OCI labels it carries."""
    version, reviewed = reviewed_releases(policy)[variable]
    repository = policy.IMAGE_OWNERS.get(variable, variable)
    label_version = container.labels.get(LABEL_VERSION, "")
    revision = container.labels.get(LABEL_REVISION, "")
    labels = f"label version {label_version or '-'}, source revision {revision[:12] or '-'}"
    digest = container.digest
    if not digest:
        return ImageRow(
            container.service,
            "unpinned",
            f"{repository}: NOT a pinned digest ({container.image_ref or 'no image'}); reviewed is v{version}; {labels}",
        )
    if digest == reviewed:
        return ImageRow(container.service, "reviewed", f"{repository}: reviewed v{version} (sha256:{digest[:12]}); {labels}")
    others = [other for other, (_version, other_digest) in reviewed_releases(policy).items() if other_digest == digest]
    known = f"; that digest is the reviewed {policy.IMAGE_OWNERS.get(others[0], others[0])}, not this service's image" if others else ""
    return ImageRow(
        container.service,
        "other",
        f"{repository}: NOT the reviewed release v{version}: running sha256:{digest[:12]}{known}; {labels}",
    )


# --- gathering --------------------------------------------------------------------------


def probe_gate(
    containers: Mapping[str, Container],
    configured: Any,
    files: Sequence[str],
    env_file: str | None,
    project: str | None,
    runner: Any,
    root_check: str,
) -> Gate:
    """Run the Discogs gate when the stack is there to ask; an uninstalled stack is "not started", not an error."""
    anchor = containers.get(root_check)
    if anchor is None:
        return Gate("not started", None, "the stack is not installed yet")
    if anchor.state != "running":
        return Gate("cannot determine", 2, f"{root_check} is {anchor.state}, so the gate cannot be read")
    compose = gate.Compose(list(files), env_file, project, runner=runner, configured=configured)
    report = gate.evaluate(gate.observe(compose))
    detail = "; ".join(f"{signal.name} {signal.summary}" for signal in report.signals)
    return Gate(report.status.label, int(report.status), detail, report)


# --- rendering --------------------------------------------------------------------------


def phase(containers: Mapping[str, Container], gate_result: Gate, musicbrainz_enabled: bool) -> str:
    if not containers:
        return "not installed: nothing of GrooveMap exists on this Docker host yet (run `just deploy`)"
    if gate_result.code == 2:
        return "installed; the phase cannot be told because the Discogs gate cannot be read"
    if musicbrainz_enabled and gate_result.code == 0:
        return "phase 2: Discogs import complete, MusicBrainz enabled"
    if musicbrainz_enabled:
        return "MusicBrainz is enabled in .env but the Discogs gate is not complete"
    if gate_result.code == 0:
        return "phase 1 finished: the Discogs import is complete and MusicBrainz is not enabled yet (run `just deploy`)"
    return "phase 1: importing Discogs; MusicBrainz waits on the gate"


def render(
    rows: Sequence[ServiceRow],
    images: Sequence[ImageRow],
    gate_result: Gate,
    decisions: Sequence[tuple[str, str, str]],
    phase_text: str,
) -> str:
    lines = [f"GrooveMap: {phase_text}", ""]
    lines.append("Decisions recorded")
    if decisions:
        lines.extend(f"  {name.ljust(12)} {value} ({where})" for name, value, where in decisions)
    else:
        lines.append("  none")
    lines += ["", "Discogs gate: " + gate_result.label.upper() + (f" (exit {gate_result.code})" if gate_result.code is not None else "")]
    if gate_result.detail:
        lines.append(f"  {gate_result.detail}")
    if gate_result.code == 3:
        lines.append("  Exit 3 is normal progress while the import runs; `just discogs-import-status --prod --wait` waits for it.")
    lines += ["", "Services"]
    width = max(len(row.service) for row in rows)
    for row in rows:
        mark = "!" if row.problem else " "
        lines.append(f" {mark}{row.service.ljust(width)}  {row.state}" + (f"  - {row.note}" if row.note else ""))
    lines += ["", "Images (reviewed release table: RELEASED_IMAGE_VERSIONS)"]
    if images:
        lines.extend(f" {'!' if image.verdict != 'reviewed' else ' '}{image.service.ljust(width)}  {image.text}" for image in images)
    else:
        lines.append("  no repository-owned image is running")
    lines.append(f"  {UNRELEASED_NOTE}")
    lines += ["", f"MusicBrainz sizing: {MUSICBRAINZ_SIZING}"]
    problems = [row for row in rows if row.problem]
    flagged = [image for image in images if image.verdict != "reviewed"]
    if problems or flagged:
        lines.append("")
        if problems:
            lines.append("Attention: " + ", ".join(f"{row.service} ({row.state.split(', ', 1)[-1]})" for row in problems))
        if flagged:
            lines.append("Attention: not the reviewed release: " + ", ".join(image.service for image in flagged))
    return "\n".join(lines)


# --- main -------------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="status",
        description="Report where this deployment stands (read-only). Exit 0 state determined, 2 could not be determined.",
    )
    parser.add_argument("--dev", action="store_true", help="the development stack (docker-compose.yml alone) instead of the production overlay")
    parser.add_argument("-f", "--file", action="append", default=[], dest="files", help="compose file (repeatable; appended to the default files)")
    parser.add_argument("--env-file", help="compose --env-file")
    parser.add_argument("-p", "--project-name", help="compose --project-name")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    environ: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    runner = runner or subprocess.run
    environ = os.environ if environ is None else environ
    env_values = stores.read_env(env_path or (Path(args.env_file) if args.env_file else ROOT / ".env"))
    decisions = stores.recorded_decisions(env_values, environ)
    try:
        configured = stores.read_stores(env_values, environ)
    except stores.StoreConfigError as error:
        print(f"cannot determine: {error}", file=sys.stderr)
        return 2
    try:
        containers = read_containers(runner)
    except StatusError as error:
        print(f"cannot determine: Docker could not be asked: {error}", file=sys.stderr)
        return 2

    files = [] if args.dev else [*gate.PROD_COMPOSE_FILES, *configured.overlays]
    files += [str(Path(path).resolve()) for path in args.files]
    musicbrainz_enabled = helpers.has_profile(helpers.effective("COMPOSE_PROFILES", env_values, environ))
    gate_result = probe_gate(
        containers, configured, files, args.env_file, args.project_name, runner, configured.stack_container.removeprefix(helpers.CONTAINER_PREFIX)
    )

    policy = helpers.load_image_policy()
    names = [*SERVICES, *sorted(name for name in containers if name not in SERVICES)]
    rows = [classify(name, containers.get(name), gate_result.code, musicbrainz_enabled, configured.external) for name in names]
    images = [
        image_row(containers[name], variable, policy)
        for name, variable in SERVICES.items()
        if variable and name in containers and containers[name].state == "running"
    ]
    print(render(rows, images, gate_result, decisions, phase(containers, gate_result, musicbrainz_enabled)))
    return 2 if gate_result.code == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
