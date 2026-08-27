from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_performance_runner_requires_an_immutable_image() -> None:
    env = os.environ.copy()
    env.pop("PERFTEST_IMAGE", None)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "run-perftest.sh")],
        capture_output=True,
        env=env,
        text=True,
    )
    assert result.returncode == 2
    assert "@sha256" in result.stderr


def test_performance_source_is_not_duplicated_in_deployment() -> None:
    retained = {path.name for path in (ROOT / "tests" / "perftest").iterdir()}
    assert retained == {"README.md", "config.yaml"}
