#!/usr/bin/env python3
"""Prove rendered application log writers can use their real /logs tmpfs.

The caller supplies the active Compose render and the Compose command prefix that
produced it, followed by ``--``. Every service with a rendered ``/logs`` mount is
included, and Python file-log settings make a missing mount fail closed. Running
services are checked in place against Docker inspect and then exec'd with a harmless
shell probe. One-shot and job services use an entrypoint override with
``run --no-deps``; no service entrypoint, dependency, schema DDL, or background job
is started.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence


NEO4J_UID = "7474"
NEO4J_GID = "7474"
PROBE = r"""set -eu
expected_uid=$1
expected_gid=$2
actual_uid=$(id -u)
actual_gid=$(id -g)
test "$actual_uid" = "$expected_uid"
test "$actual_gid" = "$expected_gid"
test "$(stat -c '%u:%g' /logs)" = "$expected_uid:$expected_gid"
awk '$5 == "/logs" { for (i = 6; i <= NF; i++) if ($i == "-") { if ($(i + 1) == "tmpfs") found = 1 } } END { exit !found }' /proc/self/mountinfo
probe="/logs/.groovemap-tmpfs-probe-$$"
cleanup() { test -z "${probe:-}" || rm -f "$probe"; }
trap cleanup EXIT
umask 077
printf 'tmpfs-writable\n' > "$probe"
test -s "$probe"
test "$(stat -c '%u:%g' "$probe")" = "$expected_uid:$expected_gid"
rm "$probe"
probe=
test ! -e "/logs/.groovemap-tmpfs-probe-$$"
"""


class ProbeError(RuntimeError):
    """The render or a runtime ownership proof did not meet the contract."""


def _environment(service: dict[str, Any]) -> dict[str, Any]:
    value = service.get("environment") or {}
    if not isinstance(value, dict):
        raise ProbeError("rendered service environment must be a mapping")
    return value


def _is_log_writer(name: str, service: dict[str, Any]) -> bool:
    env = _environment(service)
    image = str(service.get("image", ""))
    if name == "neo4j" or image.startswith("neo4j:") or "LOG_FILE_MAX_BYTES" in env or "LOG_FILE" in env:
        return True
    for mount in service.get("tmpfs") or []:
        if isinstance(mount, str) and mount.partition(":")[0] == "/logs":
            return True
        if isinstance(mount, dict) and mount.get("target") == "/logs":
            return True
    return any(isinstance(mount, dict) and mount.get("target") == "/logs" for mount in service.get("volumes") or [])


def _logs_mount(service: dict[str, Any], name: str) -> dict[str, str]:
    mounts: list[dict[str, str]] = []
    for value in service.get("tmpfs") or []:
        if isinstance(value, str):
            target, _separator, raw_options = value.partition(":")
            if target == "/logs":
                mounts.append(dict(option.partition("=")[::2] for option in raw_options.split(",") if option))
        elif isinstance(value, dict) and value.get("target") == "/logs":
            mounts.append({str(key): str(item) for key, item in value.items() if key != "target"})
    if len(mounts) != 1:
        raise ProbeError(f"{name}: expected exactly one tmpfs mount at /logs, found {len(mounts)}")
    return mounts[0]


def _parse_options(raw_options: str) -> dict[str, str]:
    return dict(option.partition("=")[::2] for option in raw_options.split(",") if option)


def _expected_identity(name: str, service: dict[str, Any], options: dict[str, str]) -> tuple[str, str]:
    if name == "neo4j" or str(service.get("image", "")).startswith("neo4j:"):
        expected = (NEO4J_UID, NEO4J_GID)
    else:
        user = str(service.get("user", ""))
        match = re.fullmatch(r"(\d+):(\d+)", user)
        if not match:
            raise ProbeError(f"{name}: expected a numeric Compose user, got {user!r}")
        expected = (match.group(1), match.group(2))
    configured = (options.get("uid", ""), options.get("gid", ""))
    if configured != expected:
        raise ProbeError(f"{name}: /logs tmpfs uid/gid {configured!r} do not match runtime {expected!r}")
    return expected


def log_writer_inventory(render: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return runtime (service, uid, gid) writers represented in this render.

    The service set is taken from Compose, so profile selection at each smoke
    phase controls which images are instantiated. Every service with a configured
    /logs mount is included, even if it currently logs to stdout; shared Python
    file-log settings also make a missing /logs mount fail closed.
    """
    services = render.get("services")
    if not isinstance(services, dict):
        raise ProbeError("rendered Compose config has no services mapping")
    writers: list[tuple[str, str, str]] = []
    for name, service in services.items():
        if not isinstance(service, dict) or not _is_log_writer(str(name), service):
            continue
        options = _logs_mount(service, str(name))
        uid, gid = _expected_identity(str(name), service, options)
        writers.append((str(name), uid, gid))
    if not writers:
        raise ProbeError("render contains no /logs mounts or file writers to probe")
    return writers


