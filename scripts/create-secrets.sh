#!/usr/bin/env bash
# Bootstrap the secrets/ directory for production deployment.
# Run this once on the host before starting docker-compose.prod.yml.
#
# Usage: bash scripts/create-secrets.sh
#
# Behaviour:
#   - Skips files that already exist (idempotent — safe to re-run)
#   - Sets secrets/ to mode 700 and each file to 600

set -euo pipefail

SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets"

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

write_secret() {
  local name="$1"
  local value="$2"
  local path="$SECRETS_DIR/$name"
  if [ -f "$path" ]; then
    echo "[skip] $name already exists"
  else
    printf '%s' "$value" >"$path"
    chmod 600 "$path"
    echo "[ok]   $name created"
  fi
}

# Generated passwords use [A-Za-z0-9] only. Base64 output can contain "/" and "+", which
# neo4j rejected as a password, and which would also need percent-encoding wherever a
# password is embedded in a URL or key=value connection string (AMQP, Postgres, Valkey)
# or passed on a command line (cypher-shell, valkey-cli). 36 characters of a 62-symbol
# alphabet is about 214 bits, above the 192 bits of the previous 24 random bytes.
random_password() {
  python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(36)), end="")'
}

# Like write_secret, but always rewritten: the content is derived from another
# secret, so a stale copy would disagree with its source.
write_derived_secret() {
  local name="$1"
  local value="$2"
  local path="$SECRETS_DIR/$name"
  rm -f "$path"
  (umask 077 && printf '%s' "$value" >"$path")
  chmod 600 "$path"
  echo "[ok]   $name derived"
}

# JWT secret key (hex, 32 bytes = 64 hex chars)
write_secret "jwt_secret_key.txt" "$(openssl rand -hex 32)"

# HKDF master encryption key (derives OAuth + TOTP keys)
write_secret "encryption_master_key.txt" "$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")')"

# Resend API key — required for prod email delivery; get from https://resend.com/api-keys
# When empty, password reset links are logged instead of emailed.
write_secret "resend_api_key.txt" "${RESEND_API_KEY:-}"

# NLQ (natural-language query) provider API key — optional, feature is off by
# default (NLQ_ENABLED unset). The file must exist regardless: docker-compose.prod.yml
# declares it as a file-backed secret for the api service, and compose refuses to
# create the container when the source file is missing.
write_secret "nlq_api_key.txt" "${NLQ_API_KEY:-}"

# PostgreSQL credentials
write_secret "postgres_username.txt" "groovemap"
write_secret "postgres_password.txt" "$(random_password)"

# RabbitMQ credentials
write_secret "rabbitmq_username.txt" "groovemap"
write_secret "rabbitmq_password.txt" "$(random_password)"

# Neo4j password
write_secret "neo4j_password.txt" "$(random_password)"

# Valkey password (requirepass — see scripts/valkey-entrypoint.sh)
write_secret "valkey_password.txt" "$(random_password)"

# valkey-exporter's password file. oliver006/redis_exporter reads
# --redis.password-file as a JSON map of Redis URI to password; the URI must equal
# REDIS_ADDR in docker-compose.yml. Derived from valkey_password.txt (whether just
# created or already present) and refreshed on every run.
VALKEY_EXPORTER_ADDR="redis://valkey:6379"
write_derived_secret "valkey_exporter_passwords.json" \
  "$(python3 -c 'import json, sys; print(json.dumps({sys.argv[1]: open(sys.argv[2]).read()}), end="")' \
    "$VALKEY_EXPORTER_ADDR" "$SECRETS_DIR/valkey_password.txt")"

# Shared secret gating /api/internal/insights/* — MUST match between api and
# insights (both read the same secrets/insights_internal_secret.txt file).
write_secret "insights_internal_secret.txt" "$(openssl rand -hex 32)"

# Grafana admin password. docker-compose.prod.yml disables anonymous access and
# reads this file via GF_SECURITY_ADMIN_PASSWORD__FILE, so without it the
# dashboards have no reachable login.
write_secret "grafana_admin_password.txt" "$(random_password)"

# Grafana Viewer service-account token for the console's alerts — optional. The
# file must exist regardless (docker-compose.prod.yml mounts it for the dashboard
# service); empty content means alerts are off, with no note. Create the token per
# docs/maintenance.md, then write it to the file or re-run with GRAFANA_VIEWER_TOKEN set.
write_secret "grafana_viewer_token.txt" "${GRAFANA_VIEWER_TOKEN:-}"

# Embedding pipeline login (ADR 0013's 2026-09-24 amendment) — a member of
# database-schema's NOLOGIN embedding_pipeline group role, never the
# postgres_username superuser above. This only creates the credential file;
# scripts/provision-embedding-pipeline-login.sh grants the actual database role.
write_secret "embedding_pipeline_username.txt" "embedding_pipeline"
write_secret "embedding_pipeline_password.txt" "$(random_password)"

echo ""
echo "✅ secrets/ is ready. Files are owner-read-only (chmod 600)."
echo "   Never commit secrets/ to version control."
