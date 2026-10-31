#!/usr/bin/env python3
"""What to capture so this deployment can be rebuilt: `just recovery-manifest`.

Prints a checklist of the files and pins a recovery needs (.env, docker-compose.override.yml,
the secrets/ files, the deployment commit, the promoted image digests) and the external stores
that need their own backups. Read-only, standard library only (python3 >= 3.10).

It never prints a secret value. Secret files are listed by name, mode, owner and a short SHA-256
fingerprint, which tells the operator whether the copy they restore is the one they saved. .env
and the override are named but not read into the output, apart from the image digests and the
two store-mode switches, which are not secret.

`snapshot FILE` and `verify FILE` are deploy's tripwire (scripts/deploy.sh): `snapshot` records
the fingerprint of every existing secret file, `verify` exits 3 if any of them changed. Only
the file derived from another secret (DERIVED_SECRETS) may be rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_FILE = "docker-compose.override.yml"
FINGERPRINT_CHARS = 16
# Named tuples: the formatter would rewrite an inline `except (A, B)` to the 3.14-only form, a SyntaxError on 3.10.
GIT_ERRORS = (OSError, subprocess.SubprocessError)
GIT = shutil.which("git") or "git"
IMAGE_PIN = re.compile(r"^([A-Z][A-Z0-9_]*_IMAGE)\s*=\s*['\"]?([^\s'\"#]+@sha256:[0-9a-f]{64})['\"]?\s*(?:#.*)?$")
STORE_MODE = re.compile(r"^(GROOVEMAP_POSTGRES|GROOVEMAP_VALKEY)\s*=\s*['\"]?(external|local)['\"]?\s*(?:#.*)?$")

# Rewritten from another secret on every create-secrets.sh run, so a changed copy is not a regeneration.
DERIVED_SECRETS = frozenset({"valkey_exporter_passwords.json"})
# Supplied by the operator, not generated: a lost copy has to come from the provider, not a restore.
OPERATOR_SUPPLIED = frozenset({"resend_api_key.txt", "nlq_api_key.txt", "grafana_viewer_token.txt"})
# Generated once by create-secrets.sh. Losing these makes data or sessions unreadable.
IRREPLACEABLE = {
    "jwt_secret_key.txt": "signs sessions; a new key signs every user out",
    "encryption_master_key.txt": "derives the OAuth and TOTP keys; stored OAuth tokens and TOTP secrets are unreadable without it",
}

# Dedicated stores' volumes: (volume, what it holds, reconstructible from the Discogs/MusicBrainz dumps?)
VOLUMES = (
    ("postgres_data", "PostgreSQL (GrooveMap database): accounts, collections, identity ledger, admin audit log", "partly"),
    ("neo4j_data", "Neo4j graph", "yes, by re-import; user-linked data is not"),
    ("rabbitmq_data", "RabbitMQ queues and policies", "yes, in-flight work is re-queued by re-import"),
    ("valkey_data", "Valkey cache", "yes"),
    ("musicbrainz_data", "MusicBrainz dump files", "yes, by re-import"),
)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def secret_files(root: Path) -> list[Path]:
    """The regular files directly in secrets/, symlinks excluded: following one could leave the directory."""
    directory = root / "secrets"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file() and not path.is_symlink())


def snapshot(root: Path, target: Path) -> int:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        for path in secret_files(root):
            handle.write(f"{path.name} {fingerprint(path)}\n")
    return 0


def verify(root: Path, source: Path) -> int:
    """Exit 3 if a secret that existed before is gone or different (a derived one excepted)."""
    changed = []
    for line in source.read_text().splitlines():
        name, _, expected = line.partition(" ")
        if name in DERIVED_SECRETS:
            continue
        path = root / "secrets" / name
        if not path.is_file() or fingerprint(path) != expected:
            changed.append(name)
    if changed:
        print("error: an existing secret was replaced or removed; stopping before anything starts:", file=sys.stderr)
        for name in changed:
            print(f"  secrets/{name}", file=sys.stderr)
        print("Restore it from the encrypted recovery bundle (docs/maintenance.md#recovery-bundle); never regenerate it.", file=sys.stderr)
        return 3
    return 0


def owner(info: os.stat_result) -> str:
    try:
        return pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        return str(info.st_uid)


def git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run([GIT, *args], cwd=root, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603 -- fixed git argv, no shell
    except GIT_ERRORS:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def env_lines(root: Path) -> list[str]:
    try:
        return (root / ".env").read_text().splitlines()
    except OSError:
        return []


def describe_file(path: Path, label: str) -> str:
    try:
        info = path.lstat()
    except OSError:
        return f"[ ] {label}: not present"
    return f"[ ] {label}: mode {stat.S_IMODE(info.st_mode):04o}, owner {owner(info)}, sha256 {fingerprint(path)[:FINGERPRINT_CHARS]}"


def secret_line(path: Path) -> str:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    size = info.st_size
    prints = "empty" if size == 0 else f"sha256 {fingerprint(path)[:FINGERPRINT_CHARS]}"
    if path.name in IRREPLACEABLE:
        note = f"  IRREPLACEABLE: {IRREPLACEABLE[path.name]}"
    elif path.name in DERIVED_SECRETS:
        note = "  derived from valkey_password.txt; create-secrets.sh rebuilds it"
    elif path.name in OPERATOR_SUPPLIED:
        note = "  operator-supplied: also keep it where you got it"
    else:
        note = ""
    flag = "" if mode & 0o077 == 0 else "  WARNING: group/world access, expected 0600"
    return f"[ ] secrets/{path.name}: mode {mode:04o}, owner {owner(info)}, {prints}{note}{flag}"


def manifest(root: Path) -> list[str]:
    lines = [
        "GrooveMap recovery manifest",
        "===========================",
        "Capture everything below into ONE bundle and encrypt it (SOPS with age, as the operator already does)",
        "before it leaves this host. Secret values are never printed here, only names and fingerprints.",
        "Do not store the bundle, or any secret, in this repository.",
        "",
        "1. Files to capture (encrypted)",
        describe_file(root / ".env", ".env"),
        describe_file(root / OVERRIDE_FILE, OVERRIDE_FILE),
    ]
    secrets = secret_files(root)
    if secrets:
        lines.append(f"[ ] secrets/ ({len(secrets)} files; the directory should be mode 0700)")
        lines += [f"    {secret_line(path)}" for path in secrets]
    else:
        lines.append("[ ] secrets/: no files found (run `just secrets-bootstrap` on a new host, never on a host being recovered)")

    lines += ["", "2. Deployment pin"]
    commit = git(root, "rev-parse", "HEAD")
    if commit is None:
        lines.append("[ ] deployment commit: unknown (not a git checkout, or git is missing); record the release you installed")
    else:
        dirty = git(root, "status", "--porcelain")
        state = (
            "unknown"
            if dirty is None
            else ("clean" if not dirty else f"DIRTY, {len(dirty.splitlines())} changed paths; tracked files should not be edited")
        )
        lines.append(f"[ ] deployment commit: {commit} ({state})")
    pins = [match.groups() for line in env_lines(root) if (match := IMAGE_PIN.match(line.strip()))]
    if pins:
        lines.append("[ ] promoted image digests in .env (recorded again so a restore pulls exactly these):")
        lines += [f"    {key}={value}" for key, value in sorted(pins)]
    else:
        lines.append("[ ] promoted image digests: none found in .env (run `just env-init`)")

    modes = {match[1]: match[2] for line in env_lines(root) if (match := STORE_MODE.match(line.strip()))}
    external = sorted(name for name, mode in modes.items() if mode == "external")
    lines += ["", "3. Stores that need their own backups"]
    for name, label in (("GROOVEMAP_POSTGRES", "PostgreSQL"), ("GROOVEMAP_VALKEY", "Valkey")):
        if name in external:
            lines.append(
                f"[ ] {label} is EXTERNAL: its operator backs it up. Confirm the backup covers GrooveMap's database, which schemas it skips, and that restore verification includes it (docs/maintenance.md#backups-and-restore-drills)."
            )
    if "GROOVEMAP_POSTGRES" not in external:
        lines.append("[ ] PostgreSQL is dedicated: back up the postgres_data volume (pg_dump or a base backup).")
    for volume, holds, rebuild in VOLUMES:
        if volume == "postgres_data" and "GROOVEMAP_POSTGRES" in external:
            continue
        if volume == "valkey_data" and "GROOVEMAP_VALKEY" in external:
            continue
        lines.append(f"[ ] volume {volume}: {holds}. Reconstructible: {rebuild}.")

    lines += [
        "",
        "4. Recovery order",
        "    1. Restore secrets/ (mode 0700, files 0600), .env and the override from the decrypted bundle.",
        "    2. Check out the pinned commit. Do NOT run `just secrets-bootstrap` first: it would invent new values.",
        "    3. Restore the data stores, then start containers.",
        "    Never reset an existing secret, print a value, or purge keys or volumes after a failed staging.",
    ]
    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="recovery_manifest", description="List what to capture for recovery; prints no secret values.")
    parser.add_argument("--root", type=Path, default=ROOT, help="the deployment checkout (default: this one)")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("snapshot", help="record the fingerprint of every secret file").add_argument("file", type=Path)
    commands.add_parser("verify", help="exit 3 if a recorded secret changed").add_argument("file", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    if args.command == "snapshot":
        return snapshot(root, args.file)
    if args.command == "verify":
        return verify(root, args.file)
    print("\n".join(manifest(root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
