#!/bin/sh
# The store-check-valkey one-shot (docker-compose.external-valkey.yml). It verifies the external
# Valkey before api and insights connect, and exits nonzero with the reason. `just deploy` also
# runs it on its own first, to stop before anything starts.
#
# Read-only: it sends AUTH, SELECT, PING, INFO and QUIT, and touches no key. It never runs FLUSH*,
# CONFIG, or anything that writes. The password never reaches a command line and is never printed.
#
# Over TLS (the default) no credential is sent until the server's certificate has verified against
# VALKEY_TLS_CA_FILE, host name included unless VALKEY_TLS_VERIFY_HOSTNAME is false. valkey-cli
# checks the chain but never the host name (it has no hostname check at all), so a server holding
# any certificate from that CA could collect the password from it. The check therefore talks to
# Valkey through `openssl s_client -verify_hostname ... -verify_return_error`, first with nothing
# on stdin to verify the certificate, then with the commands, password included, on stdin of a
# connection that verifies again: the password only ever travels on a connection whose certificate
# matched the host name. In cleartext mode (accepted only on this machine, or with the opt-out)
# it uses valkey-cli with the password in REDISCLI_AUTH.
#
# Checks, each with what to do when it fails:
#   - transport security: TLS (VALKEY_TLS, default on) verified against VALKEY_TLS_CA_FILE and the
#     host name, before the password is sent. VALKEY_TLS_VERIFY_HOSTNAME=false skips only the host
#     name (with a warning); the chain is always verified. With TLS off, a server off this machine
#     is refused unless GROOVEMAP_ALLOW_PLAINTEXT (EXTERNAL_VALKEY_ALLOW_PLAINTEXT) is true;
#   - a non-empty key prefix that python-libraries accepts (letters, digits and _-.:, more than
#     colons): every GrooveMap key lives under it, so it must not be shared with another app;
#   - a connection to VALKEY_HOST:VALKEY_PORT, database VALKEY_DB, with the configured password;
#   - authentication on: a client without the password is refused;
#   - Valkey 8.0 or newer (or Redis 7.2 or newer); GrooveMap is tested with Valkey 9.1;
#   - maxmemory-policy noeviction: GrooveMap keeps security state that must never be evicted;
#   - AOF persistence on.

set -u

MIN_VALKEY="8.0"
MIN_REDIS="7.2"

fail() {
  echo "store-check-valkey: FAILED: $*" >&2
  exit 1
}
ok() { echo "store-check-valkey: ok: $*"; }
warn() { echo "store-check-valkey: WARNING: $*"; }
# Loopback, or this Docker host as the overlay maps it (host.docker.internal -> host-gateway).
local_host() {
  case "$1" in
    host.docker.internal | localhost | ::1 | "[::1]" | 127.*) return 0 ;;
  esac
  return 1
}
# python-libraries' strict flag: true/1/yes/on or false/0/no/off, unset means $2.
flag() {
  case "$(printf '%s' "${1:-$2}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) echo 1 ;;
    false | 0 | no | off) echo 0 ;;
    *) echo invalid ;;
  esac
}

[ -n "${VALKEY_HOST:-}" ] || fail "VALKEY_HOST is not set (EXTERNAL_VALKEY_HOST in .env)"
port="${VALKEY_PORT:-6379}"
db="${VALKEY_DB:-0}"
where="${VALKEY_HOST}:${port} database ${db}"

prefix="${VALKEY_KEY_PREFIX:-}"
[ -n "$prefix" ] || fail "VALKEY_KEY_PREFIX is empty (EXTERNAL_VALKEY_KEY_PREFIX in .env); on a shared Valkey every GrooveMap key needs its own prefix, such as groovemap:"
case "$prefix" in
  *[!A-Za-z0-9_.:-]*) fail "VALKEY_KEY_PREFIX may only contain letters, digits and _-.: (it is also part of SCAN patterns)" ;;
esac
[ -n "$(printf '%s' "$prefix" | tr -d ':')" ] || fail "VALKEY_KEY_PREFIX must contain more than colons"
case "$db" in
  '' | *[!0-9]*) fail "VALKEY_DB must be a database number, got $db" ;;
esac
ok "key prefix ${prefix}"

