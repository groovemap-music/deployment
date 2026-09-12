"""Exercise repository validation scripts in-process so their behavior is covered."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml


if TYPE_CHECKING:
    from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def image_policy() -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "scripts/check-images.py"), run_name="check_images")


def test_image_policy_script() -> None:
    runpy.run_path(str(ROOT / "scripts/check-images.py"), run_name="__main__")


def test_image_policy_rejects_a_mutable_compose_reference() -> None:
    policy = image_policy()
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    compose["services"]["redis"]["image"] = "redis:latest"
    env_names = (".env.example", "config/validation.env")

    with pytest.raises(AssertionError, match="redis image is not digest-pinned"):
        policy["check_compose"](
            yaml.safe_dump(compose),
            {name: (ROOT / name).read_text() for name in env_names},
        )


def test_provenance_policy_rejects_modified_promoted_content() -> None:
    policy = image_policy()
    provenance = json.loads((ROOT / "config/provenance.json").read_text())
    promoted = {relative_path: (ROOT / "config" / relative_path).read_bytes() for relative_path in provenance}
    promoted["extraction-rules.yaml"] += b"\n# drift"

    with pytest.raises(AssertionError, match=r"extraction-rules\.yaml drifted"):
        policy["check_provenance"](provenance, promoted)


def test_license_policy_script() -> None:
    runpy.run_path(str(ROOT / "scripts/check-licenses.py"), run_name="__main__")


def test_compose_checker_only_expands_supported_configurations(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    docker.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$DOCKER_LOG"\n')
    docker.chmod(0o755)

    subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/check-compose.sh")],
        cwd=ROOT,
        check=True,
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}", "DOCKER_LOG": str(log)},
    )

    assert log.read_text().splitlines() == [
        "compose --env-file config/validation.env config --quiet",
        "compose --env-file config/validation.env -f docker-compose.yml -f docker-compose.prod.yml config --quiet",
        "compose --env-file config/validation.env -f docker-compose.yml -f docker-compose.smoke.yml config --quiet",
        "compose --env-file config/validation.env -f docker-compose.yml -f docker-compose.media-smoke.yml config --quiet",
    ]


def test_secret_scanner_exclusions_are_commit_exact() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    allowlists = config["allowlists"]
    assert len(allowlists) == 1
    assert set(allowlists[0]) == {"description", "commits"}
    assert all(len(commit) == 40 for commit in allowlists[0]["commits"])
    assert "secrets/" in (ROOT / ".gitignore").read_text().splitlines()


def test_resilience_rehearsal_retains_recovery_and_stays_operator_only() -> None:
    script = (ROOT / "scripts/test-database-resilience.sh").read_text()
    outage = script[script.index("simulate_outage()") : script.index("monitor_logs()")]
    assert outage.index('docker compose stop "$service"') < outage.index('docker compose start "$service"')
    for service in ("neo4j", "postgres", "rabbitmq"):
        assert f'simulate_outage "{service}"' in script

    justfile = (ROOT / "Justfile").read_text()
    check_line = next(line for line in justfile.splitlines() if line.startswith("check:"))
    assert "resilience" not in check_line
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        assert "test-database-resilience" not in workflow.read_text()
