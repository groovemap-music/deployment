"""The SQL loader's memory cap lives in Compose, where a recreate keeps it.

Lux's 30 GiB cap was set with `docker update`, so recreating the container on 2026-11-05 dropped it
to none until it was restored by hand. One setting now drives both limits, unset by default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
GIB_30 = str(30 * 1024**3)


def render(limit: str | None) -> dict[str, object]:
    """`docker compose config` on the production files, the way `scripts/check-compose.sh` renders them."""
    if DOCKER is None:
        pytest.skip("docker is not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    environment.pop("TABLEINATOR_MEMORY_LIMIT", None)
    environment["APP_BASE_URL"] = "http://localhost:8006"
    if limit is not None:
        environment["TABLEINATOR_MEMORY_LIMIT"] = limit
    command = (
        DOCKER,
        "compose",
        "--env-file",
        "config/validation.env",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.prod.yml",
        "config",
        "--format",
        "json",
        "--no-path-resolution",
    )
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60, env=environment, check=True)
    service: dict[str, object] = json.loads(result.stdout)["services"]["tableinator"]
    return service


def test_one_setting_drives_both_limits() -> None:
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert text.count("mem_limit: ${TABLEINATOR_MEMORY_LIMIT:-0}") == 1
    assert text.count("memswap_limit: ${TABLEINATOR_MEMORY_LIMIT:-0}") == 1


def test_lux_value_caps_memory_and_swap_alike() -> None:
    service = render("30g")
    assert str(service["mem_limit"]) == GIB_30
    assert str(service["memswap_limit"]) == GIB_30


def test_unset_leaves_every_other_homelab_unlimited() -> None:
    service = render(None)
    assert not service.get("mem_limit")
    assert not service.get("memswap_limit")


def test_the_docs_say_where_it_lives_and_that_docker_update_is_not_it() -> None:
    homelab = (ROOT / "docs" / "homelab.md").read_text()
    assert "TABLEINATOR_MEMORY_LIMIT=30g" in homelab
    assert "docker update" in homelab
    assert "TABLEINATOR_MEMORY_LIMIT" in (ROOT / "docs" / "configuration.md").read_text()
