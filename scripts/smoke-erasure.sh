#!/usr/bin/env bash
# Operator-approved, disposable end-to-end assertion that the ADR 0010 erasure and export
# boundary holds: an account's activity can be exported as well-formed NDJSON, and after an
# erasure nothing keyed to that account survives in PostgreSQL, Neo4j, or Redis. It starts
# containers and destroys their volumes on exit, so it never runs inside `just check` or CI.
set -euo pipefail

project="${SMOKE_ERASURE_PROJECT:-groovemap-erasure-smoke}"
api_port="${SMOKE_ERASURE_API_PORT:-18004}"
timeout="${SMOKE_ERASURE_TIMEOUT:-300}"
env_file="${SMOKE_ERASURE_ENV_FILE:-.env}"

# The stack the assertion needs. Compose starts each service's declared dependencies, so
# naming the API pulls in the schema initializer and all three stores. No extractor, no
# broker consumer: this run drives the API itself rather than a catalog ingest.
services=(api)

compose=(
  docker compose
  --project-name "$project"
  --env-file "$env_file"
  -f docker-compose.yml
  -f docker-compose.erasure-smoke.yml
)

[[ -f "$env_file" ]] || {
  echo "smoke-erasure: $env_file is missing. The operator provides it: copy .env.example and" >&2
  echo "               replace every image placeholder with an approved digest-pinned GHCR" >&2
  echo "               reference (docs/maintenance.md records the promoted digests)." >&2
  exit 2
}

# A deletion assertion is only worth its runtime against the images an environment would
# actually run, so refuse the placeholder and validation-only digests outright. An .env
# that declares no image at all is refused too: the loop below would otherwise never run
# and the gate would pass without having inspected a single image.
image_assignments=0
while IFS= read -r assignment; do
  image_assignments=$((image_assignments + 1))
  variable="${assignment%%=*}"
  value="${assignment#*=}"
  case "$value" in
    *REPLACE_WITH*)
      echo "smoke-erasure: $variable still holds the .env.example placeholder." >&2
      exit 2
      ;;
    *@sha256:1111111111111111111111111111111111111111111111111111111111111111)
      echo "smoke-erasure: $variable holds a config/validation.env digest, which names no published image." >&2
      exit 2
      ;;
  esac
  [[ "$value" =~ @sha256:[0-9a-f]{64}$ ]] || {
    echo "smoke-erasure: $variable must promote an approved image by manifest digest, got $value" >&2
    exit 2
  }
done < <(grep -E '^[A-Z0-9_]+_IMAGE=' "$env_file")

[[ "$image_assignments" -gt 0 ]] || {
  echo "smoke-erasure: $env_file declares no *_IMAGE assignment, so no image was checked." >&2
  echo "               Copy .env.example and set every image variable to an approved" >&2
  echo "               digest-pinned GHCR reference (docs/maintenance.md records the" >&2
  echo "               promoted digests)." >&2
  exit 2
}

cleanup() {
  exit_code=$?
  if [[ "$exit_code" -ne 0 ]]; then
    "${compose[@]}" ps --all >&2 || true
    "${compose[@]}" logs --timestamps --no-color --tail 200 api >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "$exit_code"
}
trap cleanup EXIT

"${compose[@]}" up -d --wait "${services[@]}"
"${compose[@]}" ps

uv run python scripts/smoke_erasure.py \
  --project "$project" \
  --env-file "$env_file" \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.erasure-smoke.yml \
  --api-port "$api_port" \
  --timeout "$timeout"
