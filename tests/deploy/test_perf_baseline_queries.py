"""The perf-baseline explore.artist case runs the statement catalog-api runs.

The API statement is pinned under tests/deploy/fixtures/catalog_api/explore_artist.cypher, copied from
catalog-api `api/queries/neo4j_queries.py` (`explore_artist`) at 6bf3edc. The baseline script's
case is one line, with the name as a literal, so the comparison normalises whitespace and
substitutes `$name`. When catalog-api changes the query, re-pin the file and update the case in
scripts/perf-baseline.sh in the same change; this test fails until both agree.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "perf-baseline.sh"
PIN = ROOT / "tests" / "deploy" / "fixtures" / "catalog_api" / "explore_artist.cypher"
CASE = "explore.artist.bowie"
ARTIST_NAME = "David Bowie"


def _normalise(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip().rstrip(";").strip()


def _baseline_statement() -> str:
    lines = SCRIPT.read_text().splitlines()
    index = lines.index(CASE)
    statement = lines[index + 1]
    assert statement.startswith("PROFILE "), statement
    return statement[len("PROFILE ") :]


def test_the_baseline_case_is_the_pinned_api_statement() -> None:
    pinned = PIN.read_text().replace("$name", f"'{ARTIST_NAME}'")
    assert _normalise(_baseline_statement()) == _normalise(pinned)


def test_the_pin_reads_the_stored_counts_the_api_now_returns() -> None:
    pinned = PIN.read_text()
    assert "a.release_count AS release_count" in pinned
    assert "a.label_count AS label_count" in pinned
    assert "COUNT { MATCH (r:Release)-[:BY]->(a)" not in pinned
