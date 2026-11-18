"""Helpers for the homelab-shape smoke (`just smoke-homelab`, scripts/smoke-homelab.sh).

The smoke reproduces the lux installation on real Docker: a "homelab" PostgreSQL 18 on its own
bridge network with TLS, GrooveMap's production stack in external-PostgreSQL mode with its own
Valkey, Neo4j and RabbitMQ, an override that attaches exactly the PostgreSQL clients to the
homelab network, and secrets written by create-secrets.sh. The shell script drives Docker; this
module holds the decisions it can unit-test without Docker:

- `provision-sql` turns docs/homelab.md's provisioning SQL into what the homelab admin runs, so
  the smoke proves the documented SQL is enough;
- `override` writes docker-compose.override.yml from the skill's examples
  (skills/groovemap-homelab/assets/override-external-store-network.yml, keeping only the
  PostgreSQL half, and override-shared-monitoring-network.yml), plus the smoke's two documented
  stand-ins;
- `check-stack` compares what Docker runs with Compose's render: every service there, one-shots
  completed, long-running services healthy, read-only roots and users as configured, exactly
  STORE_CLIENTS["postgres"] on the homelab network and MONITORING_CLIENTS on the monitoring one;
- `check-secrets` asserts secrets/ is 0700 and every file 0600, owned by the deploying user;
- `check-blocked` asserts a failed one-shot stopped everything that depends on it.

Every check prints one `ok`/`FAIL` line per finding and exits 1 on any failure. It never reads a
secret's content.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _load_stores() -> Any:
    """scripts/stores.py by path, the one place STORE_CLIENTS lives."""
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

# The heading docs/homelab.md puts right before the provisioning SQL block.
PROVISIONING_INTRO = "The provisioning SQL, run once as an administrator"
SQL_BLOCK = re.compile(r"```sql\n(?P<body>.*?)```", re.DOTALL)
PASSWORD_PLACEHOLDER = "'<password>'"  # noqa: S105 - the doc's placeholder, not a credential
# lux's shared PostgreSQL gives GrooveMap's role and database a 60-connection limit: the store
# check's minimum, so the smoke runs at the edge the check guards.
CONNECTION_LIMIT = 60

# The skill examples name the networks by these keys; the smoke's homelab networks replace them.
# The Valkey half of the external-store example is dropped: this shape runs its own Valkey.
ASSET_POSTGRES_NETWORK = "postgres"
ASSET_MONITORING_NETWORK = "monitoring"
HOMELAB_NETWORK_KEY = "homelab"
MONITORING_NETWORK_KEY = "monitoring"

# Stand-ins, the only part of the override that is not the skill example. The extractors acquire
# their dumps from data.discogs.com and the MusicBrainz mirror (~11 GB and more), which the smoke
# never contacts. They keep their reviewed image, user, volumes and secrets; only the process is
# a sleep, so `up --wait` sees a healthy service. The smoke then runs the real Discogs extractor
# one-shot against the fixture packaged in its own image, which drives the real loaders to
# their completion signals (scripts/smoke-homelab.sh, "the Discogs fixture").
STAND_INS = ("extractor-discogs", "extractor-musicbrainz")
STAND_IN = {
    "entrypoint": ["sleep", "infinity"],
    "init": True,
    "healthcheck": {"test": ["CMD", "true"], "interval": "5s", "timeout": "5s", "retries": 3, "start_period": "0s"},
}

# Services that must run as the deploying user because they read 0600 secret files as their own
# process (gm-deployment-moc2): lux's crash loop was valkey-exporter as UID 59000.
SECRET_READERS = ("grafana", "valkey-exporter")
NEVER_STARTED = "0001-01-01T00:00:00Z"


class SmokeError(RuntimeError):
    """An input the smoke cannot work from."""


# --- provisioning SQL ------------------------------------------------------------------


def provisioning_sql(doc: str, password: str) -> str:
    """docs/homelab.md's provisioning SQL with the password filled in and lux's connection limits."""
    start = doc.find(PROVISIONING_INTRO)
    if start < 0:
        raise SmokeError(f"docs/homelab.md has no {PROVISIONING_INTRO!r} paragraph")
    match = SQL_BLOCK.search(doc, start)
    if match is None:
        raise SmokeError("docs/homelab.md has no ```sql block after the provisioning paragraph")
    body = match["body"]
    for needed in ("CREATE ROLE groovemap", "CREATE DATABASE groovemap OWNER groovemap", "\\c groovemap", "CREATE EXTENSION IF NOT EXISTS pg_trgm"):
        if needed not in body:
            raise SmokeError(f"docs/homelab.md's provisioning SQL no longer contains {needed!r}; update the smoke with it")
    if PASSWORD_PLACEHOLDER not in body:
        raise SmokeError(f"docs/homelab.md's provisioning SQL no longer contains the {PASSWORD_PLACEHOLDER} placeholder")
    if not password or "\n" in password:
        raise SmokeError("the role password must be one non-empty line")
    literal = "'" + password.replace("'", "''") + "'"
    # The homelab server runs the stack's own postgres image, which ships pgvector, so the SQL runs
    # whole: vector and pg_stat_statements are created as on lux.
    body = body.replace(PASSWORD_PLACEHOLDER, literal)
    lines = [line for line in body.splitlines() if line.strip()]
    lines += [f"ALTER ROLE groovemap CONNECTION LIMIT {CONNECTION_LIMIT};", f"ALTER DATABASE groovemap CONNECTION LIMIT {CONNECTION_LIMIT};"]
    return "\n".join(lines) + "\n"


# --- the override ----------------------------------------------------------------------


def _attachments(service: Mapping[str, Any]) -> dict[str, Any]:
    networks = service.get("networks") or {}
    return {name: {} for name in networks} if isinstance(networks, list) else dict(networks)


def _attach(services: dict[str, Any], asset: Mapping[str, Any], asset_key: str, key: str, omitted: set[str]) -> None:
    """Join every service the example attaches to `asset_key` to the smoke's `key` network instead."""
    if asset_key not in (asset.get("networks") or {}):
        raise SmokeError(f"the skill example no longer declares the {asset_key!r} network")
    for name, service in (asset.get("services") or {}).items():
        attached = _attachments(service or {})
        if asset_key in attached and name not in omitted:
            services.setdefault(name, {}).setdefault("networks", {})[key] = attached[asset_key] or {}


