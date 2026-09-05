#!/usr/bin/env bash
# Reset a user's password in the GrooveMap PostgreSQL database.
#
# Usage:
#   ./scripts/reset-password.sh <postgres_password> <email> <new_password>
#
# Example:
#   ./scripts/reset-password.sh groovemap user@example.com mynewpassword123

set -euo pipefail

if [ $# -ne 3 ]; then
  echo "Usage: $0 <postgres_password> <email> <new_password>"
  echo "Example: $0 groovemap user@example.com mynewpassword123"
  exit 1
fi

GM_POSTGRES_PASSWORD="$1"
GM_EMAIL="$2"
GM_NEW_PASSWORD="$3"
GM_COMPOSE=(docker compose)

if [ ${#GM_NEW_PASSWORD} -lt 8 ]; then
  echo "Error: Password must be at least 8 characters."
  exit 1
fi

# Generate the PBKDF2-SHA256 hash using Python (matches api/auth.py format)
# Pass the password via environment variable to avoid shell injection. Address
# Compose services rather than retained container names so project overrides work.
GM_HASHED=$("${GM_COMPOSE[@]}" exec -T -e "RESET_PW=${GM_NEW_PASSWORD}" api python3 -c "
import hashlib, os
password = os.environ['RESET_PW']
salt = os.urandom(32)
key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
print(salt.hex() + ':' + key.hex())
") || {
  echo "Error: Failed to generate password hash. Ensure the Compose api service is running."
  exit 1
}

if [ -z "$GM_HASHED" ]; then
  echo "Error: Failed to generate password hash (empty result)."
  exit 1
fi

echo "Updating password for: ${GM_EMAIL}"

GM_RESULT=$("${GM_COMPOSE[@]}" exec -T -e "PGPASSWORD=${GM_POSTGRES_PASSWORD}" postgres \
  psql -U groovemap -d groovemap -t -A \
  -v "hashed=${GM_HASHED}" -v "email=${GM_EMAIL}" -c \
  "UPDATE users SET hashed_password = :'hashed', password_changed_at = NOW(), updated_at = NOW() WHERE email = :'email' RETURNING email;")

if [ -z "$GM_RESULT" ]; then
  echo "Error: No user found with email '${GM_EMAIL}'."
  echo ""
  echo "Existing users:"
  "${GM_COMPOSE[@]}" exec -T -e "PGPASSWORD=${GM_POSTGRES_PASSWORD}" postgres \
    psql -U groovemap -d groovemap -t -A -c \
    "SELECT email FROM users;"
  exit 1
fi

echo "Password reset successfully for: ${GM_RESULT}"