tls="$(flag "${VALKEY_TLS:-}" true)"
verify_hostname="$(flag "${VALKEY_TLS_VERIFY_HOSTNAME:-}" true)"
allow_plaintext="$(flag "${GROOVEMAP_ALLOW_PLAINTEXT:-}" false)"
[ "$tls" != invalid ] || fail "VALKEY_TLS must be true or false (EXTERNAL_VALKEY_TLS in .env)"
[ "$verify_hostname" != invalid ] || fail "VALKEY_TLS_VERIFY_HOSTNAME must be true or false (EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME in .env)"
[ "$allow_plaintext" != invalid ] || fail "GROOVEMAP_ALLOW_PLAINTEXT must be true or false (EXTERNAL_VALKEY_ALLOW_PLAINTEXT in .env)"
if [ "$tls" -eq 1 ]; then
  ca="${VALKEY_TLS_CA_FILE:-}"
  [ -s "$ca" ] || fail "TLS needs the CA that signed the server's certificate in secrets/valkey_ca.crt (VALKEY_TLS_CA_FILE)"
elif local_host "$VALKEY_HOST"; then
  ca=""
elif [ "$allow_plaintext" -eq 1 ]; then
  ca=""
  warn "TLS is off (EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true): the Valkey password and every key and value cross the network to $VALKEY_HOST in the clear"
else
  fail "TLS is off and $VALKEY_HOST is not on this machine: the Valkey password and every key and value would cross the network in the clear. Set EXTERNAL_VALKEY_TLS=true with secrets/valkey_ca.crt, or EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true to accept that risk"
fi

errors="$(mktemp)"
trap 'rm -f "$errors"' EXIT

last_error() {
  error="$(grep -iE 'verif|error|refused|unable|timed out' "$errors" | tail -n 1)"
  [ -n "$error" ] || error="$(tail -n 1 "$errors")"
  printf '%s' "${error:-no output}"
}
field() {
  printf '%s\n' "$1" | sed -n "s/^$2:\(.*\)$/\1/p" | head -n 1
}
# 1 when version $1 (major.minor[.patch]) is at least $2 (major.minor).
at_least() {
  awk -v have="$1" -v need="$2" 'BEGIN {
    split(have, h, "."); split(need, n, ".");
    exit !((h[1] + 0 > n[1] + 0) || (h[1] + 0 == n[1] + 0 && h[2] + 0 >= n[2] + 0))
  }'
}
# One RESP command, each argument a bulk string (so a password with spaces or symbols is intact).
# printf is a shell builtin, so the password is never an argument of an executed program.
resp() {
  printf '*%s\r\n' "$#"
  for word in "$@"; do
    printf '$%s\r\n%s\r\n' "$(printf '%s' "$word" | wc -c | tr -d ' ')" "$word"
  done
}

host="${VALKEY_HOST#[}"
host="${host%]}"
case "$host" in
  *:*) target="[$host]:$port" ip=1 ;;
  *[!0-9.]*) target="$host:$port" ip=0 ;;
  *) target="$host:$port" ip=1 ;;
esac
# A TLS connection that fails unless the certificate verifies against the CA and, by default, the
# host name (or address); s_client writes stdin to the server only after that handshake. `probe`
# closes at the end of stdin; `session` (-quiet, which ignores EOF) reads replies until the server
# hangs up after QUIT. Either gives up after 20 seconds.
tls_connect() {
  if [ "$1" = session ]; then quiet=-quiet; else quiet=-no_ign_eof; fi
  limit=""
  command -v timeout >/dev/null 2>&1 && limit="timeout 20"
  if [ "$verify_hostname" -eq 0 ]; then
    $limit openssl s_client "$quiet" -connect "$target" -CAfile "$ca" -verify_return_error 2>"$errors"
  elif [ "$ip" -eq 1 ]; then
    $limit openssl s_client "$quiet" -connect "$target" -verify_ip "$host" -CAfile "$ca" -verify_return_error 2>"$errors"
  else
    $limit openssl s_client "$quiet" -connect "$target" -servername "$host" -verify_hostname "$host" -CAfile "$ca" -verify_return_error 2>"$errors"
  fi
}

if [ -n "$ca" ]; then
  command -v openssl >/dev/null 2>&1 || fail "this image has no openssl to verify the certificate of $target; refusing to send the Valkey password"
  if ! tls_connect probe </dev/null >/dev/null; then
    checked="its certificate against secrets/valkey_ca.crt"
    [ "$verify_hostname" -eq 0 ] || checked="$checked for $host"
    fail "cannot open a TLS connection to $target that verifies $checked: $(last_error); refusing to send the Valkey password"
  fi
  if [ "$verify_hostname" -eq 1 ]; then
    ok "TLS to $target, certificate verified against secrets/valkey_ca.crt for $host"
  else
    warn "TLS to $target, certificate chain verified against secrets/valkey_ca.crt but not the host name (EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME=false): another server holding a certificate from that CA would not be detected"
  fi
