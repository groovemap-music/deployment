"""Functional test for scripts/create-secrets.sh.

Asserts the bootstrap script generates valkey_password.txt alongside every
other prod secret — previously valkey had no secret file generated at all,
so a fresh prod deploy following the documented `bash scripts/create-secrets.sh`
step left valkey unauthenticated even after the docker-compose.prod.yml wiring
was added.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "create-secrets.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def test_generates_valkey_password_secret(tmp_path: Path) -> None:
    # The script derives SECRETS_DIR from its own location
    # ("$(dirname script)/../secrets"), so run a copy from an equivalent
    # relative layout under tmp_path to avoid touching the real repo's
    # (gitignored) secrets/ directory.
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir()
    scratch_script = scratch_scripts / "create-secrets.sh"
    shutil.copy(SCRIPT, scratch_script)
    scratch_script.chmod(0o755)

    result = subprocess.run(
        [_BASH, str(scratch_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    secrets_dir = tmp_path / "secrets"
    valkey_secret = secrets_dir / "valkey_password.txt"
    assert valkey_secret.exists()
    assert valkey_secret.read_text().strip() != ""
    # Every other secret must still be generated too (no regression).
    assert (secrets_dir / "neo4j_password.txt").exists()
    assert (secrets_dir / "rabbitmq_password.txt").exists()


GENERATED_PASSWORD_FILES = (
    "postgres_password.txt",
    "rabbitmq_password.txt",
    "neo4j_password.txt",
    "valkey_password.txt",
    "grafana_admin_password.txt",
    "embedding_pipeline_password.txt",
)
# 192 bits was the entropy of the previous `openssl rand -base64 24`.
MIN_ENTROPY_BITS = 192
RUNS = 25


def _run_in_scratch(tmp_path: Path) -> Path:
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir(parents=True)
    scratch_script = scratch_scripts / "create-secrets.sh"
    shutil.copy(SCRIPT, scratch_script)
    scratch_script.chmod(0o755)
    result = subprocess.run([_BASH, str(scratch_script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return tmp_path / "secrets"


def test_generated_passwords_use_a_safe_alphabet(tmp_path: Path) -> None:
    seen: dict[str, set[str]] = {name: set() for name in GENERATED_PASSWORD_FILES}
    for run in range(RUNS):
        secrets_dir = _run_in_scratch(tmp_path / f"run{run}")
        for name in GENERATED_PASSWORD_FILES:
            value = (secrets_dir / name).read_text()
            assert re.fullmatch(r"[A-Za-z0-9]+", value), f"{name} has unsafe characters"
            assert len(value) * math.log2(62) >= MIN_ENTROPY_BITS, f"{name} is too short"
            seen[name].add(value)
    # Distinct per run and per file: nothing is constant or shared.
    for name, values in seen.items():
        assert len(values) == RUNS, name
    assert len(set().union(*seen.values())) == RUNS * len(GENERATED_PASSWORD_FILES)


def test_existing_secret_files_are_never_overwritten(tmp_path: Path) -> None:
    secrets_dir = _run_in_scratch(tmp_path)
    before = {name: (secrets_dir / name).read_text() for name in GENERATED_PASSWORD_FILES}
    (secrets_dir / "neo4j_password.txt").write_text("kept/+value")
    before["neo4j_password.txt"] = "kept/+value"
    subprocess.run([_BASH, str(tmp_path / "scripts" / "create-secrets.sh")], capture_output=True, check=True, timeout=30)
    after = {name: (secrets_dir / name).read_text() for name in GENERATED_PASSWORD_FILES}
    assert after == before


def test_valkey_exporter_password_file_is_derived_from_the_valkey_password(tmp_path: Path) -> None:
    """oliver006/redis_exporter reads a JSON map of Redis URI to password.

    The map is derived from valkey_password.txt, and its URI is the base file's
    REDIS_ADDR for the exporter, so the exporter authenticates with the
    password Valkey was started with.
    """
    secrets_dir = _run_in_scratch(tmp_path)
    password = (secrets_dir / "valkey_password.txt").read_text()
    mapping = json.loads((secrets_dir / "valkey_exporter_passwords.json").read_text())

    assert mapping == {"redis://valkey:6379": password}
    assert (secrets_dir / "valkey_exporter_passwords.json").stat().st_mode & 0o777 == 0o600
    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    assert base["services"]["valkey-exporter"]["environment"]["REDIS_ADDR"] in mapping


def test_valkey_exporter_password_file_follows_an_existing_valkey_password(tmp_path: Path) -> None:
    """An existing valkey_password.txt is kept, and the derived map is refreshed to match it."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "valkey_password.txt").write_text('pa/ss+"word\\')
    (secrets_dir / "valkey_exporter_passwords.json").write_text('{"redis://valkey:6379": "stale"}')

    _run_in_scratch(tmp_path)

    assert (secrets_dir / "valkey_password.txt").read_text() == 'pa/ss+"word\\'
    mapping = json.loads((secrets_dir / "valkey_exporter_passwords.json").read_text())
    assert mapping == {"redis://valkey:6379": 'pa/ss+"word\\'}
