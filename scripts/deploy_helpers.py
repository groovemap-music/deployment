"""The logic behind `just deploy` (scripts/deploy.sh): preflight, .env sizing, the profile switch,
and the endpoint summary.

The shell script owns the sequence; everything here that needs a decision lives in Python so it
can be tested without a Docker host. Every Docker call goes through an injectable runner, and
nothing here starts, stops or changes a container.

Minimums, and where they come from:

- Memory, production overlay: refuse below 16 GB, warn below 32 GB. Neo4j's share is sized from
  total memory (see `neo4j_sizing`), PostgreSQL takes 2 GB shared_buffers and plans on a 6 GB
  cache (docker-compose.prod.yml), and the rest of the stack and the OS need the remainder. From
  64 GB the formula reproduces the overlay's own defaults (8g heap, 16g page cache, 48G limit).
- Memory, base stack (--dev): refuse below 8 GB, warn below 16 GB. docker-compose.yml fixes Neo4j
  at a 2 GiB heap and 1 GiB page cache, and the full import still runs.
- Disk, fresh install, on the Docker data root: refuse below 100 GB, warn below 200 GB. The
  Discogs data is ~76 GB on disk (28 GB Neo4j + 48 GB PostgreSQL; docs/architecture.md "Dataset
  Scale") plus the ~11.3 GB compressed dump the extractor keeps, plus images, WAL, logs and
  metrics. MusicBrainz has not been measured (gm-deployment-q4en), so the recommended size adds a
  conservative estimate of another 100 GB, as large as Discogs.
- Disk, existing deployment: the data is already on disk, so only warn below 20 GB free.

Memory is what Docker reports (`docker info` MemTotal), which on Docker Desktop is the VM's
share, not the host's. A machine sold as 16 GB reports a little less, so the check rounds up to
whole gigabytes before comparing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"

GIB = 1024**3
PROFILE = "musicbrainz"
CONTAINER_PREFIX = "groovemap-"

# (refuse below, warn below), in GB of memory available to Docker.
RAM_MINIMUMS = {"prod": (16, 32), "dev": (8, 16)}
DISK_REFUSE_GB = 100
DISK_RECOMMENDED_GB = 200
DISK_LOW_GB = 20
COMPOSE_MIN_MAJOR = 2

# docker-compose.prod.yml's defaults: heap + page cache = 24 GB inside a 48G container limit.
PROD_NEO4J_GB = 24
PROD_HEAP_GB = 8

SIZING_KEYS = ("NEO4J_HEAP_SIZE", "NEO4J_PAGECACHE_SIZE", "NEO4J_MEMORY_LIMIT")
SIZING_MARKER = "# Neo4j sizing written by just deploy"
PROFILE_MARKER = "# Enabled by just deploy once the Discogs import finished"
BASE_URL_MARKER = "# Written by just deploy: the public Explore origin used in email links"

# Every published host port: (variable, default, bind group in prod, bind group in the base
# stack, what it publishes). docs/configuration.md "Host ports and bind addresses".
PORTS = (
    ("GRAFANA_HOST_PORT", 3000, "web", "web", "Grafana"),
    ("API_HOST_PORT", 8004, "web", "web", "Catalog API"),
    ("API_HEALTH_HOST_PORT", 8005, "web", "web", "Catalog API health"),
    ("DASHBOARD_HOST_PORT", 8003, "web", "web", "Operations console"),
    ("EXPLORE_HOST_PORT", 8006, "web", "web", "Graph Explorer"),
    ("EXPLORE_HEALTH_HOST_PORT", 8007, "web", "web", "Graph Explorer health"),
    ("POSTGRES_HOST_PORT", 5433, "data", "data", "PostgreSQL"),
    ("NEO4J_HTTP_HOST_PORT", 7474, "data", "data", "Neo4j Browser"),
    ("NEO4J_BOLT_HOST_PORT", 7687, "data", "data", "Neo4j Bolt"),
    ("RABBITMQ_AMQP_HOST_PORT", 5672, "data", "data", "RabbitMQ AMQP"),
    ("RABBITMQ_MANAGEMENT_HOST_PORT", 15672, "data", "data", "RabbitMQ management"),
    ("RABBITMQ_METRICS_HOST_PORT", 15692, "internal", "internal", "RabbitMQ metrics"),
    ("VALKEY_HOST_PORT", 6379, "internal", "data", "Valkey"),
    ("VICTORIA_METRICS_HOST_PORT", 8428, "internal", "data", "VictoriaMetrics"),
    ("VICTORIA_TRACES_HOST_PORT", 10428, "internal", "data", "VictoriaTraces"),
)
BIND_ADDRESSES = {"web": ("WEB_BIND_ADDRESS", ""), "data": ("DATA_BIND_ADDRESS", ""), "internal": ("INTERNAL_BIND_ADDRESS", "127.0.0.1")}
WILDCARDS = ("", "0.0.0.0", "::", "[::]")  # noqa: S104 - matching Compose's wildcard binds, not binding one


# --- .env reading and writing ----------------------------------------------------------


ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    # Compose's dotenv reader drops an unquoted inline comment that follows whitespace.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=VALUE assignments the way Compose's .env reader does, ignoring comments."""
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            values[match["key"]] = _unquote(match["value"])
    return values


