"""Carry a homelab's own .env values across `just env-init --force`.

`just env-init --force` writes a fresh .env from .env.example with the newly reviewed image
digests and keeps the old file as `.env.<timestamp>.bak`. The fresh file has none of the host's
choices (APP_BASE_URL, bind addresses, port remaps, Neo4j sizing, COMPOSE_PROFILES,
COMPOSE_PROJECT_NAME). This copies them from the backup, run in the checkout right after
`git pull` with the commit the backup was rendered from:

    python3 carry_env.py --since <commit before the pull> .env.20261030T120000000000Z.bak

A value is the host's choice when it differs from that commit's .env.example; a value still at
the old template's default is left to the new template, so a default that changed upstream is
not pinned back. The `*_IMAGE` digests are never carried: they are the reason for the upgrade.

A key the new file already assigns is replaced on its own line; the rest are appended under a
comment. Values are copied exactly as written, quotes included. Standard library only, so it
runs with any Python 3 on the host.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence


ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
COMMENT = "# Carried over from {backup} after `just env-init --force` (groovemap-homelab skill)"


def assignments(text: str) -> dict[str, str]:
    """Each key's last live assignment, value as written (Compose lets the last one win)."""
    values = {}
    for line in text.splitlines():
        match = None if line.lstrip().startswith("#") else ASSIGNMENT.match(line)
        if match:
            values[match["key"]] = match["value"].strip()
    return values


def carry(backup: str, current: str, old_template: str, backup_name: str) -> tuple[str, list[str]]:
    """Return the new .env text and the keys carried from the backup."""
    present = assignments(current)
    defaults = assignments(old_template)
    wanted = {
        key: value
        for key, value in assignments(backup).items()
        if not key.endswith("_IMAGE") and value != defaults.get(key) and value != present.get(key)
    }
    if not wanted:
        return current, []
    remaining = dict(wanted)
    lines = current.splitlines()
    for index, line in enumerate(lines):
        match = None if line.lstrip().startswith("#") else ASSIGNMENT.match(line)
        if match and match["key"] in remaining:
            lines[index] = f"{match['key']}={remaining.pop(match['key'])}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(COMMENT.format(backup=backup_name))
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n", list(wanted)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the host's own values (never *_IMAGE) from an .env backup into the new .env.")
    parser.add_argument("backup", type=Path, help="the .env.<timestamp>.bak that just env-init --force kept")
    parser.add_argument("env", type=Path, nargs="?", default=Path(".env"), help="the new .env (default: ./.env)")
    parser.add_argument(
        "--since", required=True, metavar="COMMIT", help="the checkout's commit before the pull, whose .env.example the backup came from"
    )
    args = parser.parse_args(argv)
    try:
        # git from PATH on purpose: the operator's own checkout, with an argument list and no shell.
        old_template = subprocess.run(  # noqa: S603
            ["git", "show", f"{args.since}:.env.example"],  # noqa: S607
            cwd=args.env.resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        text, carried = carry(args.backup.read_text(), args.env.read_text(), old_template, args.backup.name)
    except subprocess.CalledProcessError as error:
        print(f"error: could not read .env.example at {args.since}: {error.stderr.strip()}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not carried:
        print(f"{args.backup.name} holds no host values that {args.env} lacks; nothing to carry")
        return 0
    args.env.write_text(text)
    print(f"carried into {args.env} from {args.backup.name} (values not shown):")
    for key in carried:
        print(f"  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
