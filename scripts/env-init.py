"""Generate or upgrade .env from .env.example with the reviewed release digests.

With no .env, it is written from the template with every image digest filled in. With an
existing .env, only the `*_IMAGE` lines move to the reviewed digests and template keys the file
lacks are appended; every other line, including what `just deploy` wrote (APP_BASE_URL, the
Neo4j sizing marker, COMPOSE_PROFILES) and the operator's bind addresses and port remaps, is
kept byte for byte. `--force` regenerates from the template instead.

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
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"
# An image line still carrying the placeholder; the repository before the digest is kept.
PLACEHOLDER = re.compile(
    r"^(?P<variable>[A-Z0-9_]+_IMAGE)=(?P<repository>\S+)@sha256:REPLACE_WITH_64_HEX_CHARACTERS$",
    re.MULTILINE,
)


def load_deploy_helpers() -> Any:
    """Import the .env reader from deploy_helpers so there is one parser for the file."""
    spec = importlib.util.spec_from_file_location("deploy_helpers", SCRIPTS / "deploy_helpers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("deploy_helpers", module)
    spec.loader.exec_module(module)
    return module


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


def merge_env(existing: str, rendered: str) -> tuple[str, list[str], list[str]]:
    """Move the image lines to the rendered digests and append template keys the file lacks.

    Returns the new text, the image variables whose line changed and the keys appended.
    """
    helpers = load_deploy_helpers()
    template_lines = {}
    for line in rendered.splitlines():
        match = None if line.lstrip().startswith("#") else helpers.ASSIGNMENT.match(line)
        if match:
            template_lines[match["key"]] = line
    refreshed: list[str] = []
    lines = existing.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = None if line.lstrip().startswith("#") else helpers.ASSIGNMENT.match(line)
        if match and match["key"].endswith("_IMAGE") and match["key"] in template_lines:
            ending = line[len(line.rstrip("\r\n")) :]
            wanted = template_lines[match["key"]] + ending
            if line != wanted:
                lines[index] = wanted
                if match["key"] not in refreshed:
                    refreshed.append(match["key"])
    present = helpers.parse_env(existing).keys()
    added = [key for key in template_lines if key not in present]
    text = "".join(lines)
    if added:
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += "# Added by just env-init: new in .env.example since this file was written\n"
        text += "".join(f"{template_lines[key]}\n" for key in added)
    return text, refreshed, added


def dropped_by_force(existing: str, rendered: str) -> list[str]:
    """The keys a regeneration would lose: operator values that differ from the template, and keys it lacks."""
    helpers = load_deploy_helpers()
    old, new = helpers.parse_env(existing), helpers.parse_env(rendered)
    return sorted(key for key, value in old.items() if not key.endswith("_IMAGE") and new.get(key) != value)


def backup_name(target: Path) -> Path:
    return target.with_name(f"{target.name}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.bak")


def write_env(target: Path, text: str, *, force: bool) -> Path | None:
    """Write the env file, keeping an existing one as a timestamped backup when forced."""
    backup = None
    if target.exists():
        if not force:
            raise FileExistsError(
                f"{target.name} already exists; run without --force to upgrade it in place, or pass --force to regenerate it (the old file is kept as a timestamped backup)"
            )
        backup = backup_name(target)
        target.rename(backup)
    target.write_text(text)
    return backup


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or upgrade .env with the reviewed release digests from .env.example.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate .env from the template, dropping operator values (the old file is kept as a timestamped backup)",
    )
    args = parser.parse_args(argv)
    try:
        text = render_env(TEMPLATE.read_text(), load_release_digests())
        if not TARGET.exists() or args.force:
            if TARGET.exists():
                dropped = dropped_by_force(TARGET.read_text(), text)
                if dropped:
                    print(
                        f"warning: --force regenerates {TARGET.name} from the template and drops these values: {', '.join(dropped)}", file=sys.stderr
                    )
            backup = write_env(TARGET, text, force=args.force)
            if backup is not None:
                print(f"kept the previous {TARGET.name} as {backup.name}")
            print(f"wrote {TARGET.name} from {TEMPLATE.name} with the reviewed release digests")
            return 0
        existing = TARGET.read_text()
        merged, refreshed, added = merge_env(existing, text)
    except (FileExistsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if merged == existing:
        print(f"{TARGET.name} is already up to date; nothing changed")
        return 0
    backup = backup_name(TARGET)
    backup.write_text(existing)
    TARGET.write_text(merged)
    print(f"kept the previous {TARGET.name} as {backup.name}")
    if refreshed:
        print(f"refreshed to the reviewed digests: {', '.join(refreshed)}")
    if added:
        print(f"added new keys from {TEMPLATE.name}: {', '.join(added)}")
    print(f"upgraded {TARGET.name} in place; every other line is unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
