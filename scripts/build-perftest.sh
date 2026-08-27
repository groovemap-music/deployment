#!/usr/bin/env bash
set -euo pipefail

docker-buildx build --load --tag groovemap/perftest:local tests/perftest
