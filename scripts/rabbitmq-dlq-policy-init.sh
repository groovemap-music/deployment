#!/bin/sh
# One-shot, idempotent entrypoint for the rabbitmq-dlq-policy-init service
# (gm-deployment-8mb.1). Declares the catalog-DLQ size-cap policy over the
# RabbitMQ management HTTP API using rabbitmqadmin, which is bundled in the
# rabbitmq:*-management image this service reuses -- no new third-party image.
#
# This runs AFTER the broker is healthy (docker-compose.yml's
# depends_on/condition: service_healthy), not at broker boot via
# `load_definitions`: on a blank node, a boot-time definitions import skips
# creating the default vhost/user that RABBITMQ_DEFAULT_USER/PASS/VHOST would
# otherwise create ("Nuances of Boot-time Definition Import",
# rabbitmq.com/docs/definitions), which would break every service's auth on a
# fresh install. `rabbitmqadmin declare policy` only ever touches the policy
# object -- it never declares a queue or exchange, so a catalog service's DLQ
# is never redeclared, and RabbitMQ still refuses to change an existing
# classic queue's type at startup either way.
#
# Reads /run/secrets/rabbitmq_username and /run/secrets/rabbitmq_password when
# present (the production overlay's Docker _FILE secret convention, mirroring
# scripts/rabbitmq-entrypoint.sh) and otherwise uses the
# RABBITMQADMIN_USERNAME/PASSWORD already set in the container environment.
set -eu

if [ -f /run/secrets/rabbitmq_username ]; then
  RABBITMQADMIN_USERNAME="$(cat /run/secrets/rabbitmq_username)"
  export RABBITMQADMIN_USERNAME
fi

if [ -f /run/secrets/rabbitmq_password ]; then
  RABBITMQADMIN_PASSWORD="$(cat /run/secrets/rabbitmq_password)"
  export RABBITMQADMIN_PASSWORD
fi

definition=$(printf '{"max-length":%s,"max-length-bytes":%s,"overflow":"%s"}' \
  "${RABBITMQ_DLQ_MAX_LENGTH:-50000}" \
  "${RABBITMQ_DLQ_MAX_LENGTH_BYTES:-536870912}" \
  "${RABBITMQ_DLQ_OVERFLOW:-drop-head}")

exec rabbitmqadmin declare policy \
  --name=catalog-dlq-cap \
  --pattern='^groovemap-.*\.dlq$' \
  --apply-to=queues \
  --priority=10 \
  --definition="$definition"
