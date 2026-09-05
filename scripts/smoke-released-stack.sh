#!/usr/bin/env bash
set -euo pipefail

GM_RELEASED_STACK_ENV_FILE="${GM_RELEASED_STACK_ENV_FILE:-.env}"
GM_RELEASED_STACK_PROJECT="${GM_RELEASED_STACK_PROJECT:-groovemap-released-smoke}"
# ADR 0005 gave each source its own producer repository and image variable.
GM_REQUIRED_IMAGE_VARIABLES=(
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
GM_INFRASTRUCTURE_SERVICES=(rabbitmq postgres neo4j redis)
GM_APPLICATION_SERVICES=(
  api
  extractor-discogs
  extractor-musicbrainz
  graphinator
  brainzgraphinator
  tableinator
  brainztableinator
  dashboard
  explore
  insights
)

if [ ! -f "${GM_RELEASED_STACK_ENV_FILE}" ]; then
  echo "error: set GM_RELEASED_STACK_ENV_FILE to a reviewed released-image environment file" >&2
  exit 2
fi

# The reviewed release set lives in scripts/check-images.py. Read the literal rather
# than executing the checker, so the gate answers the same on any working tree.
GM_APPROVED_DIGESTS=$(
  python3 - <<'GM_PY'
import ast
import pathlib

module = ast.parse(pathlib.Path("scripts/check-images.py").read_text())
for node in module.body:
    if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "RELEASED_IMAGE_DIGESTS" for target in node.targets):
        for key, value in zip(node.value.keys, node.value.values):
            print(key.value, value.value)
GM_PY
)
if [ -z "${GM_APPROVED_DIGESTS}" ]; then
  echo "error: scripts/check-images.py declares no RELEASED_IMAGE_DIGESTS to validate against" >&2
  exit 2
fi

for GM_IMAGE_VARIABLE in "${GM_REQUIRED_IMAGE_VARIABLES[@]}"; do
  GM_IMAGE_VALUE=$(sed -n "s/^${GM_IMAGE_VARIABLE}=//p" "${GM_RELEASED_STACK_ENV_FILE}")
  if [[ ! "${GM_IMAGE_VALUE}" =~ ^ghcr\.io/groovemap-music/[a-z0-9-]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "error: ${GM_IMAGE_VARIABLE} must be an approved immutable GrooveMap image digest" >&2
    exit 2
  fi
  if [[ "${GM_IMAGE_VALUE}" == *"@sha256:1111111111111111111111111111111111111111111111111111111111111111" ]]; then
    echo "error: ${GM_IMAGE_VARIABLE} still contains the validation-only digest" >&2
    exit 2
  fi
  GM_APPROVED_DIGEST=$(printf '%s\n' "${GM_APPROVED_DIGESTS}" | sed -n "s/^${GM_IMAGE_VARIABLE} //p")
  if [ -z "${GM_APPROVED_DIGEST}" ]; then
    echo "error: ${GM_IMAGE_VARIABLE} has no reviewed release digest in scripts/check-images.py" >&2
    exit 2
  fi
  if [[ "${GM_IMAGE_VALUE}" != *"@sha256:${GM_APPROVED_DIGEST}" ]]; then
    echo "error: ${GM_IMAGE_VARIABLE} must promote the reviewed release digest sha256:${GM_APPROVED_DIGEST}" >&2
    exit 2
  fi
done

GM_COMPOSE=(
  docker compose
  --project-name "${GM_RELEASED_STACK_PROJECT}"
  --env-file "${GM_RELEASED_STACK_ENV_FILE}"
  -f docker-compose.yml
  -f docker-compose.smoke.yml
)

gm_cleanup() {
  GM_EXIT_CODE=$?
  if [ "${GM_EXIT_CODE}" -ne 0 ]; then
    "${GM_COMPOSE[@]}" ps --all >&2 || true
    "${GM_COMPOSE[@]}" logs --timestamps --no-color --tail 200 >&2 || true
  fi
  "${GM_COMPOSE[@]}" down --volumes --remove-orphans || true
  exit "${GM_EXIT_CODE}"
}
trap gm_cleanup EXIT

"${GM_COMPOSE[@]}" config --quiet
"${GM_COMPOSE[@]}" up -d --wait "${GM_INFRASTRUCTURE_SERVICES[@]}"

# Schema ownership stays in database-schema. Prove the released one-shot image
# succeeds before any application service is allowed to start, then prove its
# DDL is idempotent against the same disposable databases.
"${GM_COMPOSE[@]}" run --rm --no-deps schema-init
"${GM_COMPOSE[@]}" run --rm --no-deps schema-init

"${GM_COMPOSE[@]}" up -d --wait "${GM_APPLICATION_SERVICES[@]}"
"${GM_COMPOSE[@]}" ps "${GM_INFRASTRUCTURE_SERVICES[@]}" schema-init "${GM_APPLICATION_SERVICES[@]}"

# Compose sends SIGTERM and waits for each service's consumer-drain path before
# resorting to SIGKILL. A non-zero stop or timeout fails and retains logs.
"${GM_COMPOSE[@]}" stop --timeout 30 "${GM_APPLICATION_SERVICES[@]}"
