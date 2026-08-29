from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (
    "cleanup-implausible-years.sh",
    "compute-label-stats.sh",
    "migrate-master-year-to-int.sh",
)


@pytest.fixture
def fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "docker"
    log = tmp_path / "docker.log"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >>"$DOCKER_LOG"\n'
        "if [[ \"${1:-}\" == inspect ]]; then printf 'true\\n'; else printf '%s\\n' \"${FAKE_DOCKER_RESULT:-0}\"; fi\n"
    )
    binary.chmod(0o755)
    return binary, log


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_migrations_default_to_non_mutating_dry_run(
    script_name: str,
    fake_docker: tuple[Path, Path],
) -> None:
    binary, log = fake_docker
    env = os.environ | {
        "PATH": f"{binary.parent}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "NEO4J_PASSWORD": "not-printed-test-value",
        "FAKE_DOCKER_RESULT": "1",
    }
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / script_name)],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    commands = log.read_text()
    assert "DRY RUN" in result.stdout
    assert "not-printed-test-value" not in commands
    assert " SET " not in commands
    assert "UPDATE " not in commands


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_migrations_reject_unknown_arguments(script_name: str) -> None:
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / script_name), "--unexpected"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_migrations_support_password_files(script_name: str) -> None:
    source = (ROOT / "scripts" / script_name).read_text()
    assert "NEO4J_PASSWORD_FILE" in source
    assert "--apply" in source
