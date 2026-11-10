#!/usr/bin/env bash
# Runs analytics-engine's one-shot embedding pipeline entry point
# (`analytics-engine-embeddings`) against an already-running dev/CI stack, after
# that month's dump has loaded and its consumer queues have drained — the same
# signal docs/maintenance.md's identity re-attachment sequence uses. See
# docs/maintenance.md#monthly-embedding-refresh for the full operator sequence,
# including the post-load index step this job only logs.
#
# The job has no service of its own: it reuses the `insights` service's image
# (ANALYTICS_ENGINE_IMAGE) with its entrypoint overridden and --no-deps, so this
# one-shot run never starts or waits on api/valkey. `docker-compose.yml`'s
# `embeddings` service (the "jobs" profile) passes through
# SOURCE_DUMP_ID/SOURCE_DUMP_DATE (empty when unset, so the stack renders without them; this
# script is what refuses to run without real values) and its EMBEDDING_PIPELINE_POSTGRES_USERNAME/
# _PASSWORD login — a member of database-schema's NOLOGIN embedding_pipeline
# group role, never this stack's POSTGRES_USERNAME superuser.
#
# Requires pgvector and database-schema's `embedding_pipeline` role and
# `public.artist_embeddings` table (gm-database-schema-lhp2). This stack's
# postgres service runs groovemap-music/postgres (postgres:18-alpine plus
# pgvector), so schema-init creates both; against a server without pgvector this
# script's `docker compose run` fails at connection or relation-not-found — see
# docs/maintenance.md#monthly-embedding-refresh.
set -euo pipefail

env_file="${RUN_EMBEDDINGS_ENV_FILE:-.env}"

: "${SOURCE_DUMP_ID:?Set SOURCE_DUMP_ID to the dump this run embeds, e.g. discogs-2026-09.}"
: "${SOURCE_DUMP_DATE:?Set SOURCE_DUMP_DATE to the release date of that dump (YYYY-MM-DD).}"
[[ "$SOURCE_DUMP_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || {
  echo "run-embeddings: SOURCE_DUMP_DATE must be an ISO date (YYYY-MM-DD), got '$SOURCE_DUMP_DATE'." >&2
  exit 2
}
export SOURCE_DUMP_ID SOURCE_DUMP_DATE

[[ -f "$env_file" ]] || {
  echo "run-embeddings: $env_file is missing. Copy .env.example to $env_file and set" >&2
  echo "                EMBEDDING_PIPELINE_POSTGRES_USERNAME/_PASSWORD (see .env.example)." >&2
  exit 2
}

# Which Compose files run the job. The job must run the same files as the stack it targets, or the
# embeddings service loses its production wiring (the _FILE secrets, the embedding_pipeline login
# and, on an external-store host, the external PostgreSQL overlay). So:
#   - COMPOSE_FILE set (in the shell or in $env_file): pass no -f. Compose reads it from the env
#     file itself. `just deploy` writes it there whenever an overlay is in use, so this is exactly
#     the file list deploy runs: production overlay, store overlays, shared monitoring, override.
#   - otherwise: a dedicated production stack, so docker-compose.yml + docker-compose.prod.yml, and
#     docker-compose.override.yml when the host has one. That is the file list `just deploy` uses.
#   - RUN_EMBEDDINGS_STACK=dev (only when COMPOSE_FILE is unset): the development stack, the base
#     file alone (plus the override when present), as `just deploy --dev` runs it.
# A bare `docker compose` on a production host would otherwise render the base file alone, which
# the homelab rules forbid. --profile jobs enables the one-shot's profile explicitly.
stack="${RUN_EMBEDDINGS_STACK:-prod}"
[[ "$stack" == prod || "$stack" == dev ]] || {
  echo "run-embeddings: RUN_EMBEDDINGS_STACK must be 'prod' or 'dev', got '$stack'." >&2
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

exec docker compose --env-file "$env_file" ${compose_files[@]+"${compose_files[@]}"} \
  --profile jobs run --rm --no-deps --name groovemap-embeddings-run embeddings