def read_env(path: Path) -> dict[str, str]:
    return parse_env(path.read_text()) if path.exists() else {}


def effective(key: str, env_values: Mapping[str, str], environ: Mapping[str, str], default: str = "") -> str:
    """Compose's precedence: the shell environment, then .env; an empty value means the default."""
    value = environ.get(key, env_values.get(key, ""))
    return value or default


def set_env_values(text: str, values: Mapping[str, str], comment: str) -> str:
    """Replace each key's live assignment in place, appending the rest under `comment`."""
    remaining = dict(values)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = None if line.lstrip().startswith("#") else ASSIGNMENT.match(line)
        if match and match["key"] in remaining:
            lines[index] = f"{match['key']}={remaining.pop(match['key'])}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(comment)
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


# --- sizing (pure) ---------------------------------------------------------------------


@dataclass(frozen=True)
class Sizing:
    """Neo4j memory for the production overlay, in whole GB."""

    heap: int
    pagecache: int
    limit: int

    def env(self) -> dict[str, str]:
        return {"NEO4J_HEAP_SIZE": f"{self.heap}g", "NEO4J_PAGECACHE_SIZE": f"{self.pagecache}g", "NEO4J_MEMORY_LIMIT": f"{self.limit}G"}


def nominal_gb(mem_bytes: int) -> int:
    """Memory as sold: firmware and the kernel keep some back, so round up to whole GB."""
    return max(1, math.ceil(mem_bytes / GIB))


