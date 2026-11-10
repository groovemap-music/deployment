"""The local secret scan covers git-visible files only and still catches real secrets in them."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "secret-scan-worktree.sh"

# An AWS access key id shaped value, assembled at runtime so this file holds no literal key.
FAKE_SECRET = "AKIA" + "ZQ7M3K2XB5N4WJ6P"

GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "bash"

requires_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repository carrying the real gitleaks config and a .beads/ ignore rule."""
    _git(tmp_path, "init", "-q")
    shutil.copy(REPO_ROOT / ".gitleaks.toml", tmp_path / ".gitleaks.toml")
    (tmp_path / ".gitignore").write_text(".beads/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("clean\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "README.md", ".gitleaks.toml")
    return tmp_path


def _scan(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@requires_gitleaks
def test_clean_tree_passes(repo: Path) -> None:
    assert _scan(repo).returncode == 0


@requires_gitleaks
def test_secret_in_git_ignored_path_is_not_scanned(repo: Path) -> None:
    beads = repo / ".beads"
    beads.mkdir()
    (beads / "issues.jsonl").write_text(f'{{"aws": "{FAKE_SECRET}"}}\n', encoding="utf-8")

    result = _scan(repo)

    assert result.returncode == 0, result.stdout + result.stderr


@requires_gitleaks
def test_secret_in_untracked_unignored_file_is_caught(repo: Path) -> None:
    (repo / "notes.txt").write_text(f"key = {FAKE_SECRET}\n", encoding="utf-8")

    result = _scan(repo)

    assert result.returncode != 0, "a leak in an untracked, unignored file must fail the scan"


@requires_gitleaks
def test_secret_in_tracked_file_is_caught(repo: Path) -> None:
    (repo / "config.env").write_text(f"AWS_ACCESS_KEY_ID={FAKE_SECRET}\n", encoding="utf-8")
    _git(repo, "add", "config.env")

    result = _scan(repo)

    assert result.returncode != 0, "a leak in a tracked file must fail the scan"
