"""The MusicBrainz loaders' memory caps live in Compose, where a recreate keeps them.

Lux's 30g and 20g were set with `docker update` during the 2026-11-07 first load, which any recreate
drops. The measured peaks (160 MiB and 136 MiB over 164 samples) size the 1g defaults.
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
GIB = 1024**3
SERVICES = {"brainztableinator": "BRAINZTABLEINATOR_MEMORY_LIMIT", "brainzgraphinator": "BRAINZGRAPHINATOR_MEMORY_LIMIT"}
PEAK_MIB = {"brainztableinator": 160, "brainzgraphinator": 136}


def render(service: str, **limits: str) -> dict[str, object]:
    """`docker compose config` on the production files, the way `scripts/check-compose.sh` renders them."""
    if DOCKER is None:
        pytest.skip("docker is not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_") and key not in SERVICES.values()}
    environment["APP_BASE_URL"] = "http://localhost:8006"
    environment.update(limits)
    command = (DOCKER, "compose", "--env-file", "config/validation.env", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml")
    result = subprocess.run(
        (*command, "--profile", "*", "config", "--format", "json", "--no-path-resolution"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=True,
    )
    rendered: dict[str, object] = json.loads(result.stdout)["services"][service]
    return rendered


@pytest.mark.parametrize("variable", SERVICES.values())
def test_one_setting_drives_both_limits(variable: str) -> None:
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert text.count(f"mem_limit: ${{{variable}:-1g}}") == 1
    assert text.count(f"memswap_limit: ${{{variable}:-1g}}") == 1


@pytest.mark.parametrize("service", SERVICES)
def test_the_default_is_a_cap_with_headroom_over_the_measured_peak(service: str) -> None:
    rendered = render(service)
    assert str(rendered["mem_limit"]) == str(GIB)
    assert str(rendered["memswap_limit"]) == str(GIB)
    assert int(str(rendered["mem_limit"])) >= 4 * PEAK_MIB[service] * 1024**2


@pytest.mark.parametrize(("service", "variable"), SERVICES.items())
def test_the_lux_value_caps_memory_and_swap_alike(service: str, variable: str) -> None:
    rendered = render(service, **{variable: "2g"})
    assert str(rendered["mem_limit"]) == str(2 * GIB)
    assert str(rendered["memswap_limit"]) == str(2 * GIB)


def test_the_docs_name_both_settings() -> None:
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    for variable in SERVICES.values():
        assert variable in configuration
