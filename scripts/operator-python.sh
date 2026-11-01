#!/usr/bin/env bash
# Runs a stdlib-only operator script on the same interpreter deploy.sh selects: DEPLOY_PYTHON if
# set, else `uv run --quiet python` when uv is installed, else the host's python3. Needs Python
# 3.10 or newer. Replaces itself with the script, so its exit code reaches the caller unchanged.
# Usage: bash scripts/operator-python.sh scripts/<name>.py [args...]
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: operator-python.sh <script.py> [args...]" >&2
  exit 2
fi

if [[ -n "${DEPLOY_PYTHON:-}" ]]; then
  read -r -a PYTHON <<<"$DEPLOY_PYTHON"
elif command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run --quiet python)
else
  PYTHON=(python3)
fi

if ! "${PYTHON[@]}" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  found="$("${PYTHON[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
  echo "operator-python: Python 3.10 or newer is needed (${PYTHON[*]}: ${found:-not found})." >&2
  echo "Install python3 3.10+ (Debian 12 and Ubuntu 22.04 ship it), or point DEPLOY_PYTHON at one." >&2
  exit 1
fi

exec "${PYTHON[@]}" "$@"
