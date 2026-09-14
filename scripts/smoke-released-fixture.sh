#!/usr/bin/env bash
# Operator-approved, disposable proof that the released Discogs extractor consumes
# its packaged tiny dump and delivers the resulting release through RabbitMQ to both
# released database consumers. The stack is always removed, including its volumes.
set -euo pipefail

project="${SMOKE_RELEASED_FIXTURE_PROJECT:-groovemap-released-fixture-smoke}"
broker_port="${SMOKE_RELEASED_FIXTURE_RABBITMQ_PORT:-15674}"
timeout="${SMOKE_RELEASED_FIXTURE_TIMEOUT:-300}"
env_file="${SMOKE_RELEASED_FIXTURE_ENV_FILE:-.env}"
required_image_variables=(
  DATABASE_SCHEMA_IMAGE
  CATALOG_API_IMAGE
  DISCOGS_INGESTION_IMAGE
  MUSICBRAINZ_INGESTION_IMAGE
  DISCOGS_GRAPH_ENRICHER_IMAGE
  MUSICBRAINZ_GRAPH_ENRICHER_IMAGE
  DISCOGS_SQL_LOADER_IMAGE
  MUSICBRAINZ_SQL_LOADER_IMAGE
  OPERATIONS_CONSOLE_IMAGE
  GRAPH_EXPLORER_IMAGE
  ANALYTICS_ENGINE_IMAGE
)

[[ -f "$env_file" ]] || {
  echo "smoke-released-fixture: set SMOKE_RELEASED_FIXTURE_ENV_FILE to a reviewed released-image environment file" >&2
  exit 2
}

approved_digests=$(python3 scripts/check-images.py --released-digests)
for variable in "${required_image_variables[@]}"; do
  value=$(sed -n "s/^${variable}=//p" "$env_file")
  if [[ ! "$value" =~ ^ghcr\.io/groovemap-music/[a-z0-9-]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "smoke-released-fixture: $variable must be an approved immutable GrooveMap image digest" >&2
    exit 2
  fi
  approved=$(printf '%s\n' "$approved_digests" | sed -n "s/^${variable} //p")
  if [[ -z "$approved" || "$value" != *@sha256:"$approved" ]]; then
    echo "smoke-released-fixture: $variable must promote the reviewed release digest sha256:$approved" >&2
    exit 2
  fi
done

compose=(
  docker compose
  --project-name "$project"
  --env-file "$env_file"
  -f docker-compose.yml
  -f docker-compose.released-fixture-smoke.yml
)

cleanup() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 ]]; then
    "${compose[@]}" ps --all >&2 || true
    "${compose[@]}" logs --timestamps --no-color --tail 200 >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "$exit_code"
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait tableinator graphinator

uv run python scripts/smoke_media.py \
  --project "$project" \
  --env-file "$env_file" \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.released-fixture-smoke.yml \
  --broker-port "$broker_port" \
  --timeout "$timeout" \
  --extractor-service extractor-discogs