fi

password=""
if [ -n "${VALKEY_PASSWORD_FILE:-}" ] && [ -r "$VALKEY_PASSWORD_FILE" ]; then
  password="$(cat "$VALKEY_PASSWORD_FILE")"
fi
[ -n "$password" ] || fail "no Valkey password: ${VALKEY_PASSWORD_FILE:-VALKEY_PASSWORD_FILE} is missing or empty (write it to secrets/valkey_password.txt); GrooveMap only uses a Valkey with authentication on"

if [ -n "$ca" ]; then
  # Every reply in order: AUTH, SELECT, PING, then the three INFO bulk strings and QUIT.
  session="$({
    resp AUTH "$password"
    resp SELECT "$db"
    resp PING
    resp INFO server
    resp INFO memory
    resp INFO persistence
    resp QUIT
  } | tls_connect session | tr -d '\r')"
  anonymous="$({
    resp PING
    resp QUIT
  } | tls_connect session | tr -d '\r' | head -n 1)"
  unset password
  replies="$(printf '%s\n' "$session" | grep -E '^[-+]' | head -n 3 | tr '\n' ' ')"
  case "$replies" in
    "+OK +OK +PONG ") ;;
    *) fail "cannot reach or authenticate to Valkey at $where: ${replies:-$(last_error)}" ;;
  esac
  server="$session"
  memory="$session"
  persistence="$session"
else
  REDISCLI_AUTH="$password"
  VALKEYCLI_AUTH="$password"
  export REDISCLI_AUTH VALKEYCLI_AUTH
  unset password
  cli() {
    valkey-cli -h "$VALKEY_HOST" -p "$port" -n "$db" --no-auth-warning "$@" 2>"$errors" | tr -d '\r'
  }
  pong="$(cli PING)"
  [ "$pong" = "PONG" ] || fail "cannot reach or authenticate to Valkey at $where: ${pong:-$(last_error)}"
  anonymous="$( (unset REDISCLI_AUTH VALKEYCLI_AUTH && valkey-cli -h "$VALKEY_HOST" -p "$port" PING) 2>&1 | tr -d '\r')"
  server="$(cli INFO server)"
  memory="$(cli INFO memory)"
  persistence="$(cli INFO persistence)"
fi
if [ -n "$ca" ]; then
  ok "connected to $where over TLS"
else
  ok "connected to $where (no TLS)"
fi

case "$anonymous" in
  *NOAUTH* | *"Authentication required"* | *WRONGPASS* | *"invalid username-password"*) ok "clients without the password are refused" ;;
  PONG | +PONG) fail "Valkey at $where answers clients without a password; GrooveMap only uses a Valkey with authentication on (requirepass or ACL)" ;;
  *) fail "could not tell whether Valkey at $where requires authentication: $anonymous" ;;
esac

valkey_version="$(field "$server" valkey_version)"
redis_version="$(field "$server" redis_version)"
if [ -n "$valkey_version" ]; then
  at_least "$valkey_version" "$MIN_VALKEY" || fail "Valkey $valkey_version at $where; GrooveMap needs Valkey $MIN_VALKEY or newer (tested with 9.1)"
  ok "Valkey $valkey_version"
elif [ -n "$redis_version" ]; then
  at_least "$redis_version" "$MIN_REDIS" || fail "Redis $redis_version at $where; GrooveMap needs Valkey $MIN_VALKEY or newer, or Redis $MIN_REDIS or newer"
  ok "Redis $redis_version"
else
  fail "cannot read INFO server from $where (does an ACL deny INFO?): $(last_error)"
fi

policy="$(field "$memory" maxmemory_policy)"
[ -n "$policy" ] || fail "cannot read maxmemory_policy from $where: $(last_error)"
[ "$policy" = "noeviction" ] || fail "maxmemory-policy is $policy at $where; GrooveMap needs noeviction (it keeps security state that must never be evicted). Do not change a shared instance for GrooveMap: run a dedicated one instead"
ok "maxmemory-policy noeviction"

aof="$(field "$persistence" aof_enabled)"
[ -n "$aof" ] || fail "cannot read aof_enabled from $where: $(last_error)"
[ "$aof" = "1" ] || fail "AOF persistence is off at $where; GrooveMap needs appendonly yes so its state survives a restart"
ok "AOF on"

echo "store-check-valkey: $where is ready for GrooveMap (keys under ${prefix})"
