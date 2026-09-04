"""Regression test for gm-deployment-8wi.1 — extraction-rules ownership.

ADR 0005 assigned the Discogs extraction rules to `discogs-ingestion`, retiring
the combined `catalog-ingestion` repository as their owner. `config/provenance.json`
must record the current owner so promotion tooling and documentation stay aligned
with who actually maintains the editable rules today.
"""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extraction_rules_owner_is_discogs_ingestion() -> None:
    provenance = json.loads((REPO_ROOT / "config" / "provenance.json").read_text())
    assert provenance["extraction-rules.yaml"]["owner"] == "groovemap-music/discogs-ingestion"


def test_extraction_rules_owner_is_not_the_retired_catalog_ingestion_repository() -> None:
    provenance = json.loads((REPO_ROOT / "config" / "provenance.json").read_text())
    assert provenance["extraction-rules.yaml"]["owner"] != "groovemap-music/catalog-ingestion"
