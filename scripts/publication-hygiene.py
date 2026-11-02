#!/usr/bin/env python3
"""Reject committed Python bytecode and absolute user-home paths before a repository is published."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

ALLOWLIST_NAME = ".publication-hygiene-allowlist"
ALL_ZEROS = re.compile(r"0+")
BYTECODE_SUFFIXES = (".pyc", ".pyo")
GITLINK_MODE = b"160000"
BINARY_PROBE = 8192

# The home prefixes are assembled from parts so this file never contains the strings it hunts for;
# repository exposure rules scan every tracked file, this one included.
_MAC_HOME = b"/" + b"Us" + b"ers"
_LINUX_HOME = b"/" + b"ho" + b"me"
# Generic or placeholder account names that identify no one. The list is closed: an exempt name must be
# the whole path segment and match case-sensitively, so a longer or differently cased name still fails.
GENERIC_ACCOUNTS = ("runner", "Shared", "user", "username", "you", "yourname", "name", "me", "example")
_GENERIC = rb"(?!(?-i:(?:" + b"|".join(name.encode() for name in GENERIC_ACCOUNTS) + rb")[\\/]))"
_NAME = _GENERIC + rb"(?!\.+[\\/])[^\x00-\x20\\/<>:\"'|?*$%{}()\[\]~`]+"
_WINDOWS_NAME = _GENERIC + rb"(?!\.+[\\/])[^\x00-\x1f\\/<>:\"'|?*$%{}()\[\]~`]+"
_SEPARATOR = rb"[\\/]{1,4}"
# Text files only match at a path start, so a relative path such as docs/<dir>/<page>/ is not a home
# path. Binary files embed paths after a length byte, so they match anywhere.
_TEXT_LEAD = rb"(?<![A-Za-z0-9_.~\-])"
PATTERNS = (
    ("home-path-macos", re.escape(_MAC_HOME) + rb"/" + _NAME + rb"/", 0),
    ("home-path-linux", re.escape(_LINUX_HOME) + rb"/" + _NAME + rb"/", 0),
    ("home-path-windows", rb"[A-Za-z]:" + _SEPARATOR + b"Us" + b"ers" + _SEPARATOR + _WINDOWS_NAME + _SEPARATOR, re.IGNORECASE),
)
TEXT_PATTERNS = tuple((label, re.compile(_TEXT_LEAD + source, flags)) for label, source, flags in PATTERNS)
BINARY_PATTERNS = tuple((label, re.compile(source, flags)) for label, source, flags in PATTERNS)


class HygieneError(Exception):
    """An operational failure, as opposed to a finding."""


def escape_data(value: str) -> str:
    """Escape text as GitHub's workflow-command parser expects, so it cannot end the line."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def printable(value: str) -> str:
    """Make a repository path safe to print: control and undecodable characters become escapes."""
    out = []
    for character in value:
        if character.isprintable():
            out.append(character)
        elif ord(character) <= 0xFF:
            out.append(f"\\x{ord(character):02x}")
        else:
            out.append(f"\\u{ord(character):04x}")
    return "".join(out)


def error_line(message: str) -> str:
    """Format a stderr error; the runner scans stderr for commands too, so it is escaped the same way."""
    return f"ERROR {escape_data(message)}"


