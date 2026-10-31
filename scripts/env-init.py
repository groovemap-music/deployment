"""Generate .env from .env.example, filling every image digest from the reviewed release set.

The digests are imported from RELEASED_IMAGE_DIGESTS in scripts/check-images.py rather than
copied, so the table that `just smoke-released` validates against stays the only record of
a reviewed digest.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"
# An image line still carrying the placeholder; the repository before the digest is kept.
PLACEHOLDER = re.compile(
    r"^(?P<variable>[A-Z0-9_]+_IMAGE)=(?P<repository>\S+)@sha256:REPLACE_WITH_64_HEX_CHARACTERS$",
    re.MULTILINE,
)


def load_release_digests() -> dict[str, str]:
    """Import RELEASED_IMAGE_DIGESTS from the image policy without duplicating the table."""
    spec = importlib.util.spec_from_file_location("check_images", ROOT / "scripts" / "check-images.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digests: dict[str, str] = module.RELEASED_IMAGE_DIGESTS
    return digests


def render_env(template: str, digests: Mapping[str, str]) -> str:
    """Fill each image placeholder with its reviewed digest and keep every other line as written."""
    unreviewed = sorted({match["variable"] for match in PLACEHOLDER.finditer(template)} - digests.keys())
    if unreviewed:
        raise ValueError(f"no reviewed release digest in scripts/check-images.py for: {', '.join(unreviewed)}")
    return PLACEHOLDER.sub(lambda match: f"{match['variable']}={match['repository']}@sha256:{digests[match['variable']]}", template)


def write_env(target: Path, text: str, *, force: bool) -> Path | None:
    """Write the env file, keeping an existing one as a timestamped backup when forced."""
    backup = None
    if target.exists():
        if not force:
            raise FileExistsError(f"{target.name} already exists; pass --force to replace it (the old file is kept as a timestamped backup)")
        backup = target.with_name(f"{target.name}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.bak")
        target.rename(backup)
    target.write_text(text)
    return backup


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate .env from .env.example with the reviewed release digests.")
    parser.add_argument("--force", action="store_true", help="replace an existing .env, keeping it as a timestamped backup")
    args = parser.parse_args(argv)
    try:
        text = render_env(TEMPLATE.read_text(), load_release_digests())
        backup = write_env(TARGET, text, force=args.force)
    except (FileExistsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if backup is not None:
        print(f"kept the previous {TARGET.name} as {backup.name}")
    print(f"wrote {TARGET.name} from {TEMPLATE.name} with the reviewed release digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
