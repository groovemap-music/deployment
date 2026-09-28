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

# rabbitmq is already healthy above, so rabbitmq-dlq-policy-init's
# depends_on/service_healthy condition is already satisfied; `compose wait` blocks
# until the one-shot container stops and surfaces its exit code (a container that
# merely exits, even with code 0, is not something `up --wait` accepts for an
# explicitly targeted service).
"${compose[@]}" up -d rabbitmq-dlq-policy-init
"${compose[@]}" wait rabbitmq-dlq-policy-init

# gm-deployment-8mb.1: prove a BLANK broker (this stack always starts from a fresh
# volume -- see the `down --volumes` cleanup below and on trap) still gets its default
# vhost/user AND the catalog-DLQ size-cap policy. A boot-time `load_definitions` import
# was tried first and rejected in review: on a blank node it skips creating the default
# vhost/user that RABBITMQ_DEFAULT_USER/PASS/VHOST would otherwise create, breaking
# every service's auth on a fresh install ("Nuances of Boot-time Definition Import",
# rabbitmq.com/docs/definitions). Applying the policy after the broker -- and its
# default vhost/user -- already exist, via the one-shot rabbitmq-dlq-policy-init
# service, avoids that trap; this is the proof.
check_rabbitmq_dlq_policy() {
  local vhosts users policies
  vhosts="$("${compose[@]}" exec -T rabbitmq rabbitmqctl list_vhosts --formatter=json)"
  grep -qF '"name":"/"' <<<"$vhosts" || {
    echo "blank broker is missing the default / vhost" >&2
    return 1
  }

  users="$("${compose[@]}" exec -T rabbitmq rabbitmqctl list_users --formatter=json)"
  grep -qF '"user":"groovemap"' <<<"$users" || {
    echo "blank broker is missing the default groovemap user" >&2
    return 1
  }

  policies="$("${compose[@]}" exec -T rabbitmq rabbitmqctl list_policies --formatter=json)"
  grep -qF '"name":"catalog-dlq-cap"' <<<"$policies" || {
    echo "blank broker is missing the catalog-dlq-cap policy" >&2
    return 1
  }
  grep -qF '"pattern":"^groovemap-.*\\.dlq$"' <<<"$policies" || {
    echo "catalog-dlq-cap policy pattern does not match the expected ^groovemap-.*\\.dlq\$" >&2
    return 1
  }

  echo "RabbitMQ DLQ-cap policy check passed: default vhost, default user, and catalog-dlq-cap policy all present on a blank broker."
}
check_rabbitmq_dlq_policy
