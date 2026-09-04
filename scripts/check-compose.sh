#!/usr/bin/env bash
set -euo pipefail

docker compose --env-file config/validation.env config --quiet
docker compose --env-file config/validation.env \
  -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose --env-file config/validation.env \
  -f docker-compose.yml -f docker-compose.smoke.yml config --quiet
docker compose --env-file config/validation.env \
  -f docker-compose.yml -f docker-compose.media-smoke.yml config --quiet
