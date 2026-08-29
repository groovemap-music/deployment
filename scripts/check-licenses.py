"""Validate the deployment repository's first-party license metadata."""

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as source:
    project = tomllib.load(source)["project"]

assert project["license"] == "MIT"
license_text = (ROOT / "LICENSE").read_text()
assert license_text.startswith("MIT License\n")
assert "Permission is hereby granted, free of charge" in license_text
