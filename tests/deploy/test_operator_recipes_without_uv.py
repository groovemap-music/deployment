"""Operator recipes must run on a host with only Docker, git and python3 >= 3.10 (just optional).

Each test runs with a PATH holding only a fake `docker`, a `python3` and the system tools, and
asserts uv is not resolvable. Exit codes of the Discogs import gate (0 complete, 3 importing,
2 cannot determine) must reach the caller unchanged through the runner and through `just`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.test_discogs_import_status import COMPLETE_GRAPH, COMPLETE_LATCH, IDLE_QUEUES, graph_output, latch_output


ROOT = Path(__file__).resolve().parents[2]
JUST = shutil.which("just")

FAKE_DOCKER = """#!/usr/bin/env bash
# Answers `docker compose ... exec -T <service> ...` from files in $FAKE_DOCKER_DIR.
dir="${FAKE_DOCKER_DIR:?}"
service=""
for ((i = 1; i <= $#; i++)); do
  if [[ "${!i}" == "-T" ]]; then j=$((i + 1)); service="${!j}"; fi
done
[[ -f "$dir/down" ]] && { echo "service \\"$service\\" is not running" >&2; exit 1; }
case "$service" in
  postgres) if [[ "${*: -1}" == *to_regclass* ]]; then cat "$dir/relations"; else cat "$dir/latch"; fi ;;
  neo4j) cat "$dir/graph" ;;
  rabbitmq) cat "$dir/queues" ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def no_uv_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(os.path.realpath(__import__("sys").executable))
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "relations").write_text("t|t\n")
    (state / "latch").write_text(COMPLETE_LATCH)
    (state / "graph").write_text(COMPLETE_GRAPH)
    (state / "queues").write_text(IDLE_QUEUES)
    # /usr/bin and /bin supply bash and coreutils; the host's uv (if any) lives elsewhere.
    path = f"{bin_dir}:/usr/bin:/bin"
    assert shutil.which("uv", path=path) is None
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEPLOY_", "COMPOSE_", "POSTGRES_", "NEO4J_", "RABBITMQ_"))}
    env.update(PATH=path, FAKE_DOCKER_DIR=str(state))
    return env


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=120)


def importing(env: dict[str, str]) -> None:
    state = Path(env["FAKE_DOCKER_DIR"])
    (state / "latch").write_text(latch_output())
    (state / "graph").write_text(graph_output())


def unknown(env: dict[str, str]) -> None:
    (Path(env["FAKE_DOCKER_DIR"]) / "down").write_text("")


SCENARIOS = {"complete": (None, 0), "importing": (importing, 3), "unknown": (unknown, 2)}


def command_for(via: str) -> list[str]:
    if via == "runner":
        return ["bash", "scripts/operator-python.sh", "scripts/discogs_import_status.py"]
    if JUST is None:
        pytest.skip("just is not installed; the runner variant covers the same path")
    return [JUST, "discogs-import-status"]


@pytest.mark.parametrize("via", ["runner", "just"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_import_status_exit_codes_pass_through_without_uv(no_uv_env: dict[str, str], via: str, scenario: str) -> None:
    setup, expected = SCENARIOS[scenario]
    if setup:
        setup(no_uv_env)
    result = run(command_for(via), no_uv_env)
    assert result.returncode == expected, result.stdout + result.stderr
    if expected == 3:
        assert "Exit 3 is normal progress, not a failure" in result.stdout
    else:
        assert "normal progress" not in result.stdout


def test_env_init_runs_without_uv(no_uv_env: dict[str, str]) -> None:
    result = run(["bash", "scripts/operator-python.sh", "scripts/env-init.py", "--help"], no_uv_env)
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_runner_rejects_an_interpreter_older_than_3_10(no_uv_env: dict[str, str], tmp_path: Path) -> None:
    old = tmp_path / "old-python"
    old.write_text("#!/usr/bin/env bash\nexit 1\n")
    old.chmod(0o755)
    env = {**no_uv_env, "DEPLOY_PYTHON": str(old)}
    result = run(["bash", "scripts/operator-python.sh", "scripts/env-init.py"], env)
    assert result.returncode == 1
    assert "Python 3.10 or newer is needed" in result.stderr


@pytest.mark.skipif(JUST is None, reason="just is not installed")
def test_maintainer_recipe_says_uv_is_missing(no_uv_env: dict[str, str]) -> None:
    result = run([str(JUST), "typecheck"], no_uv_env)
    assert result.returncode == 1
    assert "maintainer recipe and needs uv" in result.stderr


def test_every_operator_recipe_uses_the_runner() -> None:
    justfile = (ROOT / "Justfile").read_text()
    assert "{{operator_python}} scripts/discogs_import_status.py" in justfile
    assert "{{operator_python}} scripts/env-init.py" in justfile
    recipe = justfile.split("discogs-import-status *args:")[1].split("\n\n")[0]
    assert "uv run" not in recipe
