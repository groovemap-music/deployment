#!/bin/sh
# The store-check-postgres one-shot (docker-compose.external-postgres.yml). It verifies the
# external PostgreSQL before any GrooveMap service connects, and exits nonzero with the reason,
# so `docker compose up` never starts a service against a server that cannot hold GrooveMap.
# `just deploy` also runs it on its own first, to stop before anything starts.
#
# Read-only: SELECT and SHOW inside the configured database, nothing else (no DDL, no other
# database). Credentials come from the Docker secret files; the password reaches psql through
# PGPASSWORD, never a command line. Output never names the role or prints the password.
#
# Checks, each with what to do when it fails:
#   - transport security: PGSSLMODE verify-full (default) or verify-ca with the CA in PGSSLROOTCERT,
#     or require (encrypted, unverified). A cleartext mode (disable, allow, prefer) is refused for a
#     server off this machine unless GROOVEMAP_ALLOW_PLAINTEXT (EXTERNAL_POSTGRES_ALLOW_PLAINTEXT) is
#     true. `just deploy` refuses the same settings first; this keeps a bare `docker compose up` honest;
#   - a connection to PGHOST:PGPORT/POSTGRES_DB with the configured role;
#   - PostgreSQL 18 or newer (the schema uses uuidv7());
#   - pg_trgm installed in the database (pre-provisioned mode never creates it);
#   - the role may create schemas in the database and objects in public;
#   - at least 60 usable connections (database-schema's connection budget), and no role or
#     database connection limit below that;
#   - connection headroom: the server's max_connections minus superuser_reserved_connections,
#     reserved_connections and the connections in use now, and each role or database limit minus the
#     connections in use under it, must leave the 60 the services need. Every number is printed.
#     When GrooveMap already runs against this server (STORE_CHECK_STACK_RUNNING=1, set by
#     `just deploy`), its own connections to this database count as headroom, since a re-run
#     replaces them;
#   - session pooling: two concurrent sessions must not share a server backend. A PgBouncer in
#     transaction or statement mode hands the second session the backend the first one just
#     released; catalog-api's session-level advisory lock breaks silently there. A direct
#     connection or a session-mode pool always gives each session its own backend, so this
#     check never refuses one of those.

set -u

MIN_SERVER_VERSION_NUM=180000
MIN_CONNECTIONS=60
# `just deploy` sets this to 1 when the stack is already running, so its connections are not counted against it.
stack_running="${STORE_CHECK_STACK_RUNNING:-0}"
SESSION_HOLD_SECONDS=3

fail() {
  echo "store-check-postgres: FAILED: $*" >&2
  exit 1
}
ok() { echo "store-check-postgres: ok: $*"; }
warn() { echo "store-check-postgres: WARNING: $*"; }
# Loopback, or this Docker host as the overlay maps it (host.docker.internal -> host-gateway).
local_host() {
  case "$1" in
    host.docker.internal | localhost | ::1 | "[::1]" | 127.*) return 0 ;;
  esac
  return 1
}

[ -n "${PGHOST:-}" ] || fail "PGHOST is not set (EXTERNAL_POSTGRES_HOST in .env)"
db="${POSTGRES_DB:-groovemap}"
where="${PGHOST}:${PGPORT:-5432}/${db}"

sslmode="${PGSSLMODE:-verify-full}"
PGSSLMODE="$sslmode"
export PGSSLMODE
case "$(printf '%s' "${GROOVEMAP_ALLOW_PLAINTEXT:-false}" | tr '[:upper:]' '[:lower:]')" in
  true | 1 | yes | on) allow_plaintext=1 ;;
  false | 0 | no | off | '') allow_plaintext=0 ;;
  *) fail "GROOVEMAP_ALLOW_PLAINTEXT must be true or false (EXTERNAL_POSTGRES_ALLOW_PLAINTEXT in .env)" ;;
esac
case "$sslmode" in
  verify-ca | verify-full)
    [ -s "${PGSSLROOTCERT:-}" ] || fail "sslmode=$sslmode needs the CA that signed the server's certificate in secrets/postgres_ca.crt (PGSSLROOTCERT)"
    ;;
  require) ;;
  disable | allow | prefer)
    if ! local_host "$PGHOST" && [ "$allow_plaintext" -eq 0 ]; then
      fail "sslmode=$sslmode may send the password and every query to $PGHOST in the clear; set EXTERNAL_POSTGRES_SSLMODE=verify-full with secrets/postgres_ca.crt, or EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true to accept that risk"
    fi
    ;;
  *) fail "PGSSLMODE=$sslmode is not a libpq sslmode (EXTERNAL_POSTGRES_SSLMODE in .env)" ;;
esac