def run_git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)
    if check and result.returncode != 0:
        raise HygieneError(f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result


def resolve_commit(repo: str, revision: str) -> str | None:
    result = run_git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def bytecode_path(path: str) -> bool:
    parts = path.split("/")
    return "__pycache__" in parts or parts[-1].lower().endswith(BYTECODE_SUFFIXES)


def classify_content(data: bytes) -> list[str]:
    patterns = BINARY_PATTERNS if b"\0" in data[:BINARY_PROBE] else TEXT_PATTERNS
    return [label for label, pattern in patterns if pattern.search(data)]


def load_allowlist(path: str, required: bool) -> set[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        if required:
            raise HygieneError(f"allowlist file not found: {printable(path)}") from None
        return set()
    except (OSError, UnicodeDecodeError) as error:
        raise HygieneError(f"allowlist file is unreadable: {printable(path)}: {error.__class__.__name__}") from None
    entries: set[str] = set()
    for number, line in enumerate(lines, start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        pure = PurePosixPath(entry)
        if pure.is_absolute() or ".." in pure.parts or any(character in entry for character in "*?[]"):
            raise HygieneError(f"allowlist line {number} must be one exact repository-relative path")
        entries.add(pure.as_posix())
    return entries


class BlobReader:
    """Read blobs through one long-lived `git cat-file --batch` process."""

    def __init__(self, repo: str) -> None:
        self._process = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    def read(self, object_id: str) -> bytes:
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(object_id.encode() + b"\n")
        self._process.stdin.flush()
        header = self._process.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise HygieneError("git cat-file could not read a blob")
        size = int(header[2])
        data = self._process.stdout.read(size)
        self._process.stdout.read(1)
        return data

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait()


def select_commits(repo: str, history: bool, base: str | None, head: str) -> tuple[list[tuple[str, list[str]]], str]:
    """Return (commit, parents) pairs to scan and a short description of the scope.

    Only --history scans every commit. A range without a usable base narrows to the head commit's own
    changes: published history that a new commit cannot change must not fail every later run.
    """
    head_sha = resolve_commit(repo, head)
    if head_sha is None:
        raise HygieneError("head revision is not a commit in this repository")
    revisions = [head_sha]
    description = "all commits reachable from head"
    narrowed = False
    if not history and base is not None:
        no_base = not base.strip() or ALL_ZEROS.fullmatch(base.strip()) is not None
        base_sha = None if no_base else resolve_commit(repo, base.strip())
        if base_sha is not None:
            revisions.append(f"^{base_sha}")
            description = "commits in the base..head range"
        else:
            reason = "no base revision, e.g. a new branch" if no_base else "base revision is not available locally"
            revisions = ["--max-count=1", head_sha]
            narrowed = True
            description = f"head commit's own changes only ({reason}); earlier history is not scanned"
    if not narrowed and run_git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
        raise HygieneError("the repository is a shallow clone; fetch complete history before scanning commits")
    output = run_git(repo, "rev-list", "--parents", *revisions).stdout.decode()
    commits = []
    for line in output.splitlines():
        commit, *parents = line.split()
        commits.append((commit, parents))
    return commits, description


def commit_blobs(repo: str, commit: str, parents: list[str]) -> list[tuple[str, str]]:
    """Return (path, blob) for every blob this commit adds or changes relative to its first parent."""
    diff = ["diff-tree", "-r", "-z", "--no-commit-id", "--no-renames", "--diff-filter=ACMRT"]
    diff += [parents[0], commit] if parents else ["--root", commit]
    tokens = run_git(repo, *diff).stdout.split(b"\0")
    blobs = []
    index = 0
    while index + 1 < len(tokens) and tokens[index].startswith(b":"):
        fields = tokens[index][1:].split(b" ")
        path = os.fsdecode(tokens[index + 1])
        index += 2
        if fields[1] != GITLINK_MODE:
            blobs.append((path, fields[3].decode()))
    return blobs


def scan(repo: str, allowlist: set[str], commits: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, str, str]], int, int]:
    """Scan the worktree, then each commit's introduced blobs; return findings and the files and blobs scanned."""
    findings: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def record(label: str, where: str, path: str) -> None:
        if (label, where, path) not in seen:
            seen.add((label, where, path))
            findings.append((label, where, path))

    tracked = [os.fsdecode(item) for item in run_git(repo, "ls-files", "-z").stdout.split(b"\0") if item]
    scanned_files = 0
    for path in tracked:
        if path in allowlist:
            continue
        if bytecode_path(path):
            record("bytecode", "worktree", path)
        data = read_worktree_file(repo, path)
        if data is None:
            continue
        scanned_files += 1
        for label in classify_content(data):
            record(label, "worktree", path)

    cache: dict[str, list[str]] = {}
    reader = BlobReader(repo)
    try:
        for commit, parents in commits:
            for path, blob in commit_blobs(repo, commit, parents):
                if path in allowlist:
                    continue
                if bytecode_path(path):
                    record("bytecode", commit[:12], path)
                if blob not in cache:
                    cache[blob] = classify_content(reader.read(blob))
                for label in cache[blob]:
                    record(label, commit[:12], path)
    finally:
        reader.close()
    return findings, scanned_files, len(cache)


def read_worktree_file(repo: str, path: str) -> bytes | None:
    full = os.path.join(repo, path)
    try:
        if os.path.islink(full):
            return os.fsencode(os.readlink(full))
        if os.path.isfile(full):
            with open(full, "rb") as handle:
                return handle.read()
    except OSError:
        return None
    return None


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publication-hygiene",
        description="Reject tracked Python bytecode and absolute user-home paths in the worktree and in commits.",
    )
    parser.add_argument("--repo", default=".", help="repository to scan (default: current directory)")
    parser.add_argument("--history", action="store_true", help="scan every commit reachable from the head revision (explicit only; no CI event implies it)")
    parser.add_argument(
        "--base",
        help="scan commits in BASE..HEAD; an empty or all-zero BASE, or one that is not available locally, scans the head commit's own changes only",
    )
    parser.add_argument("--head", default="HEAD", help="head revision of the range (default: HEAD)")
    parser.add_argument("--allowlist", help=f"reviewed exact-path allowlist (default: {ALLOWLIST_NAME} in the repository, if present)")
    arguments = parser.parse_args(argv)
    if arguments.history and arguments.base is not None:
        parser.error("--history and --base are mutually exclusive")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        explicit = arguments.allowlist is not None
        allowlist_path = arguments.allowlist if explicit else os.path.join(arguments.repo, ALLOWLIST_NAME)
        allowlist = load_allowlist(allowlist_path, required=explicit)
        commits: list[tuple[str, list[str]]] = []
        description = "tracked tree at HEAD only; no commit range"
        if arguments.history or arguments.base is not None:
            commits, description = select_commits(arguments.repo, arguments.history, arguments.base, arguments.head)
        findings, files, blobs = scan(arguments.repo, allowlist, commits)
    except HygieneError as error:
        print(error_line(f"publication hygiene could not run: {error}"), file=sys.stderr)
        return 2

    if findings:
        counts: dict[str, int] = {}
        for label, where, path in findings:
            counts[label] = counts.get(label, 0) + 1
            print(error_line(f"{label} in {where}: {printable(path)}"), file=sys.stderr)
        summary = ", ".join(f"{label}: {count}" for label, count in sorted(counts.items()))
        print(error_line(f"publication hygiene failed with {len(findings)} finding(s) ({summary})"), file=sys.stderr)
        print(
            "Matched text is never printed. Remove the file and rewrite unpublished history, or add a reviewed exact path to the allowlist.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Publication hygiene clean: {files} tracked file(s) in the worktree and {len(commits)} commit(s) "
        f"({blobs} distinct blob(s)) scanned; scope: {description}; allowlist entries: {len(allowlist)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
