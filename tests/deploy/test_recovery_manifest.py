"""`just recovery-manifest` (scripts/recovery_manifest.py) and deploy's refusal to regenerate a secret."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.deploy.test_deploy import Deploy


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "recovery_manifest.py"
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "git"
JUST = shutil.which("just") or "just"
DIGEST = "ghcr.io/groovemap-music/catalog-api@sha256:" + "ab" * 32

spec = importlib.util.spec_from_file_location("recovery_manifest", SCRIPT)
assert spec is not None and spec.loader is not None
recovery = importlib.util.module_from_spec(spec)
sys.modules["recovery_manifest"] = recovery
spec.loader.exec_module(recovery)


def _checkout(tmp_path: Path) -> Path:
    """A scratch deployment checkout with bootstrapped secrets, a .env and an override."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "create-secrets.sh", tmp_path / "scripts" / "create-secrets.sh")
    subprocess.run(
        [BASH, str(tmp_path / "scripts" / "create-secrets.sh")],
        capture_output=True,
        check=True,
        timeout=30,
        env={**os.environ, "RESEND_API_KEY": "re_live_sentinel"},
    )
    (tmp_path / ".env").write_text(
        f"CATALOG_API_IMAGE={DIGEST}\nAPP_BASE_URL=https://music.example\nGROOVEMAP_POSTGRES=external\nEXTERNAL_POSTGRES_HOST=db.lan\n"
    )
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n")
    subprocess.run([GIT, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True
    )
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "--root", str(root), *args], capture_output=True, text=True, timeout=60, check=False)


def test_manifest_lists_what_to_capture_and_prints_no_secret_value(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    result = _run(root)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for name in (
        "jwt_secret_key.txt",
        "encryption_master_key.txt",
        "postgres_password.txt",
        "valkey_password.txt",
        "neo4j_password.txt",
        "rabbitmq_password.txt",
    ):
        assert f"secrets/{name}: mode 0600" in out
    assert "sha256 " in out and ".env: mode" in out and "docker-compose.override.yml: mode" in out
    head = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    assert head in out
    assert f"CATALOG_API_IMAGE={DIGEST}" in out
    assert "PostgreSQL is EXTERNAL" in out and "encrypt" in out.lower() and "SOPS" in out
    # Not one secret value, nor an unrelated .env value, reaches the output (stdout or stderr).
    leaked = [(path.name, path.read_text()) for path in (root / "secrets").iterdir() if not path.name.endswith("_username.txt") and path.read_text()]
    leaked += [("env", "music.example"), ("env", "db.lan"), ("resend", "re_live_sentinel")]
    for name, value in leaked:
        assert value not in out + result.stderr, name


def test_manifest_flags_an_empty_secret_and_loose_modes(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    (root / "secrets" / "jwt_secret_key.txt").chmod(0o644)
    out = _run(root).stdout
    assert "secrets/nlq_api_key.txt" in out and "empty" in out
    assert "WARNING: group/world access" in out


def test_manifest_is_read_only(tmp_path: Path) -> None:
    root = _checkout(tmp_path)

    def state() -> dict[str, tuple[float, int]]:
        return {str(p.relative_to(root)): (p.stat().st_mtime, p.stat().st_size) for p in root.rglob("*") if ".git" not in p.parts and p.is_file()}

    before = state()
    assert _run(root).returncode == 0
    assert state() == before


def test_manifest_works_without_a_checkout_state(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert ".env: not present" in result.stdout and "unknown" in result.stdout


def test_verify_accepts_an_untouched_secrets_directory_and_rejects_a_replaced_one(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    record = tmp_path / "record"
    assert _run(root, "snapshot", str(record)).returncode == 0
    assert record.stat().st_mode & 0o777 == 0o600
    assert "jwt_secret_key" in record.read_text() and "re_live_sentinel" not in record.read_text()
    (root / "secrets" / "valkey_exporter_passwords.json").write_text("{}")  # derived: may change
    assert _run(root, "verify", str(record)).returncode == 0
    (root / "secrets" / "jwt_secret_key.txt").write_text("regenerated")
    refused = _run(root, "verify", str(record))
    assert refused.returncode == 3 and "secrets/jwt_secret_key.txt" in refused.stderr and "regenerated" not in refused.stderr


def test_verify_rejects_a_removed_secret(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    record = tmp_path / "record"
    _run(root, "snapshot", str(record))
    (root / "secrets" / "encryption_master_key.txt").unlink()
    assert _run(root, "verify", str(record)).returncode == 3


def test_deploy_refuses_a_bootstrap_that_would_regenerate_a_secret(tmp_path: Path) -> None:
    deploy = Deploy(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "jwt_secret_key.txt").write_text("restored-key")
    (tmp_path / "scripts" / "create-secrets.sh").write_text('#!/usr/bin/env bash\nprintf new >"$(dirname "$0")/../secrets/jwt_secret_key.txt"\n')
    result = deploy.run("--no-wait")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "secrets/jwt_secret_key.txt" in result.stderr and "restored-key" not in result.stdout + result.stderr
    assert deploy.ups == []


def test_deploy_keeps_restored_secrets(tmp_path: Path) -> None:
    deploy = Deploy(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "jwt_secret_key.txt").write_text("restored-key")
    result = deploy.run("--no-wait")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (secrets_dir / "jwt_secret_key.txt").read_text() == "restored-key"
    assert (secrets_dir / "neo4j_password.txt").exists()


@pytest.mark.skipif(shutil.which("just") is None, reason="just is not installed")
def test_just_recipe_runs_the_manifest() -> None:
    result = subprocess.run([JUST, "--dry-run", "recovery-manifest"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "recovery_manifest.py" in result.stderr
