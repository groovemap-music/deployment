#!/usr/bin/env bash
set -euo pipefail

image="${PERFTEST_IMAGE:-}"
network="${PERFTEST_NETWORK:-deployment_groovemap}"
config="${PERFTEST_CONFIG:-tests/perftest/config.yaml}"
results="${PERFTEST_RESULTS:-perftest-results}"

[[ "$image" =~ ^[^[:space:]]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "PERFTEST_IMAGE must be an approved immutable image reference containing @sha256:<64 hex>." >&2
  exit 2
}
[[ -f "$config" ]] || { echo "PERFTEST_CONFIG does not name a file." >&2; exit 2; }
[[ -n "$network" && "$network" != *[[:space:]]* ]] || { echo "PERFTEST_NETWORK is invalid." >&2; exit 2; }

mkdir -p "$results"
docker run --rm \
  --network "$network" \
  --volume "$(pwd)/$results:/results" \
  --volume "$(pwd)/$config:/config/config.yaml:ro" \
  "$image"
