"""Publication-boundary tests for the deployment extraction guide."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_extraction_guide_uses_portable_private_source_inputs() -> None:
    guide = (ROOT / "docs" / "extraction.md").read_text()
    retired_product_name = "discogs" + "ography"

    assert "/Users/" not in guide
    assert "SimplicityGuy" not in guide
    assert retired_product_name not in guide.casefold()
    assert "${LEGACY_SOURCE_REPOSITORY:?" in guide
    assert "${LEGACY_SOURCE_BRANCH:?" in guide
    assert '"${LEGACY_SOURCE_REPOSITORY}" deployment' in guide
    assert '--branch "${LEGACY_SOURCE_BRANCH}"' in guide
    assert "git filter-repo --force" in guide
    assert "The filtered source branch contains 288 retained commits" in guide
