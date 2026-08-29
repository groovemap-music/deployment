"""Exercise repository validation scripts in-process so their behavior is covered."""

from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_image_policy_script() -> None:
    runpy.run_path(str(ROOT / "scripts/check-images.py"), run_name="__main__")


def test_license_policy_script() -> None:
    runpy.run_path(str(ROOT / "scripts/check-licenses.py"), run_name="__main__")
