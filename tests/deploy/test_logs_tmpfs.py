"""/logs is a per-service tmpfs owned by the runtime UID, never a named volume (gm-deployment-lvhq).

A named `*_logs` volume initialises with the image user's ownership (UID 1000), so under any other
host UID the services run as ${UID}:${GID} and cannot open /logs/<service>.log. These tests render
the real production stack under UID/GID 1001 and check the replacement: a size-capped tmpfs at
/logs owned by the runtime user, and a log rotation budget that fits inside it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
HOST_UID = "1001"
HOST_GID = "1002"
NEO4J_UID = "7474"
UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}

# Services built on the shared Python logging (common.config): LOG_FILE defaults to /logs/<service>.log
# and rotates by LOG_FILE_MAX_BYTES x LOG_FILE_BACKUP_COUNT.
PYTHON_LOG_SERVICES = [
    "schema-init",
    "api",
    "graphinator",
    "brainzgraphinator",
    "tableinator",
    "brainztableinator",
    "dashboard",
    "explore",
    "insights",
]
# The Rust extractors log JSON to stdout only; /logs exists in their image but nothing rotates there.
EXTRACTOR_SERVICES = ["extractor-discogs", "extractor-musicbrainz"]
RUNTIME_UID_SERVICES = [*PYTHON_LOG_SERVICES, *EXTRACTOR_SERVICES]
LOGS_SERVICES = [*RUNTIME_UID_SERVICES, "neo4j"]


@pytest.fixture(scope="module")
def render() -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary to render the Compose files")
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            "config/validation.env",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.prod.yml",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env={**os.environ, "UID": HOST_UID, "GID": HOST_GID},
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    config: dict[str, Any] = json.loads(rendered.stdout)
    return config


def _logs_mounts(service: dict[str, Any]) -> list[str]:
    return [entry for entry in service.get("tmpfs") or [] if entry.split(":", 1)[0] == "/logs"]


def _options(mount: str) -> dict[str, str]:
    _, _, raw = mount.partition(":")
    return dict(option.partition("=")[::2] for option in raw.split(","))


def _bytes(size: str) -> int:
    match = re.fullmatch(r"(\d+)([kmg])", size.lower())
    assert match, f"tmpfs size {size!r} must be an explicit k/m/g cap"
    return int(match[1]) * UNITS[match[2]]


def test_no_service_mounts_a_named_logs_volume(render: dict[str, Any]) -> None:
    assert not [name for name in render.get("volumes", {}) if name.endswith("_logs")]
    for name, service in render["services"].items():
        for volume in service.get("volumes") or []:
            assert not (volume.get("type") == "volume" and (volume.get("source", "").endswith("_logs") or volume.get("target") == "/logs")), name
        assert len(_logs_mounts(service)) <= 1, name


@pytest.mark.parametrize("name", LOGS_SERVICES)
def test_logs_is_a_capped_tmpfs_owned_by_the_runtime_user(render: dict[str, Any], name: str) -> None:
    service = render["services"][name]
    (mount,) = _logs_mounts(service)
    options = _options(mount)
    expected_uid, expected_gid = (NEO4J_UID, NEO4J_UID) if name == "neo4j" else (HOST_UID, HOST_GID)
    assert (options["uid"], options["gid"]) == (expected_uid, expected_gid)
    assert options["mode"] == "0750"
    assert {"noexec", "nosuid"} <= set(options)
    assert _bytes(options["size"]) <= 64 * UNITS["m"]
    if name != "neo4j":
        assert service["user"] == f"{HOST_UID}:{HOST_GID}"


@pytest.mark.parametrize("name", PYTHON_LOG_SERVICES)
def test_rotation_budget_fits_the_tmpfs_cap(render: dict[str, Any], name: str) -> None:
    service = render["services"][name]
    env = service["environment"]
    (mount,) = _logs_mounts(service)
    # The active file plus every backup may be full at once.
    budget = (int(env["LOG_FILE_BACKUP_COUNT"]) + 1) * int(env["LOG_FILE_MAX_BYTES"])
    assert budget <= _bytes(_options(mount)["size"])
    assert "LOG_FILE" not in env or str(env["LOG_FILE"]).startswith("/logs/")


@pytest.mark.parametrize("name", [*PYTHON_LOG_SERVICES, *EXTRACTOR_SERVICES])
def test_hardening_is_unchanged(render: dict[str, Any], name: str) -> None:
    service = render["services"][name]
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    if name not in EXTRACTOR_SERVICES:
        assert service["read_only"] is True
