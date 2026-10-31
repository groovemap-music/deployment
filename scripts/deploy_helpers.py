"""The logic behind `just deploy` (scripts/deploy.sh): preflight, .env sizing, the override check,
the profile switch, and the endpoint summary.

The shell script owns the sequence; everything here that needs a decision lives in Python so it
can be tested without a Docker host. Every Docker call goes through an injectable runner, and
nothing here starts, stops or changes a container.

Minimums, and where they come from:

- Memory, production overlay: refuse below 16 GB, warn below 32 GB. Neo4j's share is sized from
  total memory (see `neo4j_sizing`), PostgreSQL takes 2 GB shared_buffers and plans on a 6 GB
  cache (docker-compose.prod.yml), and the rest of the stack and the OS need the remainder. From
  64 GB the formula reproduces the overlay's heap, page cache and limit (8g, 16g, 48G); its
  transaction cap comes out at 5g there, below the heap, where the overlay's 8G is not.
- Memory, base stack (--dev): refuse below 8 GB, warn below 16 GB. docker-compose.yml fixes Neo4j
  at a 2 GiB heap and 1 GiB page cache, and the full import still runs.
- Disk, fresh install, per filesystem: each data volume is resolved from the rendered Compose
  config (`resolve_locations`), the volumes are grouped by the filesystem they land on, and each
  filesystem needs the sum of its volumes' shares (`DATA_VOLUMES`): refuse below the minimums,
  warn below the recommended sizes. With nothing relocated everything lands on Docker's data
  root and the shares add up to the old single figures, 100 GB and 200 GB. The Discogs data is
  ~76 GB on disk (28 GB Neo4j + 48 GB PostgreSQL; docs/architecture.md "Dataset Scale") plus the
  ~11.3 GB compressed dump the extractor keeps; images, layers, logs and the small volumes
  take the rest of the minimum on the data root. MusicBrainz has not been measured
  (gm-deployment-q4en), so the recommended size adds a conservative estimate of another 100 GB,
  as large as Discogs, as musicbrainz_data's share.
- Disk, existing deployment: the data is already on disk, so only warn below 20 GB free on any of those filesystems.

Memory is what Docker reports (`docker info` MemTotal), which on Docker Desktop is the VM's
share, not the host's. A machine sold as 16 GB reports a little less, so the check rounds up to
whole gigabytes before comparing.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
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
DISK_LOW_GB = 20
COMPOSE_MIN_MAJOR = 2

# docker-compose.prod.yml's defaults: heap + page cache = 24 GB inside a 48G container limit.
PROD_NEO4J_GB = 24
PROD_HEAP_GB = 8
# Neo4j's transaction memory cap lives inside the heap, so it has to stay below it to bound anything.
TRANSACTION_SHARE = 0.7

SIZING_KEYS = ("NEO4J_HEAP_SIZE", "NEO4J_PAGECACHE_SIZE", "NEO4J_MEMORY_LIMIT", "NEO4J_TRANSACTION_MEMORY_MAX")
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

OVERRIDE_FILE = "docker-compose.override.yml"
# The services published on DATA_BIND_ADDRESS or INTERNAL_BIND_ADDRESS (the "data" and "internal"
# groups in PORTS, plus nothing else); only .env moves their ports.
DATA_TIER = ("postgres", "neo4j", "rabbitmq", "valkey", "victoria-metrics", "victoria-traces")
# What an override must leave alone on every service of the stack, and why.
OVERRIDE_FIXED = (
    ("image", "images come only from the reviewed digests in .env (`just env-init`), never a tag or `latest`"),
    ("build", "the stack runs the released images; it never builds them"),
    ("platform", "GrooveMap's images are linux/amd64 only"),
    ("container_name", "just deploy finds the stack by its groovemap-* container names"),
    ("profiles", "only just deploy turns on the musicbrainz profile, once the Discogs import gate reports complete"),
)
OVERRIDE_DATA_TIER_FIXED = ("ports", "network_mode")
# Host-specific keys that would still expose the data tier: a reverse-proxy network, Traefik TCP
# router labels, or a hosts entry that redirects a peer. Data-tier services get none of them.
OVERRIDE_DATA_TIER_ISOLATED = (
    ("networks", "a data-tier service stays on the groovemap network only; never attach it to a reverse-proxy network"),
    ("labels", "reverse-proxy labels (Traefik TCP routers included) could publish the data tier; route only explore, dashboard and grafana"),
    ("extra_hosts", "hosts entries could redirect the data tier's peers"),
)
# Services the override adds need a sha256 digest, and may publish ports or join groovemap only
# when named in this variable (in .env or the shell), comma-separated.
OVERRIDE_ALLOW_VARIABLE = "OVERRIDE_ALLOW_SERVICES"
DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
STACK_NETWORK = "groovemap"


@dataclass(frozen=True)
class DataVolume:
    """A volume the disk check places, and its share of the documented sizes, in GB."""

    name: str
    service: str
    target: str
    minimum_gb: int
    recommended_gb: int


# Where each large volume is mounted, and its share. The minimums add up to the 100 GB Discogs
# needs on a fresh install (docs/architecture.md "Dataset Scale": 28 GB Neo4j + 48 GB PostgreSQL;
# the ~11.3 GB dump, rounded up). The recommended sizes add MusicBrainz's estimated 100 GB.
DATA_VOLUMES = (
    DataVolume("postgres_data", "postgres", "/var/lib/postgresql/data", 48, 48),
    DataVolume("neo4j_data", "neo4j", "/data", 28, 28),
    DataVolume("discogs_data", "extractor-discogs", "/discogs-data", 12, 12),
    DataVolume("musicbrainz_data", "extractor-musicbrainz", "/musicbrainz-data", 0, 100),
    DataVolume("victoria_metrics_data", "victoria-metrics", "/victoria-metrics-data", 2, 2),
    DataVolume("rabbitmq_data", "rabbitmq", "/var/lib/rabbitmq", 2, 2),
)
# Always on Docker's data root: images (several GB), container layers, logs and the small volumes.
DOCKER_ROOT_SHARE = DataVolume("images and the small volumes", "", "", 8, 8)


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


# .env holds API keys and passwords, so a file this code creates is private; an existing one keeps its mode.
ENV_MODE = 0o600


def env_mode(path: Path) -> int:
    """The mode a rewrite of `path` keeps: the existing file's, or ENV_MODE for a new file."""
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else ENV_MODE


