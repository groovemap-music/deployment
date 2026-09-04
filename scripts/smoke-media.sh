#!/usr/bin/env bash
# Operator-approved, disposable end-to-end assertion that the ADR 0007 canonical media
# block reaches both stores. It starts containers and destroys their volumes on exit, so
# it never runs inside `just check` or CI. See docs/testing-guide.md.
set -euo pipefail

project="${SMOKE_MEDIA_PROJECT:-groovemap-media-smoke}"
broker_port="${SMOKE_MEDIA_RABBITMQ_PORT:-15673}"
timeout="${SMOKE_MEDIA_TIMEOUT:-300}"
env_file="${SMOKE_MEDIA_ENV_FILE:-.env}"

# The stack the assertion needs. Compose starts each service's declared dependencies, so
# this pulls in the schema initializer, the broker, and both stores. The extractors are
# deliberately absent: this run publishes the producers' promoted contract fixtures itself
# instead of downloading a dump.
services=(tableinator brainztableinator graphinator brainzgraphinator)

compose=(
  docker compose
  --project-name "$project"
  --env-file "$env_file"
  -f docker-compose.yml
  -f docker-compose.media-smoke.yml
)

[[ -f "$env_file" ]] || {
  echo "smoke-media: $env_file is missing. The operator provides it: copy .env.example and" >&2
  echo "             replace every image placeholder with an approved digest-pinned GHCR" >&2
  echo "             reference (docs/maintenance.md records the promoted digests)." >&2
  exit 2
}

# A media assertion is only worth its runtime against the images an environment would
# actually run, so refuse the placeholder and validation-only digests outright.
while IFS= read -r assignment; do
  variable="${assignment%%=*}"
  value="${assignment#*=}"
  case "$value" in
    *REPLACE_WITH*)
      echo "smoke-media: $variable still holds the .env.example placeholder." >&2
      exit 2
      ;;
    *@sha256:1111111111111111111111111111111111111111111111111111111111111111)
      echo "smoke-media: $variable holds a config/validation.env digest, which names no published image." >&2
      exit 2
      ;;
  esac
  [[ "$value" =~ @sha256:[0-9a-f]{64}$ ]] || {
    echo "smoke-media: $variable must promote an approved image by manifest digest, got $value" >&2
    exit 2
  }
done < <(grep -E '^[A-Z0-9_]+_IMAGE=' "$env_file")

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${compose[@]}" up -d "${services[@]}"
"${compose[@]}" ps

uv run python scripts/smoke_media.py \
  --project "$project" \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.media-smoke.yml \
  --broker-port "$broker_port" \
  --timeout "$timeout"
