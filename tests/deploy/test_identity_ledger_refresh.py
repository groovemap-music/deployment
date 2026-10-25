"""Deploy-config tests for the Identity Ledger refresh poll.

catalog-api defaults IDENTITY_LEDGER_REFRESH_ENABLED to false. With the poll off, link changes
from loads never reach the ledger until the next re-attachment run, and a new deployment answers
503 on the works list and density until then. docker-compose.yml turns it on for the api service;
the production and smoke overlays merge into that environment block rather than replace it.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.deploy.test_docker_compose_prod import _base_compose, _prod_compose


REPO_ROOT = Path(__file__).resolve().parents[2]

LEDGER_DEFAULTS = {
    "IDENTITY_LEDGER_REFRESH_ENABLED": "true",
    "IDENTITY_LEDGER_REFRESH_INTERVAL": "300",
    "IDENTITY_LEDGER_REFRESH_QUIET": "600",
    "IDENTITY_LEDGER_MAX_STALENESS": "21600",
}


def test_base_api_enables_the_ledger_refresh_poll() -> None:
    api = _base_compose()["services"]["api"]
    assert api["environment"]["IDENTITY_LEDGER_REFRESH_ENABLED"] == "true"


def test_prod_overlay_does_not_switch_the_poll_off() -> None:
    api = _prod_compose()["services"]["api"]
    assert api["environment"].get("IDENTITY_LEDGER_REFRESH_ENABLED", "true") == "true"


def test_env_example_documents_all_four_ledger_variables_with_catalog_api_defaults() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    for name, default in LEDGER_DEFAULTS.items():
        assert re.search(rf"^# {name}={default}\b", text, re.MULTILINE), name
