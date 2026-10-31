#!/usr/bin/env bash
set -euo pipefail

check_compose() {
  # The MusicBrainz path sits behind a profile; validate every merge with it active so its
  # services and their overlay entries are checked too.
  local command=(docker compose --profile musicbrainz --env-file config/validation.env)
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
check_compose docker-compose.yml docker-compose.erasure-smoke.yml

# The profile is what keeps a bare `docker compose up` safe on first load, so assert both
# sides: absent by default, present when enabled, for the base and the production overlay.
musicbrainz_services=(extractor-musicbrainz brainzgraphinator brainztableinator)

check_profile() {
  local command=(docker compose --env-file config/validation.env)
  local compose_file service
  for compose_file in "$@"; do
    command+=(-f "$compose_file")
  done
  local without with
  without=$("${command[@]}" config --services)
  with=$("${command[@]}" --profile musicbrainz config --services)
  for service in "${musicbrainz_services[@]}"; do
    if grep -qx "$service" <<<"$without"; then
      echo "check-compose: $service must not start without the musicbrainz profile" >&2
      return 1
    fi
    if ! grep -qx "$service" <<<"$with"; then
      echo "check-compose: $service is missing with the musicbrainz profile" >&2
      return 1
    fi
  done
}

check_profile docker-compose.yml
check_profile docker-compose.yml docker-compose.prod.yml

# The homelab skill's example overrides must merge onto the production stack and pass the
# override check `just deploy` runs (scripts/deploy_helpers.py override-check).
check_override_example() {
  local stack=(docker compose --profile '*' --env-file config/validation.env -f docker-compose.yml -f docker-compose.prod.yml)
  local before after output status=0
  before="$(mktemp)"
  after="$(mktemp)"
  "${stack[@]}" config --format json >"$before"
  "${stack[@]}" -f "$1" config --format json >"$after"
  if ! output=$(uv run --quiet python scripts/deploy_helpers.py override-check "$before" "$after" 2>&1); then
    echo "check-compose: $1 does not pass the override check:" >&2
    echo "$output" >&2
    status=1
  fi
  rm -f "$before" "$after"
  return "$status"
}

for example in skills/groovemap-homelab/assets/override-*.yml; do
  check_compose docker-compose.yml docker-compose.prod.yml "$example"
  check_override_example "$example"
done
