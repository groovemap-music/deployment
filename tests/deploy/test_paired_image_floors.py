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


MB_LOADER = "MUSICBRAINZ_SQL_LOADER_IMAGE"
CATALOG = "CATALOG_API_IMAGE"
ANALYTICS = "ANALYTICS_ENGINE_IMAGE"


def test_both_loaders_are_tied_to_their_schema(policy: dict[str, Any]) -> None:
    assert list(policy["PAIRED_IMAGE_FLOORS"]) == [
        (LOADER, "0.5.0", SCHEMA, "0.8.0"),
        (MB_LOADER, "0.6.0", SCHEMA, "0.9.0"),
        (CATALOG, "0.11.0", SCHEMA, "0.11.0"),
        (CATALOG, "0.11.2", SCHEMA, "0.12.0"),
        (MB_LOADER, "0.8.0", SCHEMA, "0.14.0"),
        (ANALYTICS, "0.9.0", SCHEMA, "0.15.1"),
    ]


def test_the_musicbrainz_loader_is_refused_on_an_older_schema(policy: dict[str, Any]) -> None:
    """The real declaration: loader 0.8.0 on schema 0.8.0 (which has purge_vetoed but not the single key) is refused."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.8.0"})
    assert policy["paired_floor_problems"](versions) == [
        f"{MB_LOADER} 0.8.0 needs {SCHEMA} 0.9.0 or newer, and the reviewed set has 0.8.0",
        f"{CATALOG} 0.15.3 needs {SCHEMA} 0.11.0 or newer, and the reviewed set has 0.8.0",
        f"{CATALOG} 0.15.3 needs {SCHEMA} 0.12.0 or newer, and the reviewed set has 0.8.0",
        f"{MB_LOADER} 0.8.0 needs {SCHEMA} 0.14.0 or newer, and the reviewed set has 0.8.0",
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.8.0",
    ]
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.7.0"})
    assert len(policy["paired_floor_problems"](versions)) == 6  # the Discogs loader is refused too


def test_the_musicbrainz_refresh_owner_is_refused_without_the_refresh_lock(policy: dict[str, Any]) -> None:
    """The real declaration: loader 0.8.0 refreshes the shared derived tables, and schema 0.13.0 lacks the lock."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.13.0"})
    assert policy["paired_floor_problems"](versions) == [
        f"{MB_LOADER} 0.8.0 needs {SCHEMA} 0.14.0 or newer, and the reviewed set has 0.13.0",
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.13.0",
    ]
    older_loader = dict(versions, **{MB_LOADER: "0.7.0"})
    assert policy["paired_floor_problems"](older_loader) == [
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.13.0",
    ]


def test_the_promoted_catalog_api_is_refused_on_the_previous_schema(policy: dict[str, Any]) -> None:
    """The real declaration: catalog-api 0.15.3 reads release_search and master_search, which schema 0.10.0 lacks."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.10.0"})
    problems = policy["paired_floor_problems"](versions)
    assert problems == [
        f"{CATALOG} 0.15.3 needs {SCHEMA} 0.11.0 or newer, and the reviewed set has 0.10.0",
        f"{CATALOG} 0.15.3 needs {SCHEMA} 0.12.0 or newer, and the reviewed set has 0.10.0",
        f"{MB_LOADER} 0.8.0 needs {SCHEMA} 0.14.0 or newer, and the reviewed set has 0.10.0",
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.10.0",
    ]


def test_the_catalog_api_is_refused_on_schema_0_11_because_it_lacks_master_search(policy: dict[str, Any]) -> None:
    """The real declaration: schema 0.11.0 has release_search but not the master_search or facet columns 0.12.0 adds."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.11.0"})
    assert policy["paired_floor_problems"](versions) == [
        f"{CATALOG} 0.15.3 needs {SCHEMA} 0.12.0 or newer, and the reviewed set has 0.11.0",
        f"{MB_LOADER} 0.8.0 needs {SCHEMA} 0.14.0 or newer, and the reviewed set has 0.11.0",
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.11.0",
    ]


