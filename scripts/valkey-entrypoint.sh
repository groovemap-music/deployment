#!/bin/sh
# Thin wrapper for the valkey container.
# Reads /run/secrets/valkey_password and appends --requirepass to the
# valkey-server arguments before delegating to the official Valkey entrypoint.
# This is needed because Valkey does not natively support the Docker _FILE
# secret convention. Without this, prod valkey runs unauthenticated even
# though every other prod data store gets a _FILE-secret credential.
set -e

if [ -f /run/secrets/valkey_password ]; then
  VALKEY_PASSWORD="$(cat /run/secrets/valkey_password)"
  exec docker-entrypoint.sh "$@" --requirepass "$VALKEY_PASSWORD"
else
  exec docker-entrypoint.sh "$@"
fi