user=""
if [ -n "${POSTGRES_USER_FILE:-}" ] && [ -r "$POSTGRES_USER_FILE" ]; then
  user="$(cat "$POSTGRES_USER_FILE")"
fi
[ -n "$user" ] || fail "no PostgreSQL role: ${POSTGRES_USER_FILE:-POSTGRES_USER_FILE} is missing or empty (write the GrooveMap role's name to secrets/postgres_username.txt)"
password=""
if [ -n "${POSTGRES_PASSWORD_FILE:-}" ] && [ -r "$POSTGRES_PASSWORD_FILE" ]; then
  password="$(cat "$POSTGRES_PASSWORD_FILE")"
fi
[ -n "$password" ] || fail "no PostgreSQL password: ${POSTGRES_PASSWORD_FILE:-POSTGRES_PASSWORD_FILE} is missing or empty (write the role's password to secrets/postgres_password.txt)"
PGPASSWORD="$password"
export PGPASSWORD
unset password

errors="$(mktemp)"
held="$(mktemp)"
trap 'rm -f "$errors" "$held"' EXIT

q() {
  psql -X -U "$user" -d "$db" -v ON_ERROR_STOP=1 -tAq -c "$1" 2>"$errors"
}
last_error() {
  error="$(tail -n 1 "$errors")"
  printf '%s' "${error:-no output}"
}

# --- reachability and identity ---------------------------------------------------------

if ! answer="$(q "SELECT 1")" || [ "$answer" != "1" ]; then
  fail "cannot connect to PostgreSQL at $where with the role in secrets/postgres_username.txt: $(last_error)"
fi
ok "connected to $where"

tls="$(q "SELECT CASE WHEN ssl THEN version ELSE 'off' END FROM pg_stat_ssl WHERE pid = pg_backend_pid()")" || tls=""
case "$sslmode:${tls:-off}" in
  verify-full:off | verify-ca:off | require:off) fail "sslmode=$sslmode but the session to $where is not encrypted" ;;
  verify-full:*) ok "TLS ${tls}, server certificate and host name verified (sslmode=verify-full)" ;;
  verify-ca:*) ok "TLS ${tls}, server certificate verified, host name not checked (sslmode=verify-ca)" ;;
  require:*) warn "TLS ${tls} but the server is not verified (sslmode=require): a server impersonating it is not detected" ;;
  *:off) warn "the session to $where is not encrypted (sslmode=$sslmode): the password and every query cross the network in the clear" ;;
  *) ok "TLS ${tls} (sslmode=$sslmode, server not verified)" ;;
esac

version_num="$(q "SHOW server_version_num")" || fail "cannot read the server version at $where: $(last_error)"
case "$version_num" in
  '' | *[!0-9]*) fail "unreadable server_version_num from $where: $version_num" ;;
esac
version="$(q "SHOW server_version")" || version="$version_num"
if [ "$version_num" -lt "$MIN_SERVER_VERSION_NUM" ]; then
  fail "PostgreSQL $version at $where; GrooveMap needs PostgreSQL 18 or newer (its schema uses uuidv7())"
fi
ok "PostgreSQL $version"

# --- what pre-provisioned mode needs ---------------------------------------------------

trgm="$(q "SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")" || fail "cannot list the extensions in $db: $(last_error)"
[ -n "$trgm" ] || fail "pg_trgm is not installed in $db; an administrator runs CREATE EXTENSION pg_trgm; connected to $db (docs/homelab.md, Existing PostgreSQL and Valkey)"
ok "pg_trgm $trgm installed"
vector="$(q "SELECT extversion FROM pg_extension WHERE extname = 'vector'")" || vector=""
if [ -n "$vector" ]; then
  ok "vector $vector installed"
else
  echo "store-check-postgres: note: vector is not installed in $db; artist embeddings stay off (optional)"
fi

can_create="$(q "SELECT has_database_privilege(current_database(), 'CREATE') AND has_schema_privilege('public', 'CREATE')")" ||
  fail "cannot read the role's privileges in $db: $(last_error)"
[ "$can_create" = "t" ] || fail "the role cannot create schemas in $db and objects in its public schema; make it the database owner, or GRANT CREATE ON DATABASE $db and GRANT CREATE ON SCHEMA public to it"
ok "the role may create schemas in $db"

superuser="$(q "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" || superuser=""
if [ "$superuser" = "t" ]; then
  echo "store-check-postgres: note: the role is a superuser; a shared server should give GrooveMap a plain login that owns only its database"
fi

# --- connection budget -----------------------------------------------------------------

usable="$(q "SELECT current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int - current_setting('reserved_connections')::int")" ||
  fail "cannot read max_connections at $where: $(last_error)"
case "$usable" in
  '' | *[!0-9-]*) fail "unreadable connection settings from $where: $usable" ;;