def write_env_text(path: Path, text: str) -> None:
    """Replace `path` with `text` atomically: a same-directory temp file, its mode set explicitly (the umask does not apply), then os.replace."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, env_mode(path))
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write_new_env_text(path: Path, text: str, mode: int) -> None:
    """Create `path` exclusively with `text` and exactly `mode`, for a backup that must not overwrite anything."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ENV_MODE)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(text)


# --- sizing (pure) ---------------------------------------------------------------------


@dataclass(frozen=True)
class Sizing:
    """Neo4j memory for the production overlay, in whole GB."""

    heap: int
    pagecache: int
    limit: int
    transaction: int

    def env(self) -> dict[str, str]:
        return {
            "NEO4J_HEAP_SIZE": f"{self.heap}g",
            "NEO4J_PAGECACHE_SIZE": f"{self.pagecache}g",
            "NEO4J_MEMORY_LIMIT": f"{self.limit}G",
            "NEO4J_TRANSACTION_MEMORY_MAX": f"{self.transaction}g",
        }


def nominal_gb(mem_bytes: int) -> int:
    """Memory as sold: firmware and the kernel keep some back, so round up to whole GB."""
    return max(1, math.ceil(mem_bytes / GIB))


def neo4j_sizing(total_gb: int) -> Sizing:
    """Size Neo4j from total memory T (GB).

    - Neo4j gets half of memory for heap + page cache, capped at the overlay's 24 GB.
    - A third of that is heap (at least 1, at most the overlay's 8), the rest page cache.
    - The container limit is twice heap + page cache (the overlay's 48G at the cap), but leaves
      4 GB of the host for everything else and always 1 GB above heap + page cache.
    - The transaction memory cap is `transaction_max_gb(heap)`.

    At 16 GB that is 2g / 6g / 12G / 1g, at 32 GB 5g / 11g / 28G / 3g, and from 64 GB the
    overlay's 8g / 16g / 48G with a 5g transaction cap.
    """
    neo4j = max(2, min(PROD_NEO4J_GB, total_gb // 2))
    heap = max(1, min(PROD_HEAP_GB, neo4j // 3))
    pagecache = neo4j - heap
    limit = max(neo4j + 1, min(2 * neo4j, total_gb - 4))
    return Sizing(heap, pagecache, limit, transaction_max_gb(heap))


def transaction_max_gb(heap_gb: float) -> int:
    """70% of the heap, rounded down to whole GB and at least 1 GB.

    That leaves the rest of the heap for Neo4j itself, so a large transaction is refused instead
    of exhausting the heap (the overlay exits the JVM on OutOfMemoryError). Only a heap of 1 GB
    or less, below the production minimum, gets a cap as large as the heap.
    """
    return max(1, math.floor(heap_gb * TRANSACTION_SHARE))


MEMORY_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": GIB}
MEMORY_VALUE = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[kKmMgG]?)$")


def memory_gb(value: str) -> float | None:
    """A Neo4j memory setting ("2G", "4096m", "8589934592") in GB, or None if unreadable."""
    match = MEMORY_VALUE.match(value.strip())
    if not match:
        return None
    return float(match["amount"]) * MEMORY_UNITS[match["unit"].lower()] / GIB


def plan_sizing(env_text: str, template_values: Mapping[str, str], sizing: Sizing) -> dict[str, str]:
    """Which sizing values deploy should write: only the ones nobody has chosen.

    A value counts as unset when it is absent or empty, and also when it is still exactly what
    `.env.example` ships (NEO4J_HEAP_SIZE=2G): that line is a base-stack-sized placeholder copied
    by `just env-init`, not a choice for this host, and left alone it would hold the production
    heap at 2 GB beside a 16 GB page cache. Once deploy has written its sizing (the marker comment
    is present) it never rewrites any of it, so an edit after the first run always sticks.

    The transaction cap follows the heap that will actually run: when the operator chose the
    heap, the cap is derived from their value rather than from the formula's.
    """
    if SIZING_MARKER in env_text:
        return {}
    current = parse_env(env_text)
    wanted = sizing.env()
    unset = [key for key in SIZING_KEYS if not current.get(key) or current[key] == template_values.get(key)]
    chosen_heap = memory_gb(current["NEO4J_HEAP_SIZE"]) if "NEO4J_HEAP_SIZE" not in unset else None
    if chosen_heap is not None:
        wanted["NEO4J_TRANSACTION_MEMORY_MAX"] = f"{transaction_max_gb(chosen_heap)}g"
    return {key: wanted[key] for key in unset}


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


def check_free(free_bytes: int, label: str, volumes: Sequence[DataVolume], existing: bool) -> Finding:
    """Judge one filesystem against the summed shares of the data volumes that live on it."""
    free = free_bytes // 10**9
    need = sum(volume.minimum_gb for volume in volumes)
    recommended = sum(volume.recommended_gb for volume in volumes)
    names = ", ".join(volume.name for volume in volumes)
    if existing:
        if free < DISK_LOW_GB:
            return Finding("warn", f"Only {free} GB free under {label} ({names}); GrooveMap's data keeps growing, so free some space soon.")
        return Finding("ok", f"Disk: {free} GB free under {label} ({names})")
    if free < need:
        return Finding(
            "stop",
            f"Only {free} GB free under {label}, which holds {names}; they need about {need} GB ({recommended} GB recommended with MusicBrainz).",
            "Free space there, or move the large volumes to a bigger disk with docker-compose.override.yml (docs/homelab.md, Data on a large disk).",
            forcible=True,
        )
    if free < recommended:
        estimate = " (an estimate; it has not been measured yet)" if any(volume.name == "musicbrainz_data" for volume in volumes) else ""
        return Finding(
            "warn",
            f"{free} GB free under {label} ({names}): enough for the minimum of {need} GB, but {recommended} GB is recommended{estimate}.",
        )
    return Finding("ok", f"Disk: {free} GB free under {label} ({names})")


def check_unmeasured(volumes: Sequence[DataVolume], reason: str) -> Finding:
    need = sum(volume.minimum_gb for volume in volumes)
    recommended = sum(volume.recommended_gb for volume in volumes)
    names = ", ".join(volume.name for volume in volumes)
    return Finding(
        "warn",
        f"Could not measure free disk space for {names} ({reason}); make sure there is about {need} GB free "
        f"for them and {recommended} GB with MusicBrainz.",
    )


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


def parse_allowed_services(value: str) -> frozenset[str]:
    """The service names listed in OVERRIDE_ALLOW_SERVICES (comma- or space-separated)."""
    return frozenset(item for item in re.split(r"[,\s]+", value) if item)


def _joins_stack_network(service: Mapping[str, Any], networks: Mapping[str, Any]) -> bool:
    """True when the service is on the groovemap network, by key, by name or through a shared network_mode."""
    stack_names = {STACK_NETWORK, str((networks.get(STACK_NETWORK) or {}).get("name") or STACK_NETWORK)}
    for key in service.get("networks") or {}:
        if key in stack_names or str((networks.get(key) or {}).get("name") or key) in stack_names:
            return True
    # network_mode service:/container:/host shares another namespace; none and bridge stay apart.
    return service.get("network_mode") not in (None, "", "none", "bridge")


def _check_new_service(name: str, service: Mapping[str, Any], networks: Mapping[str, Any], allowed: frozenset[str]) -> list[Finding]:
    optin = f"To allow it, add {name} to {OVERRIDE_ALLOW_VARIABLE} in .env (comma-separated service names), then re-run"
    findings = []
    image = str(service.get("image") or "")
    if not DIGEST_PINNED.search(image):
        findings.append(
            Finding(
                "stop",
                f"{OVERRIDE_FILE} adds the {name} service with an image that is not pinned by digest ({image or 'no image'})",
                "Pin it as repo@sha256:<64 hex digits>; a tag can change under the stack. This rule has no opt-in.",
            )
        )
    if service.get("build"):
        findings.append(Finding("stop", f"{OVERRIDE_FILE} adds the {name} service with a build section", "Use a prebuilt image pinned by digest."))
    if name not in allowed:
        if service.get("ports"):
            findings.append(Finding("stop", f"{OVERRIDE_FILE} adds the {name} service and publishes ports", f"{optin}."))
        if _joins_stack_network(service, networks):
            findings.append(
                Finding(
                    "stop",
                    f"{OVERRIDE_FILE} adds the {name} service on the {STACK_NETWORK} network",
                    f"{optin}. Without it, leave it on the default network (omit networks).",
                )
            )
    return findings


def check_override(before: Mapping[str, Any], after: Mapping[str, Any], allowed: frozenset[str] = frozenset()) -> list[Finding]:
    """Compare the rendered stack without and with docker-compose.override.yml.

    The override is for host-specific settings: reverse-proxy labels and networks, data on
    another disk, restart policies, the network's subnet. Anything that would bypass the
    reviewed digests, the import gate or the data-tier bind addresses is a stop. Comparing
    Compose's own renders (`config --format json`) judges what the merge does, `!override` and
    `!reset` included, rather than how the file spells it.

    Services the override adds must be pinned by digest. One that publishes ports or joins the
    groovemap network (a forwarder into the data tier, say) must also be named in `allowed`.
    A new service on the project's default network alone cannot reach the stack, so it passes.
    """
    findings = []
    rendered = after.get("services") or {}
    for name, service in (before.get("services") or {}).items():
        if name not in rendered:
            findings.append(Finding("stop", f"{OVERRIDE_FILE} removes the {name} service", "Leave the stack's services in place."))
            continue
        changed = rendered[name]
        for key, why in OVERRIDE_FIXED:
            if service.get(key) != changed.get(key):
                findings.append(Finding("stop", f"{OVERRIDE_FILE} changes {key} on {name}", f"Remove it: {why}."))
        if name in DATA_TIER:
            for key in OVERRIDE_DATA_TIER_FIXED:
                if service.get(key) != changed.get(key):
                    findings.append(
                        Finding(
                            "stop",
                            f"{OVERRIDE_FILE} changes {key} on {name}",
                            "Data-tier ports move only with DATA_BIND_ADDRESS, INTERNAL_BIND_ADDRESS and the *_HOST_PORT variables in .env, "
                            "so they stay off the LAN.",
                        )
                    )
            for key, why in OVERRIDE_DATA_TIER_ISOLATED:
                if service.get(key) != changed.get(key):
                    findings.append(Finding("stop", f"{OVERRIDE_FILE} changes {key} on {name}", f"Remove it: {why}."))
    for name, service in rendered.items():
        if name not in (before.get("services") or {}):
            findings += _check_new_service(name, service, after.get("networks") or {}, allowed)
    if not findings:
        findings.append(Finding("ok", f"{OVERRIDE_FILE} changes only host-specific settings"))
    return findings


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
        except (FileNotFoundError, subprocess.TimeoutExpired):
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


@dataclass(frozen=True)
class Location:
    """Where a data volume's bytes land: a host path, or None with the reason it cannot be seen."""

    volume: DataVolume
    path: str | None
    reason: str = ""


def resolve_locations(config: Mapping[str, Any] | None, root_dir: str, local: bool, desktop: bool) -> list[Location]:
    """Find each data volume's host path in `docker compose config --format json`.

    The render already merges docker-compose.override.yml, so a service-level bind mount has
    replaced the named volume at its target, and a named volume carries its `driver_opts`. A
    named volume on the default driver lives under the Docker data root. Without a render
    (None) every volume is assumed to be one of those.
    """
    services = (config or {}).get("services") or {}
    top_level = (config or {}).get("volumes") or {}
    locations = []
    for volume in DATA_VOLUMES:
        mount: Mapping[str, Any] = {"type": "volume", "source": volume.name}
        if config is not None:
            mounts = (services.get(volume.service) or {}).get("volumes") or []
            found = next((entry for entry in mounts if isinstance(entry, dict) and entry.get("target") == volume.target), None)
            if found is None:
                continue
            mount = found
        locations.append(_locate(volume, mount, top_level.get(mount.get("source")) or {}, root_dir, local, desktop))
    return locations


def _locate(volume: DataVolume, mount: Mapping[str, Any], definition: Mapping[str, Any], root_dir: str, local: bool, desktop: bool) -> Location:
    if not local:
        return Location(volume, None, "the Docker engine is remote")
    if mount.get("type") == "bind":
        return Location(volume, str(mount.get("source", "")) or None, "" if mount.get("source") else "no source path")
    if definition.get("external"):
        return Location(volume, None, "it is an external volume")
    driver = str(definition.get("driver") or "local")
    options = definition.get("driver_opts") or {}
    if driver != "local":
        return Location(volume, None, f"it uses the {driver} volume driver")
    if options:
        if options.get("type", "none") != "none" or "bind" not in str(options.get("o", "")).split(","):
            return Location(volume, None, f"it is mounted as {options.get('type', 'none')} ({options.get('o', '')})")
        if desktop:
            return Location(volume, None, f"{options.get('device')} is a path inside Docker Desktop's VM")
        return Location(volume, str(options.get("device", "")) or None, "" if options.get("device") else "no device path")
    if desktop:
        return Location(volume, None, "Docker Desktop keeps its data inside its VM disk image")
    if not root_dir:
        return Location(volume, None, "Docker did not report its data root")
    return Location(volume, root_dir)


def probe_path(path: str, walk_up: bool = True) -> tuple[int, int]:
    """(filesystem id, free bytes) for `path`, or for its nearest existing ancestor.

    A bind-mount target that is not created yet will be made on its parent's filesystem. The
    Docker data root is different: if it is not visible from here, nothing says where it is.
    """
    target = Path(path)
    while walk_up and not target.exists() and target != target.parent:
        target = target.parent
    return target.stat().st_dev, int(shutil.disk_usage(target).free)


def check_volumes(
    locations: Sequence[Location], root_dir: str, local: bool, desktop: bool, existing: bool, probe: Callable[[str], tuple[int, int]]
) -> list[Finding]:
    """One finding per filesystem holding data volumes, plus one for what cannot be measured."""
    placed: dict[int, tuple[str, int, list[DataVolume]]] = {}
    unmeasured: dict[str, list[DataVolume]] = {}

    def place(volume: DataVolume, path: str | None, reason: str) -> None:
        if path is not None:
            try:
                device, free = probe(path)
            except OSError as error:
                path, reason = None, f"{path}: {error.strerror or error}"
            else:
                placed.setdefault(device, (path, free, []))[2].append(volume)
                return
        unmeasured.setdefault(reason, []).append(volume)

    for location in locations:
        place(location.volume, location.path, location.reason)
    if not local:
        place(DOCKER_ROOT_SHARE, None, "the Docker engine is remote")
    elif desktop:
        place(DOCKER_ROOT_SHARE, None, "Docker Desktop keeps its data inside its VM disk image")
    else:
        place(DOCKER_ROOT_SHARE, root_dir or None, "Docker did not report its data root")
    findings = [check_free(free, path, volumes, existing) for path, free, volumes in placed.values()]
    findings += [check_unmeasured(volumes, reason) for reason, volumes in unmeasured.items()]
    return findings


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
    findings = [
        check_architecture(str(info.get("Architecture", "")), desktop, binfmt() if local else None),
        check_memory(int(info.get("MemTotal") or 0), mode, desktop),
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


def disk_check(
    config: Mapping[str, Any] | None,
    *,
    force: bool,
    docker: Docker,
    environ: Mapping[str, str],
    probe: Callable[[str], tuple[int, int]] | None = None,
) -> int:
    """Check free space behind every data volume of the rendered stack; 0 to go ahead, 3 to stop."""
    info, error = docker.info()
    if info is None:
        print(f"error: Docker Engine is not reachable: {error}", file=sys.stderr)
        return 1
    desktop = "Docker Desktop" in str(info.get("OperatingSystem", ""))
    local = local_engine(environ)
    root_dir = str(info.get("DockerRootDir", ""))
    existing = bool(docker.groovemap_containers(all_states=True))
    probe = probe or (lambda path: probe_path(path, walk_up=path != root_dir))
    findings = check_volumes(resolve_locations(config, root_dir, local, desktop), root_dir, local, desktop, existing, probe)
    for finding in findings:
        show(finding)
    stops = [finding for finding in findings if finding.level == "stop"]
    if stops and not force:
        print("\nStopping before the stack starts: free space on the disk(s) marked [stop], or add --force to go ahead below the minimums anyway.")
        return 3
    if stops:
        print("\n--force given: going ahead below the disk minimums. The import may run out of disk.")
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
    write_env_text(env_path, text)
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


# --- console sign-in without TLS ---------------------------------------------------------

INSECURE_CONSOLE_VARIABLE = "GROOVEMAP_ALLOW_INSECURE_CONSOLE"
INSECURE_CONSOLE_MARKER = "# Written by just deploy at your request: the console's session cookie is not Secure (plain-http LAN)"
# Exit status of `console-http` when it changed .env and `dashboard` must be recreated to read it.
CONSOLE_RECREATE = 10
PROXY_DOCS = "skills/groovemap-homelab/references/reverse-proxy.md and docs/homelab.md"


def is_plain_http_remote(url: str) -> bool:
    """True for an http:// URL whose host is not this machine (localhost or a loopback address)."""
    scheme, separator, rest = url.strip().partition("://")
    if not separator or scheme.lower() != "http":
        return False
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
    if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def insecure_console_allowed(env_values: Mapping[str, str], environ: Mapping[str, str]) -> bool:
    """The opt-in as docker-compose.prod.yml reads it: only the exact value `true` counts."""
    return effective(INSECURE_CONSOLE_VARIABLE, env_values, environ) == "true"


def console_needs_https(mode: str, env_values: Mapping[str, str], environ: Mapping[str, str], hostname: str) -> tuple[str, str] | None:
    """(console URL, APP_BASE_URL) when the console's Secure cookie will be dropped, else None.

    Either the configured public origin (APP_BASE_URL) or the console's own published URL
    being plain http on a non-loopback host triggers it, unless the opt-in is set. The console
    URL is the one the endpoints block prints.
    """
    if mode != "prod" or insecure_console_allowed(env_values, environ):
        return None
    console = next(url for what, url, _ in endpoints(mode, env_values, environ, hostname) if what == "Operations console")
    base_url = effective("APP_BASE_URL", env_values, environ)
    if is_plain_http_remote(base_url) or is_plain_http_remote(console):
        return console, base_url
    return None


def console_https_notice(console: str, base_url: str, command: str) -> str:
    if is_plain_http_remote(base_url):
        reason = f"GrooveMap is configured for plain http (APP_BASE_URL={base_url}), so the console at {console}"
    else:
        reason = f"The console at {console} is published over plain http, so it"
    return "\n".join(
        [
            f"    {reason}",
            "    is reached without TLS. Its session cookie is Secure, and a browser drops a Secure",
            "    cookie on http away from localhost, so signing in will silently return to the login form.",
            "",
            "    Pick one:",
            "      1. Recommended: put a TLS reverse proxy in front of the console and set APP_BASE_URL to",
            f"         its https:// address. See {PROXY_DOCS}.",
            f"      2. On a trusted LAN only: set {INSECURE_CONSOLE_VARIABLE}=true in .env, which turns",
            f"         the Secure attribute off for the console. Then recreate it: {command}",
            "         The session cookie then travels unencrypted, so anyone on the network path can read it.",
        ]
    )


def console_http(
    mode: str,
    *,
    env_path: Path,
    environ: Mapping[str, str],
    hostname: str,
    recreate_command: str,
    interactive: bool,
    ask: Callable[[str], str] = input,
) -> int:
    """Explain the sign-in limitation; interactively offer the opt-in (default no).

    Returns CONSOLE_RECREATE when .env was changed and `dashboard` has to be recreated, else 0.
    """
    env_values = read_env(env_path)
    found = console_needs_https(mode, env_values, environ, hostname)
    if found is None:
        return 0
    print("\n==> Console sign-in")
    print(console_https_notice(*found, recreate_command))
    if not interactive:
        return 0
    if INSECURE_CONSOLE_VARIABLE in environ:
        print(
            f"\n    {INSECURE_CONSOLE_VARIABLE} is set to {environ[INSECURE_CONSOLE_VARIABLE]!r} in your shell, which outranks .env; unset it to change this."
        )
        return 0
    if ask(f"\n    Set {INSECURE_CONSOLE_VARIABLE}=true now and recreate the console? [y/N] ").strip().lower() not in ("y", "yes"):
        print("    Left as it is. The console's sign-in needs HTTPS.")
        return 0
    text = env_path.read_text() if env_path.exists() else ""
    write_env_text(env_path, set_env_values(text, {INSECURE_CONSOLE_VARIABLE: "true"}, INSECURE_CONSOLE_MARKER))
    print(f"    Set {INSECURE_CONSOLE_VARIABLE}=true in .env.")
    return CONSOLE_RECREATE


# --- command line ----------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="deploy_helpers", description="Helpers for scripts/deploy.sh (`just deploy`).")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="check Docker, architecture, memory and ports; exit 0 to go ahead, 3 to stop")
    check.add_argument("--mode", choices=("prod", "dev"), default="prod")
    check.add_argument("--yes", action="store_true", help="go ahead under amd64 emulation without asking")
    check.add_argument("--force", action="store_true", help="go ahead below the memory minimum")
    disk = commands.add_parser("disk-check", help="check free space on the filesystem behind each data volume; exit 0 to go ahead, 3 to stop")
    disk.add_argument(
        "rendered", nargs="?", type=Path, help="`docker compose config --format json`; without it every volume is assumed to be on Docker's data root"
    )
    disk.add_argument("--force", action="store_true", help="go ahead below the disk minimums")
    sizing = commands.add_parser("configure", help="write Neo4j sizing and APP_BASE_URL into .env where they are unset (production overlay)")
    sizing.add_argument("--mode", choices=("prod", "dev"), default="prod")
    commands.add_parser("profile-enabled", help=f"exit 0 if COMPOSE_PROFILES (shell or .env) includes {PROFILE}, else 1")
    commands.add_parser("enable-profile", help=f"add {PROFILE} to COMPOSE_PROFILES in .env")
    name = commands.add_parser("project-name", help="print the Compose project name")
    name.add_argument("--project-name")
    urls = commands.add_parser("endpoints", help="print the configured browser-facing endpoints")
    urls.add_argument("--mode", choices=("prod", "dev"), default="prod")
    console = commands.add_parser("console-http", help="explain (and offer to fix) console sign-in on a plain-http host; exit 10 if .env changed")
    console.add_argument("--mode", choices=("prod", "dev"), default="prod")
    console.add_argument("--recreate-command", default="docker compose up -d --no-deps dashboard")
    override = commands.add_parser(
        "override-check", help=f"compare `docker compose config --format json` without and with {OVERRIDE_FILE}; exit 0 to go ahead, 3 to stop"
    )
    override.add_argument("before", type=Path, help="the render without the override")
    override.add_argument("after", type=Path, help="the render with the override")
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
    if args.command == "disk-check":
        try:
            config = json.loads(args.rendered.read_text()) if args.rendered else None
        except (OSError, json.JSONDecodeError) as problem:
            print(f"error: could not read the rendered stack: {problem}", file=sys.stderr)
            return 1
        return disk_check(config, force=args.force, docker=docker, environ=environ)
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
        write_env_text(ENV_FILE, set_env_values(ENV_FILE.read_text(), {"COMPOSE_PROFILES": merged}, PROFILE_MARKER))
        print(f"    Set COMPOSE_PROFILES={merged} in .env, so every later `docker compose up` keeps MusicBrainz running.")
        return 0
    if args.command == "project-name":
        print(project_name(args.project_name, env_values, environ))
        return 0
    if args.command == "console-http":
        return console_http(
            args.mode,
            env_path=ENV_FILE,
            environ=environ,
            hostname=short_hostname(),
            recreate_command=args.recreate_command,
            interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        )
    if args.command == "override-check":
        try:
            findings = check_override(
                json.loads(args.before.read_text()),
                json.loads(args.after.read_text()),
                parse_allowed_services(effective(OVERRIDE_ALLOW_VARIABLE, env_values, environ)),
            )
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: could not read the rendered stack: {error}", file=sys.stderr)
            return 1
        for finding in findings:
            show(finding)
        if any(finding.level == "stop" for finding in findings):
            print(f"\nStopping before the stack starts: fix {OVERRIDE_FILE} and re-run.")
            return 3
        return 0
    print(render_endpoints(endpoints(args.mode, env_values, environ, short_hostname())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
