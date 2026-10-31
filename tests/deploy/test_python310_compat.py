"""The deploy-time scripts must run on a stock distro python3 (3.10: Ubuntu 22.04, 3.11: Debian 12).

`uv run --python 3.10` provisions the interpreter, so this executes the scripts rather than only
compiling them: syntax (PEP 758 `except A, B`), 3.11+ stdlib names (datetime.UTC) and third-party
imports all fail here. There is no skip: without uv the test fails, because a green run that
never touched 3.10 is the failure this test exists to prevent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MIN_PYTHON = "3.10"
UV = shutil.which("uv")
# Deploy-time entry points with a --help. env-init.py loads check-images.py (a validator that imports
# yaml inside check_compose), so the probe below covers that import path instead.
HELP_SCRIPTS = ("deploy_helpers.py", "discogs_import_status.py", "env-init.py")

# Loads each script by path (as env-init.py does) and exercises code that differs across versions.
PROBE = """
import importlib.util, sys
from pathlib import Path

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

scripts = Path(sys.argv[1]) / "scripts"
status = load("discogs_import_status", scripts / "discogs_import_status.py")
load("deploy_helpers", scripts / "deploy_helpers.py")
env_init = load("env_init", scripts / "env-init.py")
assert "yaml" not in sys.modules, "yaml must stay a lazy import in check-images.py"
for stamp in ("2026-10-01T01:25:43.89805+00:00", "2026-10-01T00:00:00Z", "2026-10-01T01:25:43.5+00:00"):
    assert status._timestamp(stamp).tzinfo is not None, stamp
digests = env_init.load_release_digests()
assert digests and "yaml" not in sys.modules
print(sys.version_info[:2])
"""


def _python310(*args: str) -> subprocess.CompletedProcess[str]:
    assert UV is not None, f"uv is required to run the tests under Python {MIN_PYTHON}; install it (https://docs.astral.sh/uv/)"
    return subprocess.run(
        [UV, "run", "--no-project", "--python", MIN_PYTHON, "python", "-I", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=ROOT,
    )


@pytest.mark.parametrize("script", HELP_SCRIPTS)
def test_script_help_runs_on_python_3_10(script: str) -> None:
    result = _python310(f"scripts/{script}", "--help")
    assert result.returncode == 0, result.stderr


def test_deploy_time_code_paths_run_on_python_3_10_with_only_the_stdlib() -> None:
    result = _python310("-c", PROBE, str(ROOT))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"({MIN_PYTHON.replace('.', ', ')})"


def test_env_init_writes_env_on_python_3_10(tmp_path: Path) -> None:
    for relative in ("scripts/env-init.py", "scripts/check-images.py", ".env.example"):
        (tmp_path / relative).parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / relative, tmp_path / relative)
    for _ in range(2):
        result = _python310(str(tmp_path / "scripts" / "env-init.py"), "--force")
        assert result.returncode == 0, result.stderr
    assert "REPLACE_WITH_64_HEX_CHARACTERS" not in (tmp_path / ".env").read_text()
    assert len(list(tmp_path.glob(".env.*.bak"))) == 1
