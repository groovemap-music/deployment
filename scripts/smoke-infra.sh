#!/usr/bin/env bash
set -euo pipefail

compose=(
  docker-compose
  --project-name groovemap-deployment-smoke
  --env-file config/validation.env
  -f docker-compose.yml
  -f docker-compose.smoke.yml
)

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${compose[@]}" up -d --wait rabbitmq postgres neo4j redis
"${compose[@]}" ps rabbitmq postgres neo4j redis
