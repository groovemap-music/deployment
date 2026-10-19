"""Copy GrooveMap's security-critical keys from the old Redis to Valkey.

Valkey cannot read Redis 7.4+/8 RDB or AOF files, so the ``redis_data`` volume is
not mounted into the Valkey service. Everything that must survive the cutover is
copied key by key instead: revoked token ids (so a revoked token stays revoked),
password-change markers, 2FA challenges, reset tokens, Discogs OAuth state, and
sync cooldowns. Cache keys are deliberately not copied; they rebuild on demand.

Each key is copied with ``GET`` plus ``PTTL`` -> ``SET PX`` so the remaining
lifetime is preserved to the millisecond. A key without a TTL is copied without
expiry; a key that expired between ``SCAN`` and ``GET`` is skipped. ``--dry-run``
reads the source and prints per-prefix counts without writing anything. The
exit status is non-zero if any key fails to copy.

Both ends use valkey-py, which speaks to Redis 8 as well.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import valkey


if TYPE_CHECKING:
    from collections.abc import Sequence

# The exact security-state prefixes from the Valkey migration design (epic
# gm-deployment-u4e). Anything outside this list is cache and is never copied.
PREFIXES: tuple[str, ...] = (
    "revoked:jti:",
    "password_changed:",
    "2fa_challenge:",
    "reset:",
    "discogs:oauth:state:",
    "sync:cooldown:",
)

SCAN_COUNT = 500


@dataclass
class PrefixResult:
    """Per-prefix outcome of one run."""

    scanned: int = 0
    copied: int = 0
    persistent: int = 0
    skipped_expired: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def read_password(path: str | None) -> str | None:
    """Read a password from a secret file, stripping the trailing newline."""
    if path is None:
        return None
    password = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError(f"password file {path} is empty")
    return password


def connect(url: str, password_file: str | None) -> valkey.Valkey:
    """Open a bytes-safe client; a password file overrides any password in the URL."""
    password = read_password(password_file)
    kwargs: dict[str, object] = {"decode_responses": False}
    if password is not None:
        kwargs["password"] = password
    return valkey.Valkey.from_url(url, **kwargs)


def _glob_escape(text: str) -> str:
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


def _copy_key(source: valkey.Valkey, target: valkey.Valkey, key: bytes, result: PrefixResult, *, dry_run: bool) -> None:
    value = cast("bytes | None", source.get(key))
    if value is None:
        result.skipped_expired += 1
        return
    pttl = cast("int", source.pttl(key))
    if pttl == -2 or pttl == 0:
        # Gone (or about to be) between GET and PTTL.
        result.skipped_expired += 1
        return
    if not dry_run:
        if pttl == -1:
            target.set(key, value)
        else:
            target.set(key, value, px=pttl)
    if pttl == -1:
        result.persistent += 1
    result.copied += 1


def copy_prefixes(
    source: valkey.Valkey,
    target: valkey.Valkey,
    prefixes: Sequence[str] = PREFIXES,
    *,
    dry_run: bool = False,
) -> dict[str, PrefixResult]:
    """Copy every key under each prefix; failures are recorded, never raised."""
    results: dict[str, PrefixResult] = {}
    for prefix in prefixes:
        result = results[prefix] = PrefixResult()
        pattern = _glob_escape(prefix) + "*"
        seen: set[bytes] = set()
        for raw in source.scan_iter(match=pattern, count=SCAN_COUNT):
            key = bytes(raw)
            # SCAN may return a key more than once; defend against any server-side
            # pattern looseness too, so only the exact prefix is ever copied.
            if key in seen or not key.startswith(prefix.encode()):
                continue
            seen.add(key)
            result.scanned += 1
            try:
                _copy_key(source, target, key, result, dry_run=dry_run)
            except (valkey.ValkeyError, OSError) as exc:
                result.failed += 1
                result.errors.append(f"{key.decode(errors='replace')}: {exc}")
    return results


def format_report(results: dict[str, PrefixResult], *, dry_run: bool) -> str:
    verb = "would copy" if dry_run else "copied"
    lines = [f"{'DRY RUN - nothing written. ' if dry_run else ''}Per-prefix counts:"]
    for prefix, r in results.items():
        lines.append(
            f"  {prefix:<24} scanned={r.scanned} {verb}={r.copied} no-ttl={r.persistent} expired-skipped={r.skipped_expired} failed={r.failed}"
        )
    total_copied = sum(r.copied for r in results.values())
    total_failed = sum(r.failed for r in results.values())
    lines.append(f"Total: {verb} {total_copied}, failed {total_failed}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-url", required=True, help="old Redis URL, e.g. redis://localhost:6379/0")
    parser.add_argument("--target-url", required=True, help="new Valkey URL, e.g. redis://localhost:6379/0 (Valkey speaks the same scheme)")
    parser.add_argument("--source-password-file", help="file holding the Redis password")
    parser.add_argument("--target-password-file", help="file holding the Valkey password")
    parser.add_argument("--dry-run", action="store_true", help="read the source and print counts; write nothing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.source_url == args.target_url:
        print("error: source and target URLs are identical", file=sys.stderr)
        return 2
    try:
        source = connect(args.source_url, args.source_password_file)
        target = connect(args.target_url, args.target_password_file)
        source.ping()
        if not args.dry_run:
            target.ping()
    except (valkey.ValkeyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    results = copy_prefixes(source, target, dry_run=args.dry_run)
    print(format_report(results, dry_run=args.dry_run))
    failed = [e for r in results.values() for e in r.errors]
    for err in failed:
        print(f"FAILED {err}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
