#!/usr/bin/env bash
# Creates the LOGIN role that authenticates analytics-engine's embedding pipeline
# and grants it membership in database-schema's NOLOGIN `embedding_pipeline`
# group role (ADR 0013's 2026-09-24 amendment: "The login that is a member of
# that role is provisioned where credentials live: `deployment` for development
# and CI"). Idempotent — safe to re-run after every dump load, before
# scripts/run-embeddings.sh.
#
# `embedding_pipeline` itself is created by schema-init's guarded initializer,
# only once the `vector` extension is installed. This stack's postgres service
# (postgres:18-alpine) has no pgvector build yet — see
# docs/maintenance.md#monthly-embedding-refresh for the PostgreSQL 19 + pgvector
# dependency (gm-deployment-kh5, gated on the PostgreSQL 19 GA pin
# gm-deployment-2sb.2) this blocks on. Until that lands, this script finds no
# `embedding_pipeline` role, logs why, and exits 0 without creating a login no
# group role yet exists to constrain — it must never fail `just check`/CI over a
# dependency this repository does not own.
set -euo pipefail

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-groovemap-postgres}"
POSTGRES_USER="${POSTGRES_USER:-groovemap}"
POSTGRES_DB="${POSTGRES_DB:-groovemap}"
username="${EMBEDDING_PIPELINE_POSTGRES_USERNAME:-embedding_pipeline}"
password="${EMBEDDING_PIPELINE_POSTGRES_PASSWORD:?Set EMBEDDING_PIPELINE_POSTGRES_PASSWORD (see .env.example).}"

docker inspect --format '{{.State.Running}}' "$POSTGRES_CONTAINER" | grep -qx true || {
  echo "provision-embedding-pipeline-login: $POSTGRES_CONTAINER is not running." >&2
  exit 2
}

role_exists() {
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c \
    "SELECT 1 FROM pg_roles WHERE rolname = '$1'"
}

if [[ "$(role_exists embedding_pipeline)" != "1" ]]; then
  echo "provision-embedding-pipeline-login: no embedding_pipeline role yet — needs PostgreSQL 19" >&2
  echo "  + pgvector and database-schema's guarded initializer (see" >&2
  echo "  docs/maintenance.md#monthly-embedding-refresh). Nothing to do." >&2
  exit 0
fi

if [[ "$(role_exists "$username")" == "1" ]]; then
  echo "provision-embedding-pipeline-login: $username already exists; leaving its password alone."
else
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c \
    "CREATE ROLE \"$username\" LOGIN PASSWORD '$password'"
  echo "provision-embedding-pipeline-login: created $username"
fi

docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c \
  "GRANT embedding_pipeline TO \"$username\""
echo "provision-embedding-pipeline-login: $username is a member of embedding_pipeline"
