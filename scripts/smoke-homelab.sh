#!/usr/bin/env bash
# The homelab-shape smoke (`just smoke-homelab`): lux's installation on real Docker, with the
# negative cases that must fail closed. Mocks and Compose renders missed every failure found
# installing GrooveMap on lux (gm-deployment-zg3x); this starts the real containers instead.
#
# What it builds, on any Linux amd64 Docker host:
#   - a "homelab" PostgreSQL 18 (config/homelab-smoke/compose.yml, its own Compose project and
#     bridge network) with a throwaway CA and a server certificate for homelab-postgres, and
#     GrooveMap's role, database and pg_trgm provisioned by its admin with docs/homelab.md's SQL
#     (non-superuser, 60-connection limits);
#   - GrooveMap from a disposable copy of this checkout: `just env-init`'s .env with the reviewed
#     digests, GROOVEMAP_POSTGRES=external with verify-full TLS, dedicated Valkey, Neo4j and
#     RabbitMQ, an override generated from the skill's external-store example (exactly
#     STORE_CLIENTS on the homelab network), the operator's postgres credentials and CA, and
#     every other secret from create-secrets.sh as the running user, 0600;
#   - `just deploy --no-wait` with uv off PATH, then the Discogs gate (3, then 0 once the real
#     Discogs extractor has run its packaged fixture through the real loaders), the p9mu resume
#     regression, phase 2, and `just status` at each step.
#
# Negative cases, each failing closed with a precise message: the store check off the homelab
# network, a wrong CA, a plaintext sslmode, an override rewiring POSTGRES_HOST (all exit 3 with
# no container started), the valkey exporter as UID 59000 (permission denied), an invalid
# collector configuration (unhealthy through its same-source healthcheck), and a failing
# rabbitmq-dlq-policy-init (deploy fails and nothing that waits on it starts).
#
# Disposable: unique project names, every container, volume and network removed by its
# com.docker.compose.project label on every exit, and nothing of another project touched. It
# refuses to start where groovemap-* containers exist, because the stack's container names are
# fixed. It needs no credentials and no network access beyond pulling the pinned images.
# Starts containers, so it is never part of `just check`; CI runs it in
# .github/workflows/smoke-homelab.yml. See docs/testing-guide.md#the-homelab-shape-smoke.
#
# Run it as the operator: the stack runs as the deploying user (UID/GID in .env) and create-secrets.sh
# writes 0600 files owned by that user. CI runs it as UID 1000, lux's operator UID.
#
# Knobs (environment): SMOKE_HOMELAB_RUN (unique suffix), SMOKE_HOMELAB_SUBNET (stack network,
# default 172.29.0.0/16), SMOKE_HOMELAB_EVIDENCE_DIR (logs and report kept after the run),
# SMOKE_HOMELAB_GATE_TIMEOUT (seconds for the gate to reach 0, default 600).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="${SMOKE_HOMELAB_RUN:-${GITHUB_RUN_ID:-$(date +%s)}-$$}"
PROJECT="gm-homelab-smoke-$(printf '%s' "$RUN" | tr -c 'a-z0-9-' '-')"
HOMELAB_PROJECT="$PROJECT-pg"
NETWORK="$PROJECT-homelab"
MONITORING_NETWORK="$PROJECT-monitoring"
SUBNET="${SMOKE_HOMELAB_SUBNET:-172.29.0.0/16}"
GATE_TIMEOUT="${SMOKE_HOMELAB_GATE_TIMEOUT:-600}"
FIXTURE_MANIFEST=/usr/share/discogs-ingestion/contracts/extractor-smoke/v1/manifest.json
LABEL=com.docker.compose.project
STARTED=$SECONDS

WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gm-homelab-smoke.XXXXXX")"
STACK="$WORK/stack"
TLS="$WORK/tls"
EVIDENCE="${SMOKE_HOMELAB_EVIDENCE_DIR:-$WORK/evidence}"
REPORT="$EVIDENCE/report.txt"
mkdir -p "$EVIDENCE"
: >"$REPORT"

say() { printf '\n==> %s\n' "$*"; }
record() { printf '%s\n' "$*" | tee -a "$REPORT"; }
pass() { record "PASS $*"; }
fail() {
  record "FAIL $*"
  exit 1
}
# A failure that leaves the stack usable for the cases after it; the run still exits 1.
SOFT_FAILURES=0
soft_fail() {
  record "FAIL $1"
  shift
  local line
  for line in "$@"; do record "     $line"; done
  SOFT_FAILURES=$((SOFT_FAILURES + 1))
}
die() {
  echo "smoke-homelab: $*" >&2
  exit 2
}