def neo4j_sizing(total_gb: int) -> Sizing:
    """Size Neo4j from total memory T (GB).

    - Neo4j gets half of memory for heap + page cache, capped at the overlay's 24 GB.
    - A third of that is heap (at least 1, at most the overlay's 8), the rest page cache.
    - The container limit is twice heap + page cache (the overlay's 48G at the cap), but leaves
      4 GB of the host for everything else and always 1 GB above heap + page cache.

    At 16 GB that is 2g / 6g / 12G, at 32 GB 5g / 11g / 28G, and from 64 GB the overlay's
    defaults of 8g / 16g / 48G.
    """
    neo4j = max(2, min(PROD_NEO4J_GB, total_gb // 2))
    heap = max(1, min(PROD_HEAP_GB, neo4j // 3))
    pagecache = neo4j - heap
    limit = max(neo4j + 1, min(2 * neo4j, total_gb - 4))
    return Sizing(heap, pagecache, limit)


def plan_sizing(env_text: str, template_values: Mapping[str, str], sizing: Sizing) -> dict[str, str]:
    """Which sizing values deploy should write: only the ones nobody has chosen.

    A value counts as unset when it is absent or empty, and also when it is still exactly what
    `.env.example` ships (NEO4J_HEAP_SIZE=2G): that line is a base-stack-sized placeholder copied
    by `just env-init`, not a choice for this host, and left alone it would hold the production
    heap at 2 GB beside a 16 GB page cache. Once deploy has written its sizing (the marker comment
    is present) it never rewrites any of it, so an edit after the first run always sticks.
    """
    if SIZING_MARKER in env_text:
        return {}
    current = parse_env(env_text)
    wanted = sizing.env()
    return {key: wanted[key] for key in SIZING_KEYS if not current.get(key) or current[key] == template_values.get(key)}


def merge_profiles(value: str, profile: str = PROFILE) -> str:
    profiles = [item.strip() for item in value.split(",") if item.strip()]
    return ",".join(profiles if profile in profiles else [*profiles, profile])


def has_profile(value: str, profile: str = PROFILE) -> bool:
    return profile in (item.strip() for item in value.split(","))


def project_name(explicit: str | None, env_values: Mapping[str, str], environ: Mapping[str, str], root: Path = ROOT) -> str:
    """The Compose project name: -p, then COMPOSE_PROJECT_NAME, then the normalized directory name."""
    name = explicit or effective("COMPOSE_PROJECT_NAME", env_values, environ) or root.name
    return re.sub(r"^[^a-z0-9]+", "", re.sub(r"[^a-z0-9_-]", "", name.lower()))


# --- preflight (pure decisions) --------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One preflight result. `level` is ok, warn, confirm (needs a yes) or stop."""

    level: str
    message: str
    hint: str = ""
    # A resource shortfall that --force may override; nothing else can be forced.
    forcible: bool = False


def check_engine(info: Mapping[str, Any] | None, error: str) -> Finding:
    if info is None:
        return Finding(
            "stop", f"Docker Engine is not reachable: {error}", "Install Docker Engine and start it; on Linux add yourself to the docker group."
        )
    desktop = " (Docker Desktop)" if "Docker Desktop" in str(info.get("OperatingSystem", "")) else ""
    return Finding("ok", f"Docker Engine {info.get('ServerVersion', '?')}{desktop}")


def check_compose(version: str | None) -> Finding:
    hint = "Install the Docker Compose v2 plugin: https://docs.docker.com/compose/install/"
    if version is None:
        return Finding("stop", "Docker Compose v2 (`docker compose`) is not installed", hint)
    match = re.match(r"v?(\d+)\.(\d+)", version.strip())
    if not match:
        return Finding("stop", f"could not read the Docker Compose version from {version.strip()!r}", hint)
    if int(match[1]) < COMPOSE_MIN_MAJOR:
        return Finding("stop", f"Docker Compose {version.strip()} is too old; GrooveMap needs v2 or newer", hint)
    return Finding("ok", f"Docker Compose {version.strip().removeprefix('v')}")


def check_architecture(arch: str, desktop: bool, amd64_emulation: bool | None) -> Finding:
    if arch in ("x86_64", "amd64"):
        return Finding("ok", f"CPU architecture {arch}")
    message = (
        f"This Docker host is {arch or 'an unknown architecture'}, but GrooveMap's own images are built for linux/amd64 only. "
        "They will run under emulation, which works but is much slower: expect the Discogs import to take several times longer."
    )
    if desktop:
        hint = "Docker Desktop emulates amd64 for you (turn on Rosetta in Settings > General for better speed)."
    elif amd64_emulation:
        hint = "amd64 emulation (binfmt) is registered on this host."
    else:
        hint = (
            "Linux needs amd64 emulation registered first, for example: "
            "docker run --privileged --rm tonistiigi/binfmt --install amd64 (or install qemu-user-static and binfmt-support)."
        )
    return Finding("confirm", message, hint)


def check_memory(mem_bytes: int, mode: str, desktop: bool) -> Finding:
    refuse, recommended = RAM_MINIMUMS[mode]
    total = nominal_gb(mem_bytes)
    stack = "production stack" if mode == "prod" else "development stack"
    where = " Docker Desktop's VM has this much: raise it in Settings > Resources." if desktop else ""
    if total < refuse:
        return Finding(
            "stop",
            f"Docker has about {total} GB of memory; the {stack} needs at least {refuse} GB ({recommended} GB recommended).{where}",
            forcible=True,
        )
    if total < recommended:
        return Finding("warn", f"Docker has about {total} GB of memory; {recommended} GB is recommended, so the import will be slower.{where}")
    return Finding("ok", f"Memory: about {total} GB")


def check_disk(free_bytes: int | None, root_dir: str, existing: bool, why_unknown: str = "") -> Finding:
    if free_bytes is None:
        need = "about 100 GB free for Discogs and 200 GB with MusicBrainz"
        return Finding("warn", f"Could not measure free disk space for Docker's data ({why_unknown or root_dir}); make sure there is {need}.")
    free = free_bytes // 10**9
    if existing:
        if free < DISK_LOW_GB:
            return Finding("warn", f"Only {free} GB free under {root_dir}; GrooveMap's data keeps growing, so free some space soon.")
        return Finding("ok", f"Disk: {free} GB free under {root_dir}")
    if free < DISK_REFUSE_GB:
        return Finding(
            "stop",
            f"Only {free} GB free under {root_dir}; the Discogs data alone needs about {DISK_REFUSE_GB} GB "
            f"({DISK_RECOMMENDED_GB} GB recommended with MusicBrainz).",
            forcible=True,
        )
    if free < DISK_RECOMMENDED_GB:
        return Finding(
            "warn",
            f"{free} GB free under {root_dir}: enough for Discogs (~{DISK_REFUSE_GB} GB), but MusicBrainz may need about "
            f"{DISK_RECOMMENDED_GB - DISK_REFUSE_GB} GB more (an estimate; it has not been measured yet).",
        )
    return Finding("ok", f"Disk: {free} GB free under {root_dir}")


@dataclass(frozen=True)
class Port:
    variable: str
    label: str
    address: str
    port: int


def configured_ports(mode: str, env_values: Mapping[str, str], environ: Mapping[str, str]) -> list[Port]:
    """The host ports this stack will publish, on the addresses it will bind."""
    ports = []
    for variable, default, prod_group, dev_group, label in PORTS:
        bind_variable, bind_default = BIND_ADDRESSES[prod_group if mode == "prod" else dev_group]
        value = effective(variable, env_values, environ, str(default))
        try:
            port = int(value)
        except ValueError:
            port = -1
        ports.append(Port(variable, label, effective(bind_variable, env_values, environ, bind_default), port))
    return ports


def check_ports(ports: Iterable[Port], own: set[int], in_use: Callable[[str, int], bool]) -> list[Finding]:
    findings = []
    for port in ports:
        if not 0 < port.port < 65536:
            findings.append(Finding("stop", f"{port.variable} is not a valid port number"))
        elif port.port not in own and in_use(port.address, port.port):
            findings.append(
                Finding(
                    "stop",
                    f"Port {port.port} ({port.label}) is already in use on this host",
                    f"Stop whatever uses it, or set {port.variable} to a free port in .env and re-run.",
                )
            )
    if not findings:
        findings.append(Finding("ok", "All configured host ports are free"))
    return findings


def port_in_use(address: str, port: int) -> bool:
    """True if something already listens on, or holds, the address and port Compose will bind."""
    host = "127.0.0.1" if address in WILDCARDS else address.strip("[]")
    try:
        with socket.create_connection((host, port), timeout=0.3) as connection:
            # A loopback connect to a free port can pick that same port as its source and
            # connect to itself; that is no listener.
            if connection.getsockname() != connection.getpeername():
                return True
    except OSError:
        pass
    bind_host = "" if address in WILDCARDS else address.strip("[]")
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind_host, port))
        except OSError:
            return True
    return False


PUBLISHED = re.compile(r":(\d+)->")


def published_ports(ports_column: str) -> set[int]:
    """Host ports out of `docker ps --format {{.Ports}}` ("0.0.0.0:5433->5432/tcp, ...")."""
    return {int(port) for port in PUBLISHED.findall(ports_column)}


# --- talking to Docker (impure) --------------------------------------------------------


@dataclass
class Docker:
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def run(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.runner(["docker", *args], capture_output=True, text=True, check=False, timeout=60)
        except FileNotFoundError, subprocess.TimeoutExpired:
            return None

    def info(self) -> tuple[dict[str, Any] | None, str]:
        completed = self.run("info", "--format", "{{json .}}")
        if completed is None:
            return None, "the docker command is not installed or not on PATH"
        try:
            info = json.loads(completed.stdout or "null")
        except json.JSONDecodeError:
            info = None
        if completed.returncode != 0 or not isinstance(info, dict) or info.get("ServerErrors") or not info.get("ServerVersion"):
            lines = (completed.stderr or "").strip().splitlines()
            return None, lines[-1] if lines else "no answer from the Docker daemon"
        return info, ""

    def compose_version(self) -> str | None:
        completed = self.run("compose", "version", "--short")
        return completed.stdout.strip() if completed is not None and completed.returncode == 0 and completed.stdout.strip() else None

    def lines(self, *args: str) -> list[str]:
        completed = self.run(*args)
        return [line for line in completed.stdout.splitlines() if line.strip()] if completed is not None and completed.returncode == 0 else []

    def groovemap_containers(self, all_states: bool) -> list[str]:
        return self.lines("ps", *(["-a"] if all_states else []), "--filter", f"name=^{CONTAINER_PREFIX}", "--format", "{{.Names}}\t{{.Ports}}")


def local_engine(environ: Mapping[str, str]) -> bool:
    host = environ.get("DOCKER_HOST", "")
    return not host or host.startswith(("unix://", "npipe://"))


def amd64_emulation_registered(binfmt: Path = Path("/proc/sys/fs/binfmt_misc")) -> bool | None:
    if not binfmt.is_dir():
        return None
    try:
        return any("x86_64" in entry.name or "rosetta" in entry.name for entry in binfmt.iterdir())
    except OSError:
        return None


def measure_disk(root_dir: str, local: bool, desktop: bool, usage: Callable[[str], Any] = shutil.disk_usage) -> tuple[int | None, str]:
    if not local:
        return None, "the Docker engine is remote"
    if desktop:
        return None, "Docker Desktop keeps its data inside its VM disk image"
    if not root_dir or not Path(root_dir).exists():
        return None, f"{root_dir or 'the Docker data root'} is not visible from here"
    try:
        return int(usage(root_dir).free), ""
    except OSError as error:
        return None, f"{root_dir}: {error.strerror}"


# --- commands --------------------------------------------------------------------------


MARKS = {"ok": "ok  ", "warn": "warn", "confirm": "note", "stop": "stop"}


def show(finding: Finding) -> None:
    print(f"    [{MARKS[finding.level]}] {finding.message}")
    if finding.hint:
        print(f"           {finding.hint}")


def preflight(
    mode: str,
    *,
    assume_yes: bool,
    force: bool,
    docker: Docker,
    environ: Mapping[str, str],
    env_values: Mapping[str, str],
    in_use: Callable[[str, int], bool] = port_in_use,
    usage: Callable[[str], Any] = shutil.disk_usage,
    binfmt: Callable[[], bool | None] = amd64_emulation_registered,
    interactive: bool = False,
    ask: Callable[[str], str] = input,
) -> int:
    """Print every finding; return 0 to go ahead or 3 to stop before anything changes."""
    info, error = docker.info()
    show(check_engine(info, error))
    compose = check_compose(docker.compose_version())
    show(compose)
    if info is None or compose.level == "stop":
        print("\nDocker has to be working before GrooveMap can be installed: fix the above and re-run.")
        return 3

    desktop = "Docker Desktop" in str(info.get("OperatingSystem", ""))
    local = local_engine(environ)
    root_dir = str(info.get("DockerRootDir", ""))
    free, why_unknown = measure_disk(root_dir, local, desktop, usage)
    findings = [
        check_architecture(str(info.get("Architecture", "")), desktop, binfmt() if local else None),
        check_memory(int(info.get("MemTotal") or 0), mode, desktop),
        check_disk(free, root_dir, existing=bool(docker.groovemap_containers(all_states=True)), why_unknown=why_unknown),
    ]
    if local:
        # A re-run finds the stack's own containers holding its ports; those are not conflicts.
        own = {port for line in docker.groovemap_containers(all_states=False) for port in published_ports(line)}
        findings += check_ports(configured_ports(mode, env_values, environ), own, in_use)
    else:
        findings.append(Finding("warn", "The Docker engine is remote (DOCKER_HOST), so its ports were not checked from here"))
    for finding in findings:
        show(finding)

    stops = [finding for finding in findings if finding.level == "stop"]
    if stops and (not force or not all(finding.forcible for finding in stops)):
        forcible = " (or add --force to go ahead below the minimums anyway)" if all(finding.forcible for finding in stops) else ""
        print(f"\nStopping before anything changes: fix the item(s) marked [stop] and re-run{forcible}.")
        return 3
    if stops:
        print("\n--force given: going ahead below the minimums. Expect it to be slow, and it may run out of memory or disk.")

    if any(finding.level == "confirm" for finding in findings) and not assume_yes:
        if not interactive:
            print("\nRe-run with --yes to go ahead under emulation.")
            return 3
        if ask("\nContinue under emulation? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Stopped; nothing was changed.")
            return 3
    return 0


def configure(env_path: Path, template_path: Path, mem_bytes: int, hostname: str, environ: Mapping[str, str]) -> list[str]:
    """Fill the production overlay's host-specific settings in .env; return what was done."""
    text = env_path.read_text()
    messages = []
    sizing = neo4j_sizing(nominal_gb(mem_bytes))
    wanted = plan_sizing(text, read_env(template_path), sizing)
    if wanted:
        comment = f"{SIZING_MARKER} for about {nominal_gb(mem_bytes)} GB of memory (docs/configuration.md). Edit freely; deploy will not rewrite it."
        text = set_env_values(text, wanted, comment)
        messages.append("Sized Neo4j for this host: " + ", ".join(f"{key}={value}" for key, value in wanted.items()))
    else:
        current = parse_env(text)
        messages.append(
            "Keeping the Neo4j sizing already in .env: " + ", ".join(f"{key}={current.get(key) or '(overlay default)'}" for key in SIZING_KEYS)
        )

    values = parse_env(text)
    if not effective("APP_BASE_URL", values, environ):
        address = effective("WEB_BIND_ADDRESS", values, environ)
        url = f"http://{url_host(address, hostname)}:{effective('EXPLORE_HOST_PORT', values, environ, '8006')}"
        text = set_env_values(text, {"APP_BASE_URL": url}, BASE_URL_MARKER)
        messages.append(f"Set APP_BASE_URL={url} (used in password-reset email links; change it in .env if you reach GrooveMap by another name)")
    env_path.write_text(text)
    return messages


def url_host(address: str, hostname: str) -> str:
    if address in WILDCARDS:
        return hostname
    address = address.strip("[]")
    return f"[{address}]" if ":" in address else address


def endpoints(mode: str, env_values: Mapping[str, str], environ: Mapping[str, str], hostname: str) -> list[tuple[str, str, str]]:
    """(what, URL, how to sign in) for every browser-facing port, on the address it is bound to."""
    prod = mode == "prod"
    notes = {
        "DASHBOARD_HOST_PORT": "sign in with your admin account",
        "GRAFANA_HOST_PORT": "user admin, password in secrets/grafana_admin_password.txt" if prod else "user admin, password groovemap",
        "NEO4J_HTTP_HOST_PORT": "user neo4j, password in secrets/neo4j_password.txt" if prod else "user neo4j, password groovemap",
        "RABBITMQ_MANAGEMENT_HOST_PORT": "credentials in secrets/rabbitmq_username.txt and secrets/rabbitmq_password.txt"
        if prod
        else "user groovemap, password groovemap",
    }
    shown = (
        "DASHBOARD_HOST_PORT",
        "EXPLORE_HOST_PORT",
        "API_HOST_PORT",
        "GRAFANA_HOST_PORT",
        "NEO4J_HTTP_HOST_PORT",
        "RABBITMQ_MANAGEMENT_HOST_PORT",
    )
    by_variable = {port.variable: port for port in configured_ports(mode, env_values, environ)}
    return [
        (
            by_variable[variable].label,
            f"http://{url_host(by_variable[variable].address, hostname)}:{by_variable[variable].port}",
            notes.get(variable, ""),
        )
        for variable in shown
    ]


def render_endpoints(rows: Sequence[tuple[str, str, str]]) -> str:
    width = max(len(label) for label, _, _ in rows)
    return "\n".join(f"    {label.ljust(width)}  {url}" + (f"  ({note})" if note else "") for label, url, note in rows)


def short_hostname() -> str:
    return socket.gethostname().split(".")[0] or "localhost"


# --- command line ----------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="deploy_helpers", description="Helpers for scripts/deploy.sh (`just deploy`).")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="check Docker, architecture, memory, disk and ports; exit 0 to go ahead, 3 to stop")
    check.add_argument("--mode", choices=("prod", "dev"), default="prod")
    check.add_argument("--yes", action="store_true", help="go ahead under amd64 emulation without asking")
    check.add_argument("--force", action="store_true", help="go ahead below the memory and disk minimums")
    sizing = commands.add_parser("configure", help="write Neo4j sizing and APP_BASE_URL into .env where they are unset (production overlay)")
    sizing.add_argument("--mode", choices=("prod", "dev"), default="prod")
    commands.add_parser("profile-enabled", help=f"exit 0 if COMPOSE_PROFILES (shell or .env) includes {PROFILE}, else 1")
    commands.add_parser("enable-profile", help=f"add {PROFILE} to COMPOSE_PROFILES in .env")
    name = commands.add_parser("project-name", help="print the Compose project name")
    name.add_argument("--project-name")
    urls = commands.add_parser("endpoints", help="print the configured browser-facing endpoints")
    urls.add_argument("--mode", choices=("prod", "dev"), default="prod")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, docker: Docker | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    docker = docker or Docker()
    environ = os.environ if environ is None else environ
    env_values = read_env(ENV_FILE)
    if args.command == "preflight":
        return preflight(
            args.mode, assume_yes=args.yes, force=args.force, docker=docker, environ=environ, env_values=env_values, interactive=sys.stdin.isatty()
        )
    if args.command == "configure":
        if args.mode == "dev":
            print("    The development stack fixes its own Neo4j sizing and URLs in docker-compose.yml; nothing to write.")
            return 0
        info, error = docker.info()
        if info is None:
            print(f"error: Docker Engine is not reachable: {error}", file=sys.stderr)
            return 1
        for message in configure(ENV_FILE, TEMPLATE, int(info.get("MemTotal") or 0), short_hostname(), environ):
            print(f"    {message}")
        return 0
    if args.command == "profile-enabled":
        return 0 if has_profile(effective("COMPOSE_PROFILES", env_values, environ)) else 1
    if args.command == "enable-profile":
        merged = merge_profiles(env_values.get("COMPOSE_PROFILES", ""))
        ENV_FILE.write_text(set_env_values(ENV_FILE.read_text(), {"COMPOSE_PROFILES": merged}, PROFILE_MARKER))
        print(f"    Set COMPOSE_PROFILES={merged} in .env, so every later `docker compose up` keeps MusicBrainz running.")
        return 0
    if args.command == "project-name":
        print(project_name(args.project_name, env_values, environ))
        return 0
    print(render_endpoints(endpoints(args.mode, env_values, environ, short_hostname())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
