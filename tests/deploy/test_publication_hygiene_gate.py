"""The local gate runs the same publication-hygiene scanner CI's required step runs."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "publication-hygiene.py"
PIN = ROOT / "scripts" / "publication-hygiene.sha"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# Assembled at runtime so no file in this repository contains an absolute home path.
GIT = shutil.which("git") or "git"
BAD_PATH = "/" + "Us" + "ers" + "/" + "someone" + "/" + "project" + "/file"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        (GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args),
        check=True,
        capture_output=True,
    )


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run((sys.executable, str(SCANNER), "--repo", str(repo), *args), capture_output=True, text=True, check=False)


def new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "clean.txt", "nothing here\n")
    return repo


def test_recorded_scanner_sha_matches_the_ci_pin() -> None:
    recorded = PIN.read_text().strip()
    pinned = re.findall(r"uses:\s+groovemap-music/automation/[^@\s]+@([0-9a-f]{40})", CI.read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", recorded)
    assert pinned
    assert set(pinned) == {recorded}, "bump scripts/publication-hygiene.py and .sha together with the ci.yml automation pin"


def test_clean_repository_passes(tmp_path: Path) -> None:
    assert scan(new_repo(tmp_path)).returncode == 0


def test_tracked_home_path_fails_and_names_the_file(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    commit(repo, "leaky.txt", f"see {BAD_PATH}\n")
    result = scan(repo)
    assert result.returncode == 1
    assert "leaky.txt" in result.stderr
    assert BAD_PATH not in result.stderr + result.stdout


def test_unpushed_commit_fails_even_when_a_later_commit_removed_the_path(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    git(repo, "tag", "published")
    commit(repo, "leaky.txt", f"see {BAD_PATH}\n")
    commit(repo, "leaky.txt", "fixed\n")
    assert scan(repo).returncode == 0, "the worktree alone is clean"
    result = scan(repo, "--base", "published", "--head", "HEAD")
    assert result.returncode == 1
    assert "leaky.txt" in result.stderr


def test_published_history_is_not_rescanned_for_a_new_range(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    commit(repo, "leaky.txt", f"see {BAD_PATH}\n")
    commit(repo, "leaky.txt", "fixed\n")
    git(repo, "tag", "published")
    commit(repo, "more.txt", "fine\n")
    assert scan(repo, "--base", "published", "--head", "HEAD").returncode == 0