def probe_commands(compose_prefix: Sequence[str], writers: Sequence[tuple[str, str, str]]) -> list[list[str]]:
    """Build isolated Compose run commands, keeping service entrypoints disabled."""
    if not compose_prefix:
        raise ProbeError("a Docker Compose command prefix is required")
    commands: list[list[str]] = []
    for service, uid, gid in writers:
        commands.append(
            [
                *compose_prefix,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--user",
                f"{uid}:{gid}",
                "--entrypoint",
                "/bin/sh",
                service,
                "-c",
                PROBE,
                "logs-tmpfs-probe",
                uid,
                gid,
            ]
        )
    return commands


def _compose_inspect(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError(f"could not read Docker inspect data {path}: {error}") from error
    if not isinstance(raw, list):
        raise ProbeError("Docker inspect data must be an array")
    containers: dict[str, dict[str, Any]] = {}
    for container in raw:
        if not isinstance(container, dict):
            continue
        config = container.get("Config") or {}
        labels = config.get("Labels") or {}
        if labels.get("com.docker.compose.oneoff", "False").lower() == "true":
            continue
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        if service in containers:
            raise ProbeError(f"Docker inspect data has multiple non-one-off containers for {service}")
        containers[str(service)] = container
    return containers


def _is_one_shot_or_job(service: dict[str, Any]) -> bool:
    profiles = service.get("profiles") or []
    return service.get("restart") in ("no", "none") or "jobs" in profiles


def _check_live_mount(name: str, container: dict[str, Any], expected: dict[str, str]) -> str:
    container_id = str(container.get("Id", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
        raise ProbeError(f"{name}: running Compose container has no valid inspected ID")
    host_config = container.get("HostConfig") or {}
    tmpfs = host_config.get("Tmpfs") or {}
    raw_options = tmpfs.get("/logs") if isinstance(tmpfs, dict) else None
    if not isinstance(raw_options, str):
        raise ProbeError(f"{name}: running container HostConfig.Tmpfs has no /logs entry")
    options = _parse_options(raw_options)
    for key in ("uid", "gid", "mode", "size"):
        if options.get(key) != expected.get(key):
            raise ProbeError(f"{name}: running container HostConfig.Tmpfs {key} does not match the Compose render")
    for flag in ("noexec", "nosuid"):
        if flag in expected and flag not in options:
            raise ProbeError(f"{name}: running container HostConfig.Tmpfs is missing {flag} from the Compose render")
    # Docker exposes Compose's HostConfig.Tmpfs separately; it can legitimately
    # omit tmpfs entries from Mounts. If /logs is listed there, it must agree.
    mounts = [mount for mount in container.get("Mounts") or [] if mount.get("Destination") == "/logs"]
    if len(mounts) > 1 or (mounts and (mounts[0].get("Type") != "tmpfs" or mounts[0].get("RW") is not True)):
        raise ProbeError(f"{name}: running container Mounts has a conflicting /logs mount")
    return container_id


def _live_probe_command(docker_binary: str, container_id: str, uid: str, gid: str) -> list[str]:
    return [docker_binary, "exec", "--user", f"{uid}:{gid}", container_id, "/bin/sh", "-c", PROBE, "logs-tmpfs-probe", uid, gid]


def run_probes(
    rendered: Path,
    compose_prefix: Sequence[str],
    inspect: Path | None = None,
    runner: Any = subprocess.run,
) -> None:
    try:
        render = json.loads(rendered.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError(f"could not read Compose render {rendered}: {error}") from error
    writers = log_writer_inventory(render)
    active = _compose_inspect(inspect) if inspect else {}
    if compose_prefix[:2] != ["docker", "compose"]:
        raise ProbeError("Compose command prefix must begin with docker compose")
    fresh_by_service = dict(zip((name for name, _uid, _gid in writers), probe_commands(compose_prefix, writers), strict=True))
    for service_name, uid, gid in writers:
        service = render["services"][service_name]
        container = active.get(service_name)
        status = str(((container or {}).get("State") or {}).get("Status", ""))
        if container and status == "running":
            container_id = _check_live_mount(service_name, container, _logs_mount(service, service_name))
            command = _live_probe_command(compose_prefix[0], container_id, uid, gid)
        elif _is_one_shot_or_job(service) or inspect is None:
            command = fresh_by_service[service_name]
        elif container:
            raise ProbeError(f"{service_name}: expected active log-writer container is {status or 'not running'}")
        else:
            raise ProbeError(f"{service_name}: active log-writer has no inspected container")
        result = runner(command, capture_output=True, text=True, check=False)
        if result.returncode:
            detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            raise ProbeError(
                f"{service_name}: /logs tmpfs runtime ownership/write probe failed (exit {result.returncode})" + (f"\n{detail}" if detail else "")
            )
        print(f"ok {service_name}: /logs is tmpfs, writable/removable as {uid}:{gid}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered", type=Path, required=True, help="JSON output from the active Compose config")
    parser.add_argument("--inspect", type=Path, help="Docker inspect JSON for the running Compose project")
    parser.add_argument("compose_prefix", nargs=argparse.REMAINDER, help="Compose command prefix after --, e.g. docker compose --project-name smoke")
    args = parser.parse_args(argv)
    prefix = list(args.compose_prefix)
    if prefix[:1] == ["--"]:
        prefix.pop(0)
    try:
        run_probes(args.rendered, prefix, args.inspect)
    except ProbeError as error:
        print(f"logs-tmpfs probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