helper() { (cd "$ROOT" && uv run --quiet python scripts/smoke_homelab.py "$@"); }
containers() { docker ps -aq --filter "label=$LABEL=$1"; }

# --- teardown --------------------------------------------------------------------------

remove_project() {
  local ids
  mapfile -t ids < <(containers "$1")
  ((${#ids[@]} == 0)) || docker rm -f -v "${ids[@]}" >/dev/null
}

remove_project_resources() {
  local ids
  mapfile -t ids < <(docker network ls -q --filter "label=$LABEL=$1")
  ((${#ids[@]} == 0)) || docker network rm "${ids[@]}" >/dev/null
  mapfile -t ids < <(docker volume ls -q --filter "label=$LABEL=$1")
  ((${#ids[@]} == 0)) || docker volume rm -f "${ids[@]}" >/dev/null
}

diagnose() {
  local project name
  mkdir -p "$EVIDENCE/logs"
  for project in "$PROJECT" "$HOMELAB_PROJECT"; do
    docker ps -a --filter "label=$LABEL=$project" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' >&2 || true
    for name in $(docker ps -a --filter "label=$LABEL=$project" --format '{{.Names}}'); do
      docker logs --timestamps --tail 300 "$name" >"$EVIDENCE/logs/$name.log" 2>&1 || true
      echo "--- $name (last 40 lines; full tail in the evidence logs)" >&2
      tail -n 40 "$EVIDENCE/logs/$name.log" >&2 || true
    done
  done
}

cleanup() {
  local status=$?
  set +e
  ((status == 0)) || diagnose
  say "Removing every container, volume and network labelled $LABEL=$PROJECT or $HOMELAB_PROJECT"
  remove_project "$PROJECT"
  remove_project "$HOMELAB_PROJECT"
  remove_project_resources "$PROJECT"
  remove_project_resources "$HOMELAB_PROJECT"
  record "runtime: $((SECONDS - STARTED)) s"
  [[ -z "$(containers "$PROJECT")$(containers "$HOMELAB_PROJECT")" ]] || echo "smoke-homelab: some containers could not be removed" >&2
  chmod -R u+rwX "$WORK" 2>/dev/null
  rm -rf "${WORK:?}/stack" "${WORK:?}/tls"
  [[ "$EVIDENCE" == "$WORK"/* ]] || rm -rf "$WORK"
  ((status == 0)) && echo "smoke-homelab: passed; report: $REPORT" || echo "smoke-homelab: FAILED (exit $status); report: $REPORT" >&2
  exit "$status"
}

# --- preconditions ---------------------------------------------------------------------

[[ "$(uname -s)" == Linux ]] || die "needs a Linux Docker host (bind mounts and UIDs must be the host's own); CI runs it on ubuntu-24.04"
for tool in docker git python3 openssl just uv tar; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
done
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
arch="$(docker info --format '{{.Architecture}}')"
[[ "$arch" == x86_64 ]] || die "the GrooveMap images are linux/amd64 only and this engine is $arch; run it on an amd64 host"
existing="$(docker ps -a --filter name=^groovemap- --format '{{.Names}} (project {{.Label "com.docker.compose.project"}})')"
if [[ -n "$existing" ]]; then
  die "this Docker host already has groovemap-* containers, and the stack's container names are fixed, so the smoke would collide with them:
$existing
Remove that stack first (docker compose -p <project> down), or run the smoke on another host. The smoke never touches another project."
fi
trap cleanup EXIT

say "Run $PROJECT in $WORK"
record "project: $PROJECT as $(id -un) ($(id -u):$(id -g)), Compose $(docker compose version --short), engine $(docker version --format '{{.Server.Version}}')"

# --- a disposable checkout -------------------------------------------------------------

# Tracked and untracked-but-not-ignored files only: never this checkout's .env, secrets/ or override.
mkdir -p "$STACK" "$TLS/server"
while IFS= read -r -d '' path; do
  if [[ -e "$ROOT/$path" ]]; then printf '%s\0' "$path"; fi
done < <(git -C "$ROOT" ls-files -z --cached --others --exclude-standard) | tar -C "$ROOT" --null -T - -cf - | tar -C "$STACK" -xf -
rm -rf "$STACK/.env" "$STACK/secrets" "$STACK/docker-compose.override.yml"

# --- the homelab PostgreSQL ------------------------------------------------------------

say "Starting the homelab: PostgreSQL 18 with TLS on $NETWORK, shared monitoring on $MONITORING_NETWORK"
(
  umask 077
  for ca in smoke-ca wrong-ca; do
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$ca" -keyout "$TLS/$ca.key" -out "$TLS/$ca.crt" 2>/dev/null
  done
  openssl req -newkey rsa:2048 -nodes -subj "/CN=homelab-postgres" -keyout "$TLS/server/server.key" -out "$TLS/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:homelab-postgres\nextendedKeyUsage=serverAuth\n' >"$TLS/server.ext"
  openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/smoke-ca.crt" -CAkey "$TLS/smoke-ca.key" -CAcreateserial -days 2 \
    -extfile "$TLS/server.ext" -out "$TLS/server/server.crt" 2>/dev/null
  python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(32)), end="")' >"$TLS/admin_password"
  python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(32)), end="")' >"$TLS/groovemap_password"
)
homelab=(
  env "SMOKE_HOMELAB_TLS_DIR=$TLS/server" "SMOKE_HOMELAB_ADMIN_PASSWORD_FILE=$TLS/admin_password"
  "SMOKE_HOMELAB_NETWORK=$NETWORK" "SMOKE_HOMELAB_MONITORING_NETWORK=$MONITORING_NETWORK"
  docker compose --project-name "$HOMELAB_PROJECT" -f "$STACK/config/homelab-smoke/compose.yml"
)
"${homelab[@]}" up -d --wait --quiet-pull
# The admin provisions GrooveMap's role, database and pg_trgm; the password reaches psql on stdin.
helper provision-sql --doc "$STACK/docs/homelab.md" "$TLS/groovemap_password" |
  "${homelab[@]}" exec -T homelab-postgres psql -X -q -v ON_ERROR_STOP=1 -U homelab_admin -d postgres >"$EVIDENCE/provision.log" 2>&1 ||
  fail "provisioning with docs/homelab.md's SQL (see $EVIDENCE/provision.log)"
pass "homelab PostgreSQL provisioned with docs/homelab.md's SQL: non-superuser role, own database, pg_trgm, 60-connection limits"

# --- the operator's configuration ------------------------------------------------------

uid="$(id -u)"
gid="$(id -g)"
cd "$STACK"
set_env() {
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >>.env
  fi
}
python3 scripts/env-init.py >"$EVIDENCE/env-init.log"
printf '\n# Homelab smoke (scripts/smoke-homelab.sh)\n' >>.env
set_env COMPOSE_PROJECT_NAME "$PROJECT"
set_env UID "$uid"
set_env GID "$gid"
set_env WEB_BIND_ADDRESS 127.0.0.1
set_env DATA_BIND_ADDRESS 127.0.0.1
set_env GROOVEMAP_POSTGRES external
set_env EXTERNAL_POSTGRES_HOST homelab-postgres
set_env EXTERNAL_POSTGRES_DATABASE groovemap
set_env EXTERNAL_POSTGRES_SSLMODE verify-full
# What the operator writes before deploying (docs/homelab.md, Procedure step 2).
(
  umask 077
  mkdir -p secrets
  printf 'groovemap' >secrets/postgres_username.txt
  cp "$TLS/groovemap_password" secrets/postgres_password.txt
  cp "$TLS/smoke-ca.crt" secrets/postgres_ca.crt
)

# Shared monitoring adds the second skill example's attachments once the lux shape switches to it.
MONITORING_ARGS=()
write_override() {
  helper override "$STACK/docker-compose.override.yml" --network "$NETWORK" --subnet "$SUBNET" \
    --asset "$STACK/skills/groovemap-homelab/assets/override-external-store-network.yml" \
    --monitoring-asset "$STACK/skills/groovemap-homelab/assets/override-shared-monitoring-network.yml" \
    ${MONITORING_ARGS[@]+"${MONITORING_ARGS[@]}"} "$@"
}
write_override
cp docker-compose.override.yml "$EVIDENCE/docker-compose.override.yml"

# Operator recipes need only Docker, git and python3: run them with uv off PATH. Directories that
# hold uv are replaced by a shadow of symlinks to everything else in them.
no_uv_path() {
  local dir entry shadow out=() dirs
  IFS=: read -r -a dirs <<<"$PATH"
  for dir in "${dirs[@]}"; do
    if [[ -e "$dir/uv" || -e "$dir/uvx" ]]; then
      shadow="$WORK/no-uv/$(printf '%s' "$dir" | cksum | cut -d' ' -f1)"
      mkdir -p "$shadow"
      for entry in "$dir"/*; do
        case "${entry##*/}" in uv | uvx) ;; *) ln -sf "$entry" "$shadow/" ;; esac
      done
      out+=("$shadow")
    else
      out+=("$dir")
    fi
  done
  (
    IFS=:
    printf '%s' "${out[*]}"
  )
}
NO_UV_PATH="$(no_uv_path)"
if env PATH="$NO_UV_PATH" bash -c 'command -v uv' >/dev/null 2>&1; then
  die "could not take uv off PATH"
fi
for tool in just docker python3 git; do
  env PATH="$NO_UV_PATH" bash -c "command -v $tool" >/dev/null || die "$tool is not on PATH without uv"
done
operator() { env -u COMPOSE_FILE -u COMPOSE_PROFILES PATH="$NO_UV_PATH" "$@" </dev/null; }

# `just deploy` exactly as an operator runs it. --force: a CI runner cannot meet the production
# memory and disk minimums for a full import (16 GB, ~52 GB), and the fixture needs neither.
deploy() {
  local name=$1 status=0
  shift
  say "just deploy --no-wait ($name)"
  operator "$@" just deploy --no-wait --no-admin --force >"$EVIDENCE/deploy-$name.log" 2>&1 || status=$?
  sed 's/^/    /' "$EVIDENCE/deploy-$name.log" | tail -n 25
  return "$status"
}

nothing_started() { [[ -z "$(containers "$PROJECT")" ]]; }

# A case that deploy must refuse with exit 3, a precise message, and no container started.
expect_refusal() {
  local name=$1 text=$2 status=0
  shift 2
  deploy "$name" "$@" || status=$?
  ((status == 3)) || fail "$name: deploy exited $status, not 3 (see deploy-$name.log)"
  grep -qF -- "$text" "$EVIDENCE/deploy-$name.log" || fail "$name: deploy's output lacks \"$text\" (see deploy-$name.log)"
  nothing_started || fail "$name: containers exist after the refusal: $(docker ps -a --filter "label=$LABEL=$PROJECT" --format '{{.Names}}' | tr '\n' ' ')"
  pass "$name: exit 3, \"$text\", no container started"
}

# --- negative cases before anything starts ---------------------------------------------

say "Negative cases that must stop deploy before any container starts"
expect_refusal plaintext-sslmode \
  "EXTERNAL_POSTGRES_SSLMODE=disable sends credentials and data to homelab-postgres in the clear" \
  EXTERNAL_POSTGRES_SSLMODE=disable
! grep -qF "EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true):" "$EVIDENCE/deploy-plaintext-sslmode.log" ||
  fail "plaintext-sslmode: deploy warns as if EXTERNAL_POSTGRES_ALLOW_PLAINTEXT were set"

write_override --omit store-check-postgres
expect_refusal store-check-off-network \
  "PostgreSQL host homelab-postgres did not resolve or accept a connection from store-check-postgres"
write_override

cp "$TLS/wrong-ca.crt" secrets/postgres_ca.crt
expect_refusal wrong-ca "certificate verify failed"
cp "$TLS/smoke-ca.crt" secrets/postgres_ca.crt

write_override --set-env api=POSTGRES_HOST=homelab-postgres-elsewhere
expect_refusal override-rewires-postgres-host "docker-compose.override.yml changes POSTGRES_HOST on api"
write_override

# --- phase 1 ---------------------------------------------------------------------------

compose() { env -u COMPOSE_FILE -u COMPOSE_PROFILES docker compose "$@"; }
inspect_stack() {
  local ids
  mapfile -t ids < <(containers "$PROJECT")
  if ((${#ids[@]})); then
    docker inspect "${ids[@]}" >"$EVIDENCE/inspect.json"
  else
    echo '[]' >"$EVIDENCE/inspect.json"
  fi
}
check_stack() {
  local name=$1
  # A bare `docker compose` reads COMPOSE_FILE, which deploy keeps in .env: the same files as deploy.
  compose config --format json >"$EVIDENCE/rendered-$name.json"
  inspect_stack
  local monitoring=() clients="exactly STORE_CLIENTS on $NETWORK"
  if ((${#MONITORING_ARGS[@]})); then
    monitoring=(--monitoring-network "$MONITORING_NETWORK")
    clients="$clients and MONITORING_CLIENTS on $MONITORING_NETWORK"
  fi
  helper check-stack --rendered "$EVIDENCE/rendered-$name.json" --inspect "$EVIDENCE/inspect.json" --user "$uid:$gid" \
    --homelab-network "$NETWORK" ${monitoring[@]+"${monitoring[@]}"} >"$EVIDENCE/check-stack-$name.log" || {
    cat "$EVIDENCE/check-stack-$name.log"
    fail "$name: the running stack differs from its render (see check-stack-$name.log)"
  }
  pass "$name: every service up as rendered: one-shots completed, healthchecks healthy, read-only roots and users as configured, $clients ($(grep -c '^ok' "$EVIDENCE/check-stack-$name.log") checks)"
}
# The gate as an operator runs it, bounded by `seconds` (timeout's 124 when it runs out).
gate() {
  local name=$1 seconds=$2 status=0
  shift 2
  operator timeout "$seconds" just discogs-import-status --prod -f docker-compose.override.yml "$@" >"$EVIDENCE/gate-$name.log" 2>&1 || status=$?
  return "$status"
}
status_report() {
  local name=$1 status=0
  operator just status -f docker-compose.override.yml >"$EVIDENCE/status-$name.log" 2>&1 || status=$?
  ((status == 0)) || fail "just status ($name) exited $status (see status-$name.log)"
  shift
  local text
  for text in "$@"; do
    grep -qF -- "$text" "$EVIDENCE/status-$name.log" || fail "just status ($name) lacks \"$text\" (see status-$name.log)"
  done
  ! grep -q '^Attention:' "$EVIDENCE/status-$name.log" || fail "just status ($name) flags a problem: $(grep '^Attention:' "$EVIDENCE/status-$name.log")"
  pass "just status ($name, uv off PATH): exit 0, $(printf '"%s" ' "$@")and nothing flagged"
}
musicbrainz_containers() {
  docker ps -a --filter "label=$LABEL=$PROJECT" --format '{{.Label "com.docker.compose.service"}}' | grep -E '^(extractor-musicbrainz|brainzgraphinator|brainztableinator)$' || true
}

# --- the default monitoring ------------------------------------------------------------

# GrooveMap's own Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor, as every
# installation starts: Grafana and valkey-exporter read their 0600 secrets as the deploying user.
say "Phase 1 with the stack's own monitoring"
status=0
deploy local-monitoring || status=$?
((status == 0)) || fail "local monitoring: deploy exited $status (see deploy-local-monitoring.log)"
grep -qF "Phase 1 is running" "$EVIDENCE/deploy-local-monitoring.log" || fail "local monitoring: deploy did not report phase 1 running"
pass "local monitoring: just deploy --no-wait reached up --wait success with uv off PATH"
check_stack local-monitoring
helper check-secrets "$STACK/secrets" --uid "$uid" >"$EVIDENCE/check-secrets.log" || {
  cat "$EVIDENCE/check-secrets.log"
  fail "secrets/ ownership or modes (see check-secrets.log)"
}
pass "secrets/: 0700, every file 0600 and owned by UID $uid ($(grep -c '^ok' "$EVIDENCE/check-secrets.log") checks)"
metrics="$(compose exec -T api python -c 'import urllib.request; print(urllib.request.urlopen("http://valkey-exporter:9121/metrics", timeout=10).read().decode())')" ||
  fail "could not read valkey-exporter's metrics from the api container"
grep -qx 'redis_up 1' <<<"$metrics" || fail "valkey-exporter reports redis_up $(grep '^redis_up' <<<"$metrics" || echo missing)"
pass "valkey-exporter as $uid:$gid read its 0600 password file and authenticated (redis_up 1); Grafana healthy as $uid:$gid on a fresh volume"
remove_project "$PROJECT"
remove_project_resources "$PROJECT"

# --- phase 1, lux's shape --------------------------------------------------------------

say "Phase 1 in lux's shape: shared monitoring as well as the shared PostgreSQL"
set_env GROOVEMAP_MONITORING shared
set_env SHARED_VICTORIA_METRICS_URL http://homelab-victoria-metrics:8428
set_env SHARED_VICTORIA_TRACES_URL http://homelab-victoria-traces:10428
set_env SHARED_GRAFANA_URL http://homelab-grafana:3000
set_env SHARED_NODE_EXPORTER_ADDRESS homelab-node-exporter:9100
set_env SHARED_CADVISOR_ADDRESS homelab-cadvisor:8080
set_env EXTERNAL_POSTGRES_EXPORTER_ADDRESS homelab-postgres-exporter:9187
MONITORING_ARGS=(--monitoring-network "$MONITORING_NETWORK")
write_override
cp docker-compose.override.yml "$EVIDENCE/docker-compose.override.yml"
status=0
deploy phase-1 || status=$?
((status == 0)) || fail "phase 1: deploy exited $status (see deploy-phase-1.log)"
grep -qF "Phase 1 is running" "$EVIDENCE/deploy-phase-1.log" || fail "phase 1: deploy did not report phase 1 running"
pass "phase 1: just deploy --no-wait reached up --wait success with uv off PATH"
grep -q '^COMPOSE_FILE=.*docker-compose.shared-monitoring.yml.*docker-compose.override.yml' .env ||
  fail "deploy did not keep COMPOSE_FILE in .env with the shared-monitoring overlay and the override"
check_stack phase-1

# The collector scrapes RabbitMQ and the homelab's PostgreSQL exporter and remote-writes to the
# shared VictoriaMetrics; read it back from the console's container, which is on that network.
deadline=$((SECONDS + 180))
query='import urllib.request; print(urllib.request.urlopen("http://homelab-victoria-metrics:8428/api/v1/query?query=up", timeout=10).read().decode())'
until compose exec -T dashboard python -c "$query" >"$EVIDENCE/shared-up.json" 2>&1 &&
  helper check-up "$EVIDENCE/shared-up.json" --job rabbitmq --job postgres-exporter >"$EVIDENCE/check-shared-up.log" 2>&1; do
  ((SECONDS < deadline)) || {
    cat "$EVIDENCE/check-shared-up.log"
    fail "the shared VictoriaMetrics has no up=1 series for rabbitmq and the shared postgres-exporter (see shared-up.json)"
  }
  sleep 10
done
pass "shared monitoring: the collector scraped RabbitMQ and the homelab's PostgreSQL exporter and wrote both to the shared VictoriaMetrics (up=1)"

# What the console shows on its System Health page, from its own probe code in its own container.
if compose exec -T dashboard python -c "$(helper console-probe)" >"$EVIDENCE/console-health.json" 2>"$EVIDENCE/console-health.err" &&
  helper check-console "$EVIDENCE/console-health.json" >"$EVIDENCE/check-console.log" 2>&1; then
  pass "console health reflects reality: every probe healthy, the shared services probed as shared ($(grep -c '^ok' "$EVIDENCE/check-console.log") checks)"
else
  mapfile -t reasons < <(cat "$EVIDENCE/console-health.err" 2>/dev/null; grep '^FAIL' "$EVIDENCE/check-console.log" 2>/dev/null | sed 's/^FAIL //')
  soft_fail "the console's health does not reflect the shared monitoring (see check-console.log, console-health.json)" \
    ${reasons[@]+"${reasons[@]:0:10}"}
fi

# p9mu: phase 1 itself creates musicbrainz_data (the API mounts it read-only), so that volume is no
# evidence MusicBrainz ran, and resuming must consult the gate instead of enabling MusicBrainz.
docker volume ls -q --filter "label=$LABEL=$PROJECT" --filter "label=com.docker.compose.volume=musicbrainz_data" | grep -q . ||
  fail "p9mu: musicbrainz_data does not exist after phase 1, so the regression is not exercised"
[[ -z "$(musicbrainz_containers)" ]] || fail "p9mu: MusicBrainz containers exist after phase 1: $(musicbrainz_containers | tr '\n' ' ')"
! grep -q '^COMPOSE_PROFILES=.*musicbrainz' .env || fail "p9mu: phase 1 enabled the musicbrainz profile"
pass "p9mu: after phase 1 musicbrainz_data exists, no MusicBrainz container exists, the profile is off"

say "The Discogs gate before any extraction"
# The consumers declare their queues as they start; until then the gate cannot determine (2).
deadline=$((SECONDS + 180))
while :; do
  status=0
  gate before 120 || status=$?
  ((status == 2 && SECONDS < deadline)) || break
  sleep 5
done
((status == 3)) || fail "gate before any extraction exited $status, not 3 (see gate-before.log)"
grep -qF "no loader='discogs' row yet" "$EVIDENCE/gate-before.log" || fail "gate exit 3 without its reason (see gate-before.log)"
pass "gate (uv off PATH): exit 3, \"no loader='discogs' row yet\""
status_report phase-1 "phase 1: importing Discogs; MusicBrainz waits on the gate" "Discogs gate: IN PROGRESS (exit 3)" \
  "Exit 3 is normal progress" "waiting-on-gate" "external" "Monitoring   shared (.env)"
operator just recovery-manifest >"$EVIDENCE/recovery-manifest.log" 2>&1 || fail "just recovery-manifest failed (see recovery-manifest.log)"
pass "just recovery-manifest (uv off PATH): exit 0"

say "Resuming while the gate reports 3 (p9mu)"
status=0
deploy resume || status=$?
((status == 0)) || fail "resume: deploy exited $status (see deploy-resume.log)"
grep -qF "Phase 1 of 2" "$EVIDENCE/deploy-resume.log" || fail "resume: deploy did not stay in phase 1"
if grep -qE "MusicBrainz stays on|MusicBrainz goes next|MusicBrainz is already enabled" "$EVIDENCE/deploy-resume.log"; then
  fail "resume: deploy enabled MusicBrainz without the gate (see deploy-resume.log)"
fi
[[ -z "$(musicbrainz_containers)" ]] || fail "resume: MusicBrainz containers started: $(musicbrainz_containers | tr '\n' ' ')"
pass "p9mu: resume consulted the gate (3) and stayed in phase 1; no MusicBrainz container, though musicbrainz_data exists"

# --- negative cases on the running stack -----------------------------------------------

say "Negative case: valkey-exporter as UID 59000 (lux's redis_exporter default) cannot read the 0600 secret"
status=0
timeout 120 env -u COMPOSE_FILE -u COMPOSE_PROFILES docker compose run --rm --no-deps -T --user 59000:59000 valkey-exporter \
  >"$EVIDENCE/exporter-59000.log" 2>&1 || status=$?
((status != 0)) || fail "valkey-exporter as 59000 kept running (see exporter-59000.log)"
grep -qi "permission denied" "$EVIDENCE/exporter-59000.log" || fail "valkey-exporter as 59000 failed without \"permission denied\" (see exporter-59000.log)"
pass "secret owner mismatch: valkey-exporter as 59000 refused, exit $status, \"permission denied\""

say "Negative case: an invalid collector configuration turns the collector unhealthy"
collector="$(compose ps -q otel-collector)"
health() { docker inspect --format '{{.State.Health.Status}}' "$collector"; }
cp config/otel-collector.yaml "$WORK/otel-collector.yaml"
# In place, so the bind mount (one file) sees it: the running process keeps its loaded config,
# and only a healthcheck that validates that same source can notice.
printf '\nnot_a_collector_section: {}\n' >>config/otel-collector.yaml
deadline=$((SECONDS + 240))
until [[ "$(health)" == unhealthy ]] || ((SECONDS > deadline)); do sleep 5; done
docker inspect --format '{{json .State.Health}}' "$collector" >"$EVIDENCE/collector-health.json"
[[ "$(health)" == unhealthy ]] || fail "the collector stayed healthy with an invalid configuration"
grep -qF "not_a_collector_section" "$EVIDENCE/collector-health.json" || fail "the collector's healthcheck failed without naming the invalid key (see collector-health.json)"
cat "$WORK/otel-collector.yaml" >config/otel-collector.yaml
pass "invalid collector config: unhealthy through the healthcheck that validates the command's own --config source (\"not_a_collector_section\")"
deadline=$((SECONDS + 120))
until [[ "$(health)" == healthy ]] || ((SECONDS > deadline)); do sleep 5; done
[[ "$(health)" == healthy ]] || fail "the collector did not recover once its configuration was restored"

# --- the Discogs fixture ---------------------------------------------------------------

# The released extractor image packages a one-record Discogs dump. Run once, it publishes the
# record and extraction_complete to all four exchanges; the real loaders record their latches and
# the SQL loader runs its derived-relation pass. No latch row is written by the smoke itself.
say "Running the reviewed Discogs extractor once against its packaged fixture"
status=0
timeout 600 env -u COMPOSE_FILE -u COMPOSE_PROFILES docker compose run --rm --no-deps -T --entrypoint discogs-ingestion \
  -e LOCAL_MANIFEST="$FIXTURE_MANIFEST" extractor-discogs >"$EVIDENCE/fixture.log" 2>&1 || status=$?
((status == 0)) || fail "the Discogs fixture extraction exited $status (see fixture.log)"
grep -qF "Extraction complete message sent to all 4 exchanges" "$EVIDENCE/fixture.log" ||
  fail "the fixture extraction did not broadcast extraction_complete (see fixture.log)"
pass "fixture: the reviewed extractor published its packaged dump and extraction_complete to all four exchanges"

# A gate that never completes blocks phase 2 but not the remaining negative case: it is recorded,
# phase 2 is skipped, and the run still fails at the end.
status=0
gate complete "$GATE_TIMEOUT" --wait --interval 10 || status=$?
if ((status != 0)); then
  received="$(docker logs groovemap-tableinator 2>&1 | grep -c 'Received extraction_complete' || true)"
  soft_fail "gate did not reach 0 within ${GATE_TIMEOUT}s (exit $status). Last poll: $(grep -E 'in progress|cannot determine' "$EVIDENCE/gate-complete.log" | tail -n 1)." \
    "tableinator logged 'Received extraction_complete' $received time(s); a postgres latch with no loader='discogs' row after that means the SQL loader image writes no loader_extraction_latch (discogs-sql-loader 0.3.0 predates tableinator/extraction_latch.py)"
  record "SKIP phase 2: it needs the gate at 0"
else
  pass "gate (uv off PATH): exit 0 once both loaders signalled all four types and the refresh was stamped"
  status_report gate-complete "Discogs gate: COMPLETE (exit 0)" "phase 1 finished"

  # --- phase 2 -------------------------------------------------------------------------

  say "Phase 2: MusicBrainz goes next"
  status=0
  deploy phase-2 || status=$?
  ((status == 0)) || fail "phase 2: deploy exited $status (see deploy-phase-2.log)"
  grep -qF "The Discogs import has already finished, so MusicBrainz goes next." "$EVIDENCE/deploy-phase-2.log" ||
    fail "phase 2: deploy did not enable MusicBrainz from the gate"
  grep -q '^COMPOSE_PROFILES=musicbrainz' .env || fail "phase 2: COMPOSE_PROFILES=musicbrainz is not in .env"
  pass "phase 2: deploy read the gate (0), enabled the musicbrainz profile in .env and reached up --wait success"
  check_stack phase-2
  status_report phase-2 "phase 2: Discogs import complete, MusicBrainz enabled"
fi

# --- a failing one-shot ----------------------------------------------------------------

say "Negative case: a failing rabbitmq-dlq-policy-init stops everything that waits on it"
remove_project "$PROJECT"
remove_project_resources "$PROJECT"
write_override --set-env rabbitmq-dlq-policy-init=RABBITMQ_DLQ_OVERFLOW=not-an-overflow-mode
status=0
deploy dlq-init-fails || status=$?
((status != 0)) || fail "deploy succeeded with a failing rabbitmq-dlq-policy-init"
grep -qF 'rabbitmq-dlq-policy-init' "$EVIDENCE/deploy-dlq-init-fails.log" || fail "deploy's failure does not name rabbitmq-dlq-policy-init (see deploy-dlq-init-fails.log)"
compose config --format json >"$EVIDENCE/rendered-dlq-init-fails.json"
inspect_stack
helper check-blocked --rendered "$EVIDENCE/rendered-dlq-init-fails.json" --inspect "$EVIDENCE/inspect.json" \
  --failed rabbitmq-dlq-policy-init >"$EVIDENCE/check-blocked.log" || {
  cat "$EVIDENCE/check-blocked.log"
  fail "something that waits on rabbitmq-dlq-policy-init started (see check-blocked.log)"
}
pass "failing one-shot: deploy exited $status naming rabbitmq-dlq-policy-init; it exited non-zero and none of its $(grep -c '^ok' "$EVIDENCE/check-blocked.log") dependents started"

if ((SOFT_FAILURES)); then
  echo "smoke-homelab: $SOFT_FAILURES case(s) failed; every other case ran (see the report)" >&2
  exit 1
fi
say "All cases passed"