def override_model(
    asset: Mapping[str, Any],
    network: str,
    *,
    monitoring_asset: Mapping[str, Any] | None = None,
    monitoring_network: str | None = None,
    omit: Iterable[str] = (),
    environment: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """The skill examples on the smoke's networks, the stand-ins, and any test-case environment.

    The external-store example's PostgreSQL half goes on `network`; with `monitoring_network`, the
    shared-monitoring example (`monitoring_asset`) goes on that one. `omit` drops services from the
    homelab attachment (a negative case); `environment` adds variables per service (a negative case:
    a rewired POSTGRES_HOST, a failing one-shot).
    """
    omitted = set(omit)
    unknown = omitted - set(asset.get("services") or {})
    if unknown:
        raise SmokeError(f"the skill example has no service {', '.join(sorted(unknown))} to omit")
    services: dict[str, Any] = {}
    networks: dict[str, Any] = {HOMELAB_NETWORK_KEY: {"external": True, "name": network}}
    _attach(services, asset, ASSET_POSTGRES_NETWORK, HOMELAB_NETWORK_KEY, omitted)
    if monitoring_network:
        if monitoring_asset is None:
            raise SmokeError("a monitoring network needs the shared-monitoring example")
        _attach(services, monitoring_asset, ASSET_MONITORING_NETWORK, MONITORING_NETWORK_KEY, set())
        networks[MONITORING_NETWORK_KEY] = {"external": True, "name": monitoring_network}
    for name in STAND_INS:
        services.setdefault(name, {}).update(json.loads(json.dumps(STAND_IN)))
    for name, variables in (environment or {}).items():
        services.setdefault(name, {}).setdefault("environment", {}).update(variables)
    return {"services": dict(sorted(services.items())), "networks": networks}


def render_override(model: Mapping[str, Any], subnet: str) -> str:
    """The override file. The stack network's ipam is replaced (`!override`), the one network change
    the override check allows, so the smoke never asks for the subnet an operator's stack holds."""
    header = (
        "# Generated by scripts/smoke_homelab.py for a disposable homelab smoke; never an operator file.\n"
        "# The networks come from skills/groovemap-homelab/assets/override-external-store-network.yml\n"
        "# (PostgreSQL half only) and override-shared-monitoring-network.yml; extractor-discogs and\n"
        "# extractor-musicbrainz are stand-ins (see STAND_IN).\n"
    )
    body = yaml.safe_dump(dict(model), sort_keys=False, default_flow_style=False)
    ipam = f"  groovemap:\n    ipam: !override\n      config:\n        - subnet: {json.dumps(subnet)}\n"
    return header + "---\n" + body + ipam


def parse_environment(entries: Sequence[str]) -> dict[str, dict[str, str]]:
    """SERVICE=KEY=VALUE entries, grouped by service."""
    grouped: dict[str, dict[str, str]] = {}
    for entry in entries:
        service, separator, assignment = entry.partition("=")
        key, separator2, value = assignment.partition("=")
        if not (separator and separator2 and service and key):
            raise SmokeError(f"--set-env expects SERVICE=KEY=VALUE, got {entry!r}")
        grouped.setdefault(service, {})[key] = value
    return grouped


# --- reading Compose and Docker --------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    ok: bool
    text: str

    def line(self) -> str:
        return f"{'ok  ' if self.ok else 'FAIL'} {self.text}"


@dataclass(frozen=True)
class Container:
    """What `docker inspect` says about one Compose-managed container."""

    service: str
    name: str
    status: str
    exit_code: int
    health: str
    started_at: str
    read_only: bool
    user: str
    image: str
    networks: frozenset[str]
    one_off: bool

    @classmethod
    def from_inspect(cls, data: Mapping[str, Any]) -> Container:
        config = data.get("Config") or {}
        labels = config.get("Labels") or {}
        state = data.get("State") or {}
        return cls(
            service=str(labels.get("com.docker.compose.service", "")),
            name=str(data.get("Name", "")).lstrip("/"),
            status=str(state.get("Status", "")),
            exit_code=int(state.get("ExitCode") or 0),
            health=str((state.get("Health") or {}).get("Status", "")),
            started_at=str(state.get("StartedAt", "")),
            read_only=bool((data.get("HostConfig") or {}).get("ReadonlyRootfs")),
            user=str(config.get("User") or ""),
            image=str(config.get("Image") or ""),
            networks=frozenset(((data.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
            one_off=str(labels.get("com.docker.compose.oneoff", "False")) == "True",
        )


def read_containers(inspect: Sequence[Mapping[str, Any]]) -> dict[str, Container]:
    """The stack's own containers by service; `docker compose run` containers are left out."""
    found: dict[str, Container] = {}
    for data in inspect:
        container = Container.from_inspect(data)
        if container.service and not container.one_off:
            if container.service in found:
                raise SmokeError(f"two containers for {container.service}: {found[container.service].name} and {container.name}")
            found[container.service] = container
    return found


def one_shots(rendered: Mapping[str, Any]) -> set[str]:
    """Services something waits on with service_completed_successfully: they must exit 0."""
    found: set[str] = set()
    for service in (rendered.get("services") or {}).values():
        depends = service.get("depends_on") or {}
        if isinstance(depends, dict):
            found.update(name for name, spec in depends.items() if (spec or {}).get("condition") == "service_completed_successfully")
    return found


def dependents(rendered: Mapping[str, Any], target: str) -> set[str]:
    """Every service that depends on `target`, directly or through another service."""
    services = rendered.get("services") or {}
    found: set[str] = set()
    frontier = {target}
    while frontier:
        current = frontier.pop()
        for name, service in services.items():
            depends = service.get("depends_on") or {}
            if current in depends and name not in found:
                found.add(name)
                frontier.add(name)
    return found


def _has_healthcheck(service: Mapping[str, Any]) -> bool:
    check = service.get("healthcheck") or {}
    return bool(check) and not check.get("disable") and check.get("test") not in (None, ["NONE"])


def _check_network(name: str, container: Container, network: str, clients: set[str], what: str, registry: str) -> list[Finding]:
    """`name` is on `network` exactly when it is one of `clients`."""
    on = network in container.networks
    if name in clients:
        return [Finding(on, f"{name}: {what} {'on' if on else 'NOT on'} {network}")]
    return [Finding(False, f"{name}: on {network} but not a {what} ({registry})")] if on else []


def check_stack(
    rendered: Mapping[str, Any],
    containers: Mapping[str, Container],
    *,
    user: str,
    homelab_network: str,
    monitoring_network: str | None = None,
) -> list[Finding]:
    """What runs against what Compose rendered for the active profiles."""
    findings: list[Finding] = []
    services = rendered.get("services") or {}
    completed = one_shots(rendered)
    clients = set(stores.STORE_CLIENTS["postgres"])
    monitoring_clients = set(stores.MONITORING_CLIENTS)
    for name, service in sorted(services.items()):
        container = containers.get(name)
        if container is None:
            findings.append(Finding(False, f"{name}: no container"))
            continue
        if name in completed:
            done = container.status == "exited" and container.exit_code == 0
            findings.append(Finding(done, f"{name}: one-shot {container.status} ({container.exit_code})"))
        elif _has_healthcheck(service):
            findings.append(
                Finding(container.status == "running" and container.health == "healthy", f"{name}: {container.status}, {container.health}")
            )
        else:
            findings.append(Finding(container.status == "running", f"{name}: {container.status} (no healthcheck)"))
        wanted_ro = bool(service.get("read_only"))
        findings.append(Finding(container.read_only == wanted_ro, f"{name}: read-only root {container.read_only} (configured {wanted_ro})"))
        # Without a configured user Docker reports the image's own USER, which is not the stack's to check.
        wanted_user = str(service.get("user") or "")
        if wanted_user:
            findings.append(Finding(container.user == wanted_user, f"{name}: user {container.user} (configured {wanted_user})"))
        if name in SECRET_READERS:
            findings.append(Finding(container.user == user, f"{name}: reads its 0600 secret as {container.user or '(image default)'} (owner {user})"))
        image = str(service.get("image") or "")
        findings.append(Finding(container.image == image, f"{name}: image {'as rendered' if container.image == image else container.image}"))
        findings += _check_network(name, container, homelab_network, clients, "PostgreSQL client", "STORE_CLIENTS")
        if monitoring_network:
            findings += _check_network(name, container, monitoring_network, monitoring_clients, "monitoring client", "MONITORING_CLIENTS")
    for name in sorted(set(containers) - set(services)):
        findings.append(Finding(False, f"{name}: container {containers[name].name} is not in the render"))
    return findings


def check_blocked(rendered: Mapping[str, Any], containers: Mapping[str, Container], failed: str) -> list[Finding]:
    """`failed` exited non-zero and nothing that waits on it ever started."""
    findings = []
    container = containers.get(failed)
    if container is None:
        findings.append(Finding(False, f"{failed}: no container, so it never ran"))
    else:
        findings.append(Finding(container.status == "exited" and container.exit_code != 0, f"{failed}: {container.status} ({container.exit_code})"))
    blocked = sorted(dependents(rendered, failed))
    if not blocked:
        findings.append(Finding(False, f"nothing depends on {failed}"))
    for name in blocked:
        dependent = containers.get(name)
        if dependent is None:
            findings.append(Finding(True, f"{name}: never created"))
        else:
            never = dependent.started_at in ("", NEVER_STARTED) and dependent.status == "created"
            findings.append(Finding(never, f"{name}: {dependent.status}, started {dependent.started_at or 'never'}"))
    return findings


# What the console's own probe code (dashboard.platform_health) reads inside its container, with its
# own environment: the same rows its System Health page shows. Both console releases have these two.
CONSOLE_PROBE = (
    "import asyncio, json, os; from dashboard.platform_health import build_probes, poll_platform; "
    "rows = asyncio.run(poll_platform(build_probes(os.environ['RABBITMQ_PROMETHEUS_URL']))); "
    "print(json.dumps([row.model_dump(mode='json') for row in rows]))"
)
# The rows that reach the homelab's shared services in this shape.
SHARED_ROWS = ("grafana", "victoria-metrics", "victoria-traces", "node-exporter", "cadvisor", "postgres-exporter")
CONSOLE_TARGETS_HINT = (
    "the deployed console predates configurable targets (operations-console 0.3.0, gm-deployment-8nbg) and probes the compose names"
)


def check_console(rows: Sequence[Mapping[str, Any]], shared: Iterable[str] = SHARED_ROWS) -> list[Finding]:
    """The console reports what is true: every probed service healthy, the shared ones probed as shared."""
    findings = []
    by_name = {str(row.get("name")): row for row in rows}
    for name, row in sorted(by_name.items()):
        status = str(row.get("status", ""))
        where = f" at {row['url']}" if row.get("url") else ""
        error = f": {row['error']}" if row.get("error") else ""
        findings.append(Finding(status == "healthy", f"console {name}: {status}{where}{error}"))
    for name in shared:
        found = by_name.get(name)
        if found is None:
            findings.append(Finding(False, f"console has no {name} row"))
        elif found.get("mode") != "shared":
            hint = CONSOLE_TARGETS_HINT if "mode" not in found else f"mode {found.get('mode')}"
            findings.append(Finding(False, f"console {name}: not probed as shared ({hint})"))
    return findings


def check_up(answer: Mapping[str, Any], jobs: Iterable[str]) -> list[Finding]:
    """A VictoriaMetrics `up` query answer holds a 1 for every job: the collector scraped it and wrote it there."""
    seen: dict[str, set[str]] = {}
    for series in (answer.get("data") or {}).get("result") or []:
        job = str((series.get("metric") or {}).get("job", ""))
        seen.setdefault(job, set()).add(str((series.get("value") or [None, ""])[1]))
    return [
        Finding("1" in seen.get(job, set()), f"shared VictoriaMetrics up{{job={job!r}}}: {', '.join(sorted(seen.get(job, set()))) or 'no series'}")
        for job in jobs
    ]


def check_secrets(directory: Path, uid: int) -> list[Finding]:
    """secrets/ at 0700 and every file in it at 0600, all owned by `uid`. Contents are never read."""
    findings = []
    info = directory.stat()
    findings.append(
        Finding(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == uid, f"secrets/: mode {stat.S_IMODE(info.st_mode):04o}, owner {info.st_uid}")
    )
    files = sorted(directory.iterdir())
    if not files:
        findings.append(Finding(False, "secrets/ is empty"))
    for path in files:
        meta = path.lstat()
        regular = stat.S_ISREG(meta.st_mode)
        mode = stat.S_IMODE(meta.st_mode)
        findings.append(Finding(regular and mode == 0o600 and meta.st_uid == uid, f"secrets/{path.name}: mode {mode:04o}, owner {meta.st_uid}"))
    return findings


# --- command line ----------------------------------------------------------------------


def _report(findings: Sequence[Finding]) -> int:
    for finding in findings:
        print(finding.line())
    return 0 if findings and all(finding.ok for finding in findings) else 1


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text() or "null")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="smoke_homelab", description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    sql = commands.add_parser("provision-sql", help="print docs/homelab.md's provisioning SQL for the homelab admin")
    sql.add_argument("password_file", type=Path)
    sql.add_argument("--doc", type=Path, default=ROOT / "docs" / "homelab.md")
    override = commands.add_parser("override", help="write docker-compose.override.yml from the skill example")
    override.add_argument("out", type=Path)
    override.add_argument("--network", required=True, help="the homelab PostgreSQL's Docker network")
    override.add_argument("--subnet", required=True, help="the stack network's subnet for this run")
    override.add_argument("--asset", type=Path, default=ROOT / "skills" / "groovemap-homelab" / "assets" / "override-external-store-network.yml")
    override.add_argument("--monitoring-network", help="the shared monitoring's Docker network (GROOVEMAP_MONITORING=shared)")
    override.add_argument(
        "--monitoring-asset", type=Path, default=ROOT / "skills" / "groovemap-homelab" / "assets" / "override-shared-monitoring-network.yml"
    )
    override.add_argument("--omit", action="append", default=[], help="leave this service off the homelab network (negative case)")
    override.add_argument("--set-env", action="append", default=[], help="SERVICE=KEY=VALUE (negative case)")
    stack = commands.add_parser("check-stack", help="compare the running stack with Compose's render")
    stack.add_argument("--rendered", type=Path, required=True)
    stack.add_argument("--inspect", type=Path, required=True)
    stack.add_argument("--user", required=True, help="UID:GID the stack runs as")
    stack.add_argument("--homelab-network", required=True)
    stack.add_argument("--monitoring-network", help="also check exactly MONITORING_CLIENTS on this network")
    blocked = commands.add_parser("check-blocked", help="a failed one-shot stopped everything that depends on it")
    blocked.add_argument("--rendered", type=Path, required=True)
    blocked.add_argument("--inspect", type=Path, required=True)
    blocked.add_argument("--failed", required=True)
    console = commands.add_parser("check-console", help="the console's own probe rows: everything healthy, the shared rows shared")
    console.add_argument("rows", type=Path)
    up = commands.add_parser("check-up", help="a VictoriaMetrics `up` answer has a 1 for each job")
    up.add_argument("answer", type=Path)
    up.add_argument("--job", action="append", required=True)
    commands.add_parser("console-probe", help="print the Python the console container runs to read its own probes")
    secrets = commands.add_parser("check-secrets", help="secrets/ 0700, every file 0600, owned by the deploying user")
    secrets.add_argument("directory", type=Path)
    secrets.add_argument("--uid", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "provision-sql":
            sys.stdout.write(provisioning_sql(args.doc.read_text(), args.password_file.read_text().strip()))
            return 0
        if args.command == "override":
            asset = yaml.safe_load(args.asset.read_text())
            monitoring = yaml.safe_load(args.monitoring_asset.read_text()) if args.monitoring_network else None
            model = override_model(
                asset,
                args.network,
                monitoring_asset=monitoring,
                monitoring_network=args.monitoring_network,
                omit=args.omit,
                environment=parse_environment(args.set_env),
            )
            args.out.write_text(render_override(model, args.subnet))
            return 0
        if args.command == "check-secrets":
            return _report(check_secrets(args.directory, args.uid))
        if args.command == "console-probe":
            print(CONSOLE_PROBE)
            return 0
        if args.command == "check-console":
            return _report(check_console(_load_json(args.rows) or []))
        if args.command == "check-up":
            return _report(check_up(_load_json(args.answer) or {}, args.job))
        rendered = _load_json(args.rendered) or {}
        containers = read_containers(_load_json(args.inspect) or [])
        if args.command == "check-stack":
            return _report(
                check_stack(rendered, containers, user=args.user, homelab_network=args.homelab_network, monitoring_network=args.monitoring_network)
            )
        return _report(check_blocked(rendered, containers, args.failed))
    except (SmokeError, OSError, ValueError, yaml.YAMLError) as error:
        print(f"smoke_homelab: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
