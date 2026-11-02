#!/usr/bin/env python3
"""Capture inspected smoke-stack service logs and reject permission failures.

Only container IDs from the stage's Docker inspect snapshot are queried. Compose
one-off containers are excluded so deliberately failing negative probes cannot
pollute the positive service assertion.
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


PERMISSION_FAILURE = re.compile(r"permission denied|permissionerror", re.IGNORECASE)


class LogCheckError(RuntimeError):
    """The inspected positive service logs could not be collected or were unsafe."""


def service_containers(inspect: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (service, container-id) for non-one-off project containers."""
    found: list[tuple[str, str]] = []
    for item in inspect:
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        if str(labels.get("com.docker.compose.oneoff", "False")).lower() == "true":
            continue
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        container_id = str(item.get("Id", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
            raise LogCheckError(f"{service}: inspected service container has no valid ID")
        found.append((str(service), container_id))
    if not found:
        raise LogCheckError("inspect snapshot contains no positive Compose service containers")
    return found


def capture_and_check(inspect_path: Path, evidence_dir: Path, docker: str = "docker", runner: Any = subprocess.run) -> list[Path]:
    try:
        raw = json.loads(inspect_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LogCheckError(f"could not read Docker inspect data {inspect_path}: {error}") from error
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise LogCheckError("Docker inspect data must be an array of container records")

    containers = service_containers(raw)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    captured: list[Path] = []
    findings: list[str] = []
    for service, container_id in containers:
        destination = evidence_dir / f"{service}-{container_id[:12]}.log"
        result = runner([docker, "logs", "--timestamps", container_id], capture_output=True, text=True, check=False)
        content = result.stdout + result.stderr
        destination.write_text(content)
        captured.append(destination)
        if result.returncode:
            findings.append(f"{service} ({container_id[:12]}): docker logs exited {result.returncode}; see {destination.name}")
            continue
        matches = sum(1 for line in content.splitlines() if PERMISSION_FAILURE.search(line))
        if matches:
            findings.append(f"{service} ({container_id[:12]}): {matches} permission-denied match(es); see {destination.name}")

    report = evidence_dir / "permission-log-check.txt"
    if findings:
        report.write_text("FAIL positive service log scan\n" + "\n".join(findings) + "\n")
        raise LogCheckError(f"positive service log scan found failures; see {report}")
    report.write_text(f"PASS scanned {len(captured)} inspected service container log(s); no permission denied or PermissionError\n")
    return captured


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", type=Path, required=True, help="Docker inspect JSON for the current smoke project")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="directory for captured service logs and report")
    parser.add_argument("--docker", default="docker", help="Docker CLI executable")
    args = parser.parse_args(argv)
    try:
        paths = capture_and_check(args.inspect, args.evidence_dir, args.docker)
    except LogCheckError as error:
        print(f"positive service log check: {error}", file=sys.stderr)
        return 1
    print(f"PASS captured and checked {len(paths)} inspected positive service log(s); no permission denied or PermissionError")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
