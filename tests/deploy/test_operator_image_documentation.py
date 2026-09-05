"""The operator docs must describe the environment files this repository actually ships.

`.env.example` carries placeholders and `config/validation.env` syntax-only digests;
neither is a deployment input. The reviewed release digests live in
`RELEASED_IMAGE_DIGESTS` in `scripts/check-images.py`, which is what
`just smoke-released` validates a candidate `.env` against.

A doc that instead prints real digests as though they were already in `.env.example`
tells the operator to skip the promotion step, and goes stale the moment a producer
publishes again. That happened once: the released-stack container was written against a
tree where both env files carried real digests, and landing it on the placeholder tree
left quick-start advertising a database-schema digest two releases old and testing-guide
missing the warning that validation-only digests must never reach a real `.env`.
"""

from __future__ import annotations

import re
import runpy
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DIGEST_LITERAL = re.compile(r"\b[0-9a-f]{64}\b")


def _normalize(text: str) -> str:
    """Collapse markdown line wrapping so a wrapped sentence still matches."""
    return " ".join(text.split())


def _check_images() -> dict[str, Any]:
    return runpy.run_path(str(REPO_ROOT / "scripts" / "check-images.py"), run_name="check_images")


def test_env_files_carry_placeholders_rather_than_reviewed_digests() -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    validation = (REPO_ROOT / "config/validation.env").read_text()

    assert "REPLACE_WITH_64_HEX_CHARACTERS" in example
    assert not DIGEST_LITERAL.search(example), ".env.example must stay on placeholders, not a promoted digest"

    for digest in DIGEST_LITERAL.findall(validation):
        assert digest == "1" * 64, f"config/validation.env must stay syntax-only, found {digest}"

    # The reviewed set is real, and deliberately lives somewhere neither operator file does.
    released = _check_images()["RELEASED_IMAGE_DIGESTS"]
    assert released and all(digest != "1" * 64 for digest in released.values())


def test_quick_start_tells_the_operator_to_replace_the_placeholders() -> None:
    guide = (REPO_ROOT / "docs/quick-start.md").read_text()

    assert "REPLACE_WITH_64_HEX_CHARACTERS" in guide, "quick-start must name the placeholder it asks operators to replace"
    # A pasted digest here reads as "already done" and rots at the next release.
    assert not DIGEST_LITERAL.search(guide), "quick-start must not advertise a promoted digest"
    assert "RELEASED_IMAGE_DIGESTS" in guide and "just smoke-released" in guide, (
        "quick-start must point at the reviewed release set and the check that enforces it"
    )


def test_testing_guide_keeps_the_never_copy_warning() -> None:
    guide = _normalize((REPO_ROOT / "docs/testing-guide.md").read_text())

    assert "## Validation-only image references" in (REPO_ROOT / "docs/testing-guide.md").read_text()
    assert "must never be copied into an environment `.env` file" in guide
    assert "non-published dummy digests" in guide
    assert "RELEASED_IMAGE_DIGESTS" in guide and "just smoke-released" in guide, "the guide must say where the reviewed digests do live"
