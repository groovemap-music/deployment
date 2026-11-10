"""The SQL loaders' reload modes reach the containers from `.env`, and an unset value changes nothing.

`RELOAD_MODE` (brainztableinator) and `SEEN_LIST_MODE` / `SEEN_MIN_FRACTION` (tableinator) are read by the
loaders' own parsers. Compose defaults each to the loader's default: shadow, shadow and 0.95. A value that
is not a valid mode is passed through unchanged, so the loader fails its startup instead of Compose quietly
substituting something else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
LOADER_VARIABLES = ("RELOAD_MODE", "SEEN_LIST_MODE", "SEEN_MIN_FRACTION")
# service -> (variable, rendered default)
DEFAULTS = {
    "brainztableinator": {"RELOAD_MODE": "shadow"},
    "tableinator": {"SEEN_LIST_MODE": "shadow", "SEEN_MIN_FRACTION": "0.95"},
}


def render(**variables: str) -> dict[str, dict[str, str]]:
    """Environment of every loader service in the production stack, rendered the way `scripts/check-compose.sh` does."""
    if DOCKER is None:
        pytest.skip("docker is not installed")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_") and key not in LOADER_VARIABLES}
    environment["APP_BASE_URL"] = "http://localhost:8006"
    environment.update(variables)
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
    services: dict[str, dict[str, Any]] = json.loads(result.stdout)["services"]
    return {name: {key: str(value) for key, value in services[name]["environment"].items()} for name in DEFAULTS}


@pytest.mark.parametrize(("service", "variable", "default"), [(s, v, d) for s, mapping in DEFAULTS.items() for v, d in mapping.items()])
def test_unset_variables_render_the_loader_default(service: str, variable: str, default: str) -> None:
    assert render()[service][variable] == default


@pytest.mark.parametrize(("service", "variable", "default"), [(s, v, d) for s, mapping in DEFAULTS.items() for v, d in mapping.items()])
def test_an_empty_variable_renders_the_loader_default(service: str, variable: str, default: str) -> None:
    # The loaders treat an empty value as unset, so Compose's `:-` default matches them.
    assert render(**{variable: ""})[service][variable] == default


@pytest.mark.parametrize(
    ("service", "variable", "value"),
    [
        ("brainztableinator", "RELOAD_MODE", "touch"),
        ("tableinator", "SEEN_LIST_MODE", "touch"),
        ("tableinator", "SEEN_MIN_FRACTION", "0.9"),
    ],
)
def test_an_operator_value_reaches_the_loader(service: str, variable: str, value: str) -> None:
    assert render(**{variable: value})[service][variable] == value


@pytest.mark.parametrize(
    ("service", "variable", "value"),
    [
        ("brainztableinator", "RELOAD_MODE", "sometimes"),
        ("brainztableinator", "RELOAD_MODE", " Seen "),
        ("tableinator", "SEEN_LIST_MODE", "Sometimes"),
        ("tableinator", "SEEN_MIN_FRACTION", "1.5"),
    ],
)
def test_an_invalid_value_is_passed_through_not_rewritten(service: str, variable: str, value: str) -> None:
    # The loader rejects it at startup; Compose must not turn it into the default or another mode.
    assert render(**{variable: value})[service][variable] == value


def test_the_base_compose_defines_the_defaults_in_one_place_each() -> None:
    text = (ROOT / "docker-compose.yml").read_text()
    assert text.count('RELOAD_MODE: "${RELOAD_MODE:-shadow}"') == 1
    assert text.count('SEEN_LIST_MODE: "${SEEN_LIST_MODE:-shadow}"') == 1
    assert text.count('SEEN_MIN_FRACTION: "${SEEN_MIN_FRACTION:-0.95}"') == 1


def test_the_configuration_guide_names_each_variable_and_the_seen_rule() -> None:
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    for variable in LOADER_VARIABLES:
        assert variable in configuration
    assert "Never set `RELOAD_MODE=seen` or `SEEN_LIST_MODE=seen` without a matching shadow reload" in configuration
