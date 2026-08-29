"""Validate the deployment repository's first-party license metadata."""

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as source:
    project = tomllib.load(source)["project"]

assert project["license"] == "PolyForm-Noncommercial-1.0.0"
license_text = (ROOT / "LICENSE").read_text()
assert "PolyForm Noncommercial License 1.0.0" in license_text
assert "Required Notice:" in license_text
