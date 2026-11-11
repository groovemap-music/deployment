#!/usr/bin/env bash
# Runs one window of the nightly exact similar-artists job (`analytics-engine-similar-artists`)
# by hand: for adopters without a scheduler, and for the operator's catch-up nights. See
# docs/maintenance.md#nightly-similar-artists-job for the multi-night cycle and for scheduling it.
#
# It runs `docker-compose.yml`'s `similar-artists` service (the "jobs" profile), which carries the
# spool volume, the --max-runtime window (SIMILAR_ARTISTS_MAX_RUNTIME, default 19800 s) and the
# embedding_pipeline login. The job's exit code is this script's exit code, unchanged: 0 is done
# or skipped, 75 is paused (the window ended, run again), 1 or 2 is failed. `exec` hands the
# process over to `docker compose run`, which exits with the container's status, so nothing here
# can turn a 75 into a 0 or a 1. The job's last stdout line is its JSON status.
#
# Which Compose files run it is decided exactly as scripts/run-embeddings.sh decides it (the job
# must run the files the stack runs with, or it loses the production secrets and the external
# PostgreSQL overlay): COMPOSE_FILE in the shell or in the env file means no -f; otherwise
# docker-compose.yml + docker-compose.prod.yml (+ docker-compose.override.yml when present);
# SIMILAR_ARTISTS_STACK=dev (only when COMPOSE_FILE is unset) is the base file alone.
set -euo pipefail

env_file="${SIMILAR_ARTISTS_ENV_FILE:-.env}"

[[ -f "$env_file" ]] || {
  echo "run-similar-artists: $env_file is missing. Copy .env.example to $env_file and set" >&2
  echo "                EMBEDDING_PIPELINE_POSTGRES_USERNAME/_PASSWORD (see .env.example)." >&2
  exit 2
}

# Which Compose files run the job: see the header.
stack="${SIMILAR_ARTISTS_STACK:-prod}"
[[ "$stack" == prod || "$stack" == dev ]] || {
  echo "run-similar-artists: SIMILAR_ARTISTS_STACK must be 'prod' or 'dev', got '$stack'." >&2
  exit 2
}

# Bash-only scan, so a missing grep can never read as "no COMPOSE_FILE".
env_file_names_compose_file() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE[[:space:]]*=[^[:space:]] ]] && return 0
  done <"$env_file"
  return 1
}

compose_files=()
if [[ -z "${COMPOSE_FILE:-}" ]] && ! env_file_names_compose_file; then
  compose_files+=(-f docker-compose.yml)
  [[ "$stack" == dev ]] || compose_files+=(-f docker-compose.prod.yml)
fi
if [[ ${#compose_files[@]} -gt 0 && -f docker-compose.override.yml ]]; then
  compose_files+=(-f docker-compose.override.yml)
fi

compose=(docker compose --env-file "$env_file" ${compose_files[@]+"${compose_files[@]}"})

# A volume Docker creates on first use is root-owned, and the job runs as ${UID}:${GID}. The stack's
# data-volume-init one-shot hands the spool volume to that user (a no-op once it already is), so
# run it first: a host that has only ever run this job, never `just deploy`, still works.
"${compose[@]}" run --rm --no-deps --name groovemap-similar-artists-volume-init data-volume-init >&2 || {
  echo "run-similar-artists: data-volume-init failed; the spool volume is not ready." >&2
  exit 2
}

exec "${compose[@]}" --profile jobs run --rm --no-deps --name groovemap-similar-artists-run similar-artists