def test_the_nightly_similar_artists_job_is_refused_without_the_latch_grant(policy: dict[str, Any]) -> None:
    """The real declaration: analytics-engine 0.9.0 and newer (0.10.0 reviewed) read loader_extraction_latch as embedding_pipeline, and 0.15.0 lacks the grant."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.15.0"})
    assert policy["paired_floor_problems"](versions) == [
        f"{ANALYTICS} 0.10.0 needs {SCHEMA} 0.15.1 or newer, and the reviewed set has 0.15.0",
    ]
    assert policy["paired_floor_problems"](dict(versions, **{ANALYTICS: "0.8.0"})) == []
    assert policy["paired_floor_problems"](dict(versions, **{SCHEMA: "0.15.1"})) == []


def test_the_catalog_api_is_accepted_on_schema_0_12_and_newer(policy: dict[str, Any]) -> None:
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.12.0"})
    assert [p for p in policy["paired_floor_problems"](versions) if p.startswith(CATALOG)] == []


def test_the_catalog_api_0_11_2_pair_is_enforced_by_check_images_itself(tmp_path: Path) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    row = '("CATALOG_API_IMAGE", "0.11.2", "DATABASE_SCHEMA_IMAGE", "0.12.0")'
    assert row in source
    broken = source.replace(row, '("CATALOG_API_IMAGE", "0.11.2", "DATABASE_SCHEMA_IMAGE", "0.16.0")')
    with pytest.raises(AssertionError, match=r"needs DATABASE_SCHEMA_IMAGE 0\.16\.0 or newer"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


def test_the_catalog_api_pair_is_enforced_by_check_images_itself(tmp_path: Path) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    row = '("CATALOG_API_IMAGE", "0.11.0", "DATABASE_SCHEMA_IMAGE", "0.11.0")'
    assert row in source
    broken = source.replace(row, '("CATALOG_API_IMAGE", "0.11.0", "DATABASE_SCHEMA_IMAGE", "0.16.0")')
    with pytest.raises(AssertionError, match=r"needs DATABASE_SCHEMA_IMAGE 0\.16\.0 or newer"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


def test_it_refuses_the_loader_against_an_older_schema(policy: dict[str, Any]) -> None:
    problems = policy["paired_floor_problems"]({LOADER: "0.5.0", SCHEMA: "0.6.3"}, pair(policy, "0.5.0", "0.7.0"))
    assert problems == [f"{LOADER} 0.5.0 needs {SCHEMA} 0.7.0 or newer, and the reviewed set has 0.6.3"]


def test_the_promoted_pair_refuses_the_loader_against_the_previous_schema(policy: dict[str, Any]) -> None:
    """The real declaration, not a fixture: loader 0.6.0 on schema 0.7.0 is refused."""
    versions = dict(policy["RELEASED_IMAGE_VERSIONS"], **{SCHEMA: "0.7.0"})
    problems = policy["paired_floor_problems"](versions)
    assert problems[0] == f"{LOADER} 0.6.0 needs {SCHEMA} 0.8.0 or newer, and the reviewed set has 0.7.0"


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
    row = '("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", "0.8.0")'
    assert row in source
    broken = source.replace(row, '("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", "0.16.0")')
    with pytest.raises(AssertionError, match=r"needs DATABASE_SCHEMA_IMAGE 0\.16\.0 or newer"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


@pytest.mark.parametrize(
    "row",
    [
        '("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", UNRELEASED)',
        '("DISCOGS_SQL_LOADER_IMAGE", UNRELEASED, "DATABASE_SCHEMA_IMAGE", "0.8.0")',
    ],
)
def test_check_images_refuses_half_a_pair(tmp_path: Path, row: str) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    broken = source.replace('("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", "0.8.0")', row)
    assert broken != source
    with pytest.raises(AssertionError, match="both UNRELEASED or both set"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)


def test_check_images_refuses_a_malformed_floor(tmp_path: Path) -> None:
    source = (ROOT / "scripts" / "check-images.py").read_text()
    broken = source.replace(
        '("DISCOGS_SQL_LOADER_IMAGE", "0.5.0", "DATABASE_SCHEMA_IMAGE", "0.8.0")',
        '("DISCOGS_SQL_LOADER_IMAGE", "next", "DATABASE_SCHEMA_IMAGE", "0.8.0")',
    )
    with pytest.raises(AssertionError, match=r"X\.Y\.Z or UNRELEASED"):
        _with({"__name__": "broken", "__file__": str(tmp_path / "scripts" / "check-images.py")}, broken)