esac
if [ "$usable" -lt "$MIN_CONNECTIONS" ]; then
  fail "the server has $usable usable connections (max_connections minus the reserved ones); GrooveMap's services need at least $MIN_CONNECTIONS on top of other applications"
fi
limits="$(q "SELECT (SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user) || ' ' || (SELECT datconnlimit FROM pg_database WHERE datname = current_database())")" ||
  fail "cannot read the connection limits at $where: $(last_error)"
role_limit="${limits%% *}"
database_limit="${limits##* }"
for limit in "$role_limit" "$database_limit"; do
  case "$limit" in
    '' | *[!0-9-]*) fail "unreadable connection limits from $where: $limits" ;;
  esac
  if [ "$limit" -ge 0 ] && [ "$limit" -lt "$MIN_CONNECTIONS" ]; then
    fail "a connection limit of $limit applies to the role or to $db; GrooveMap needs at least $MIN_CONNECTIONS (ALTER ROLE ... CONNECTION LIMIT / ALTER DATABASE ... CONNECTION LIMIT)"
  fi
done
ok "$usable usable connections; role limit $role_limit, database limit $database_limit (-1 is none)"

# Connections in use now, other than this check's own session: the whole server, this role, this
# database, and this role in this database (GrooveMap's own, when it is the role's only user). A
# session that PostgreSQL hides from a role without pg_read_all_stats still counts in the server total.
in_use="$(q "SELECT count(*) FILTER (WHERE backend_type = 'client backend') || ' ' || count(*) FILTER (WHERE backend_type = 'client backend' AND usename = current_user) || ' ' || count(*) FILTER (WHERE backend_type = 'client backend' AND datname = current_database()) || ' ' || count(*) FILTER (WHERE backend_type = 'client backend' AND usename = current_user AND datname = current_database()) FROM pg_stat_activity WHERE pid <> pg_backend_pid()")" ||
  fail "cannot count the connections in use at $where: $(last_error)"
# shellcheck disable=SC2086 # four space-separated integers
set -- $in_use
[ "$#" -eq 4 ] || fail "unreadable connection counts from $where: $in_use"
server_used="$1"
role_used="$2"
database_used="$3"
own_used="$4"
for count in "$server_used" "$role_used" "$database_used" "$own_used"; do
  case "$count" in
    '' | *[!0-9]*) fail "unreadable connection counts from $where: $in_use" ;;
  esac
done
credit=0
[ "$stack_running" = "1" ] && credit="$own_used"
headroom=$((usable - server_used + credit))
detail="server $headroom free (max_connections minus the reserved ones $usable, $server_used in use"
[ "$credit" -gt 0 ] && detail="$detail, $credit of them GrooveMap's own"
detail="$detail)"
for kind in role database; do
  if [ "$kind" = role ]; then
    limit="$role_limit"
    used="$role_used"
  else
    limit="$database_limit"
    used="$database_used"
  fi
  if [ "$limit" -ge 0 ]; then
    free=$((limit - used + credit))
    detail="$detail; $kind limit $limit with $used in use leaves $free"
    [ "$free" -lt "$headroom" ] && headroom="$free"
  else
    detail="$detail; $kind unlimited ($used in use)"
  fi
done
if [ "$headroom" -lt "$MIN_CONNECTIONS" ]; then
  fail "only $headroom connections of headroom at $where ($detail); GrooveMap's services need $MIN_CONNECTIONS. Free connections on the server, raise the limit (ALTER ROLE ... CONNECTION LIMIT / ALTER DATABASE ... CONNECTION LIMIT / max_connections), or run GrooveMap's own PostgreSQL"
fi
ok "connection headroom $headroom of the $MIN_CONNECTIONS the services need ($detail)"

# --- session pooling -------------------------------------------------------------------

# The first session stays connected, idle, while the second one asks for its backend.
{
  echo "SELECT pg_backend_pid();"
  sleep "$SESSION_HOLD_SECONDS"
} | psql -X -U "$user" -d "$db" -v ON_ERROR_STOP=1 -tAq >"$held" 2>"$errors" &
holder=$!
sleep 1
second="$(q "SELECT pg_backend_pid()")" || second=""
wait "$holder" || true
first="$(head -n 1 "$held")"
case "$first:$second" in
  *[!0-9:]* | :* | *:) fail "could not hold two sessions open at once on $where to check the pooling mode: $(last_error)" ;;
esac
if [ "$first" = "$second" ]; then
  fail "two concurrent sessions shared one server backend (pid $first): $where pools by transaction or statement (PgBouncer pool_mode=transaction?). GrooveMap needs a direct connection or a session-mode pool: catalog-api holds a session-level advisory lock"
fi
ok "each session gets its own server backend (direct connection or session pooling)"

echo "store-check-postgres: $where is ready for GrooveMap"
