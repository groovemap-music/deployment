"""The bundled postgres's /dev/shm comes from POSTGRES_SHM_SIZE, which defaults to 2g.

Parallel hash joins keep their hash tables in dynamic shared memory, which Docker caps at 64 MB by
default. A parallel query then fails with "could not resize shared memory segment". The default
is sized from the production parallel settings (see the comment in docker-compose.yml).
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
GIB_2 = str(2 * 1024**3)
GIB_4 = str(4 * 1024**3)


def render(shm: str | None) -> dict[str, object]:
    """`docker compose config` on the production files, the way the tableinator memory test renders them."""
    if DOCKER is None:
        pytest.skip("docker is not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    environment.pop("POSTGRES_SHM_SIZE", None)
    environment["APP_BASE_URL"] = "http://localhost:8006"
    if shm is not None:
        environment["POSTGRES_SHM_SIZE"] = shm
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
    service: dict[str, object] = json.loads(result.stdout)["services"]["postgres"]
    return service


def test_the_compose_file_defaults_shm_to_two_gib() -> None:
    text = (ROOT / "docker-compose.yml").read_text()
    assert text.count("shm_size: ${POSTGRES_SHM_SIZE:-2g}") == 1


def test_unset_renders_two_gib() -> None:
    service = render(None)
    assert str(service["shm_size"]) == GIB_2


def test_the_setting_overrides_the_default() -> None:
    service = render("4g")
    assert str(service["shm_size"]) == GIB_4


def test_the_docs_name_the_setting() -> None:
    assert "POSTGRES_SHM_SIZE" in (ROOT / "docs" / "configuration.md").read_text()
    assert "POSTGRES_SHM_SIZE" in (ROOT / "docs" / "homelab.md").read_text()
