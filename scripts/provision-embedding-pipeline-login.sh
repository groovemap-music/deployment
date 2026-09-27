#!/usr/bin/env bash
# Creates the LOGIN role that authenticates analytics-engine's embedding pipeline
# and grants it membership in database-schema's NOLOGIN `embedding_pipeline`
# group role (ADR 0013's 2026-09-24 amendment: "The login that is a member of
# that role is provisioned where credentials live: `deployment` for development
# and CI"). Idempotent — safe to re-run after every dump load, before
# scripts/run-embeddings.sh.
#
# The password never appears on a command line (docker exec argv, psql -c, or
# this script's own argv) or gets interpolated into SQL text directly: it
# crosses into the container only as an environment variable
# (`docker exec -e`), and psql's own `\getenv` plus `:'pw'` variable
# interpolation quotes it as a literal, so an embedded quote in the password
# can never break out of the SQL string or inject a second statement. The
# username is validated as a plain identifier below and always substituted via
# psql's `:"username"` quoted-identifier interpolation, never string-built.
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

# A plain lowercase identifier. psql's :"username" interpolation already quotes
# whatever this holds, so nothing below is vulnerable to an embedded quote —
# this check exists so a stray shell-metacharacter value fails loudly here
# instead of becoming a confusing role name PostgreSQL happens to accept.
[[ "$username" =~ ^[a-z_][a-z0-9_]*$ ]] || {
  echo "provision-embedding-pipeline-login: EMBEDDING_PIPELINE_POSTGRES_USERNAME must match" >&2
  echo "  ^[a-z_][a-z0-9_]*\$ (a plain lowercase identifier), got: $username" >&2
  exit 2
}

docker inspect --format '{{.State.Running}}' "$POSTGRES_CONTAINER" | grep -qx true || {
  echo "provision-embedding-pipeline-login: $POSTGRES_CONTAINER is not running." >&2
  exit 2
}

role_exists() {
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -v ON_ERROR_STOP=1 \
    -v "role=$1" -c "SELECT 1 FROM pg_roles WHERE rolname = :'role'"
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
  # The password crosses into the container as an environment variable, never
  # as part of the SQL text or this command's own argv; \getenv reads it back
  # inside psql, and :'pw' interpolates it as a properly quoted literal.
  docker exec -i -e EMBEDDING_PIPELINE_PASSWORD="$password" "$POSTGRES_CONTAINER" \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v "username=$username" <<'SQL'
\getenv pw EMBEDDING_PIPELINE_PASSWORD
CREATE ROLE :"username" LOGIN PASSWORD :'pw';
SQL
  echo "provision-embedding-pipeline-login: created $username"
fi

docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
  -v "username=$username" -c 'GRANT embedding_pipeline TO :"username"'
echo "provision-embedding-pipeline-login: $username is a member of embedding_pipeline"
