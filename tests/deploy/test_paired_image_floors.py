"""check-images refuses a reviewed release set whose images only work together but are not together."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
LOADER = "DISCOGS_SQL_LOADER_IMAGE"
SCHEMA = "DATABASE_SCHEMA_IMAGE"


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return runpy.run_path(str(ROOT / "scripts" / "check-images.py"))


def pair(policy: dict[str, Any], loader: str, schema: str) -> tuple[tuple[str, str, str, str], ...]:
    assert policy["UNRELEASED"] == "unreleased"
    return ((LOADER, loader, SCHEMA, schema),)


def test_the_veto_loader_is_tied_to_its_schema(policy: dict[str, Any]) -> None:
    assert [(row[0], row[2]) for row in policy["PAIRED_IMAGE_FLOORS"]] == [(LOADER, SCHEMA)]


def test_it_refuses_the_loader_against_an_older_schema(policy: dict[str, Any]) -> None:
    problems = policy["paired_floor_problems"]({LOADER: "0.5.0", SCHEMA: "0.6.3"}, pair(policy, "0.5.0", "0.7.0"))
    assert problems == [f"{LOADER} 0.5.0 needs {SCHEMA} 0.7.0 or newer, and the reviewed set has 0.6.3"]


def test_it_refuses_a_newer_loader_too(policy: dict[str, Any]) -> None:
    assert policy["paired_floor_problems"]({LOADER: "0.10.0", SCHEMA: "0.6.3"}, pair(policy, "0.5.0", "0.7.0"))


@pytest.mark.parametrize("schema", ["0.7.0", "0.7.1", "0.10.0", "1.0.0"])
def test_it_accepts_the_schema_at_or_above_the_floor(policy: dict[str, Any], schema: str) -> None:
    assert policy["paired_floor_problems"]({LOADER: "0.5.0", SCHEMA: schema}, pair(policy, "0.5.0", "0.7.0")) == []


def test_an_older_loader_needs_nothing(policy: dict[str, Any]) -> None:
    assert policy["paired_floor_problems"]({LOADER: "0.4.4", SCHEMA: "0.6.3"}, pair(policy, "0.5.0", "0.7.0")) == []


def test_the_rule_is_inert_while_both_are_unreleased(policy: dict[str, Any]) -> None:
    unreleased = policy["UNRELEASED"]
    assert policy["paired_floor_problems"]({LOADER: "9.9.9", SCHEMA: "0.0.1"}, pair(policy, unreleased, unreleased)) == []


def test_the_reviewed_set_passes_today(policy: dict[str, Any]) -> None:
    assert policy["paired_floor_problems"](policy["RELEASED_IMAGE_VERSIONS"]) == []


def _with(namespace: dict[str, Any], script: str) -> None:
    """Run check_declarations on a copy of the script with one line changed, so the assert itself is tested."""
    code = compile(script, "check-images.py", "exec")
    exec(code, namespace)  # noqa: S102 - the repository's own script
    namespace["check_declarations"]()


def test_check_images_itself_refuses_a_broken_pair(tmp_path: Path) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    row = '("DISCOGS_SQL_LOADER_IMAGE", UNRELEASED, "DATABASE_SCHEMA_IMAGE", UNRELEASED)'
    assert row in source
    broken = source.replace(row, '("DISCOGS_SQL_LOADER_IMAGE", "0.4.5", "DATABASE_SCHEMA_IMAGE", "0.8.0")')
    with pytest.raises(AssertionError, match=r"needs DATABASE_SCHEMA_IMAGE 0\.8\.0 or newer"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


@pytest.mark.parametrize(
    "row",
    [
        '("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", UNRELEASED)',
        '("DISCOGS_SQL_LOADER_IMAGE", UNRELEASED, "DATABASE_SCHEMA_IMAGE", "0.7.0")',
    ],
)
def test_check_images_refuses_half_a_pair(tmp_path: Path, row: str) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    broken = source.replace('("DISCOGS_SQL_LOADER_IMAGE", UNRELEASED, "DATABASE_SCHEMA_IMAGE", UNRELEASED)', row)
    assert broken != source
    with pytest.raises(AssertionError, match="both UNRELEASED or both set"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


def test_check_images_refuses_a_malformed_floor(tmp_path: Path) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    broken = source.replace(
        '("DISCOGS_SQL_LOADER_IMAGE", UNRELEASED, "DATABASE_SCHEMA_IMAGE", UNRELEASED)',
        '("DISCOGS_SQL_LOADER_IMAGE", "next", "DATABASE_SCHEMA_IMAGE", "0.7.0")',
    )
    with pytest.raises(AssertionError, match=r"X\.Y\.Z or UNRELEASED"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)
