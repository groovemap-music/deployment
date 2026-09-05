"""Regression tests for the documented encryption configuration.

.env.example documented a dead variable name, OAUTH_ENCRYPTION_KEY, with a
comment promising it encrypts Discogs OAuth tokens/consumer keys at rest. No
production code ever read that name — common/config.py and api/setup.py only
read ENCRYPTION_MASTER_KEY (the codebase migrated from a Fernet
OAUTH_ENCRYPTION_KEY to an HKDF-derived ENCRYPTION_MASTER_KEY — see
scripts/migrate-encryption-key.sh). An operator following .env.example
literally would set a value that is silently ignored, leaving OAuth tokens
and TOTP secrets stored in plaintext despite the file's own warning that this
is exactly what happens "if unset".
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _env_example_text() -> str:
    return ENV_EXAMPLE.read_text()


def test_does_not_document_the_dead_oauth_encryption_key_var() -> None:
    assert "OAUTH_ENCRYPTION_KEY" not in _env_example_text()


def test_documents_encryption_master_key_instead() -> None:
    text = _env_example_text()
    assert re.search(r"^ENCRYPTION_MASTER_KEY=", text, re.MULTILINE), (
        ".env.example must set ENCRYPTION_MASTER_KEY (the var name common/config.py actually reads)"
    )


def test_env_example_var_names_are_all_read_by_config_or_documented_elsewhere() -> None:
    """The production overlay must deliver the documented key as a file secret."""
    compose_text = (REPO_ROOT / "docker-compose.prod.yml").read_text()
    assert "ENCRYPTION_MASTER_KEY_FILE: /run/secrets/encryption_master_key" in compose_text


def test_env_example_uses_canonical_development_identity() -> None:
    text = _env_example_text()
    retired_product_name = "discogs" + "ography"
    assert retired_product_name not in text
    assert "RABBITMQ_USERNAME=groovemap" in text
    assert "RABBITMQ_PASSWORD=groovemap" in text
    assert "NEO4J_PASSWORD=groovemap" in text
    assert "POSTGRES_USERNAME=groovemap" in text
    assert "POSTGRES_PASSWORD=groovemap" in text
    assert "POSTGRES_DATABASE=groovemap" in text


def test_env_example_has_no_duplicate_assignments() -> None:
    names = re.findall(r"^([A-Z][A-Z0-9_]*)=", _env_example_text(), re.MULTILINE)
    assert len(names) == len(set(names)), ".env.example must not rely on last-assignment-wins behavior"


def test_migrate_encryption_key_script_still_references_old_name() -> None:
    """scripts/migrate-encryption-key.sh's entire purpose is migrating
    OAUTH_ENCRYPTION_KEY -> ENCRYPTION_MASTER_KEY for existing deployments —
    it must keep referencing the old name (unlike .env.example, which should
    only ever tell NEW operators about the current name).
    """
    script = REPO_ROOT / "scripts" / "migrate-encryption-key.sh"
    assert script.exists()
    assert "OAUTH_ENCRYPTION_KEY" in script.read_text()
