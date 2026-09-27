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
# one-shot run never starts or waits on api/redis. `docker-compose.yml`'s
# `embeddings` service (the "jobs" profile) declares its required
# SOURCE_DUMP_ID/SOURCE_DUMP_DATE and its EMBEDDING_PIPELINE_POSTGRES_USERNAME/
# _PASSWORD login — a member of database-schema's NOLOGIN embedding_pipeline
# group role, never this stack's POSTGRES_USERNAME superuser.
#
# Requires PostgreSQL 19 with pgvector and database-schema's `embedding_pipeline`
# role and `public.artist_embeddings` table (gm-database-schema-lhp2), neither of
# which this stack's postgres:18-alpine service has yet — see
# docs/maintenance.md#monthly-embedding-refresh for the pgvector dependency
# (gm-deployment-kh5, gated on the PostgreSQL 19 GA pin gm-deployment-2sb.2) this
# blocks on. Until that lands, this script's `docker compose run` is expected to
# fail at connection or relation-not-found.
set -euo pipefail

env_file="${RUN_EMBEDDINGS_ENV_FILE:-.env}"

: "${SOURCE_DUMP_ID:?Set SOURCE_DUMP_ID to the dump this run embeds, e.g. discogs-2026-09.}"
: "${SOURCE_DUMP_DATE:?Set SOURCE_DUMP_DATE to that dump's release date (YYYY-MM-DD).}"
export SOURCE_DUMP_ID SOURCE_DUMP_DATE

[[ -f "$env_file" ]] || {
  echo "run-embeddings: $env_file is missing. Copy .env.example to $env_file and set" >&2
  echo "                EMBEDDING_PIPELINE_POSTGRES_USERNAME/_PASSWORD (see .env.example)." >&2
  exit 2
}

docker compose --env-file "$env_file" -f docker-compose.yml run --rm --no-deps embeddings
