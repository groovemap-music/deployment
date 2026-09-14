#!/usr/bin/env bash
set -euo pipefail

check_compose() {
  local command=(docker compose --env-file config/validation.env)
  local compose_file
  for compose_file in "$@"; do
    command+=(-f "$compose_file")
  done
  "${command[@]}" config --quiet
}

check_compose
check_compose docker-compose.yml docker-compose.prod.yml
check_compose docker-compose.yml docker-compose.smoke.yml
check_compose docker-compose.yml docker-compose.media-smoke.yml
check_compose docker-compose.yml docker-compose.released-fixture-smoke.yml
