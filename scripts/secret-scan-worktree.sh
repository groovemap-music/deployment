#!/usr/bin/env bash
# Secret scan of the working tree's files that git would commit: tracked files plus untracked files
# that are not git-ignored. `gitleaks dir` ignores .gitignore and would also scan git-ignored local
# state (.beads/, .venv/, caches, .bh/), so a local checkout reports findings in files that never
# reach the repository. This copies the git-visible set into a scratch directory and scans that.
#
# Usage: scripts/secret-scan-worktree.sh [extra gitleaks args...]
# Exits non-zero when gitleaks finds a leak, like `gitleaks dir`.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
config="$repo_root/.gitleaks.toml"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/secret-scan-worktree.XXXXXX")"
list="$scratch/.file-list"
trap 'rm -rf "$scratch"' EXIT

# Tracked paths (including ones deleted from the worktree, skipped below) plus untracked paths that
# are not ignored. --exclude-standard applies .gitignore, .git/info/exclude and core.excludesFile.
git -C "$repo_root" ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' path; do
    if [ -e "$repo_root/$path" ] || [ -L "$repo_root/$path" ]; then
        printf '%s\0' "$path"
    fi
done > "$list"

if [ ! -s "$list" ]; then
    echo "secret-scan-worktree: no files to scan"
    exit 0
fi

mkdir "$scratch/tree"
tar -C "$repo_root" --null -T "$list" -cf - | tar -C "$scratch/tree" -xf -

gitleaks dir "$scratch/tree" --config "$config" --redact --no-banner "$@"
