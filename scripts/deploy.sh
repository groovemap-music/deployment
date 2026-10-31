#!/usr/bin/env bash
# One command from a fresh Docker host to a fully loaded GrooveMap: `bash scripts/deploy.sh`
# (or `just deploy`, which runs the same script). Needs only Docker, git and python3 >= 3.10.
#
# Phase 1 starts the stack without the musicbrainz profile and waits until the Discogs import
# has finished (scripts/discogs_import_status.py --wait). Phase 2 sets COMPOSE_PROFILES=musicbrainz
# in .env and brings the MusicBrainz path up. Every step is idempotent: re-running after an
# interruption or a reboot resumes the wait, and once the profile is enabled a run only
# reconciles the stack. A deployment that already runs the MusicBrainz path keeps it.
#
# Host-specific settings (reverse-proxy labels and networks, data on another disk, restart
# policies) go in an untracked docker-compose.override.yml. When it exists, every Compose call
# and the gate add it last, after it passes the override check.
#
# The decisions (preflight minimums, Neo4j sizing, the override check, .env edits, endpoints)
# live in scripts/deploy_helpers.py, which runs on the distro's own python3 (stdlib only). This starts containers, so it is operator-run only and never part
# of `just check`.
#
# Exit codes: 0 done (or phase 1 started with --no-wait), 1 a step failed, 2 bad arguments,
# 3 preflight stopped before changing anything, or the override check stopped before any
# container started, 130 interrupted while waiting (the import keeps running in Docker; re-run
# to resume).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The command the user typed, so help, resume and status hints can be pasted back as they are.
# `just deploy` sets DEPLOY_INVOKED_AS; running the script directly leaves it unset.
INVOKED_AS="${DEPLOY_INVOKED_AS:-bash scripts/deploy.sh}"
MIN_PYTHON="3.10"
DEFAULT_INTERVAL=300
OVERRIDE=docker-compose.override.yml
MUSICBRAINZ_SERVICES=(extractor-musicbrainz brainzgraphinator brainztableinator)
MUSICBRAINZ_VOLUMES=(musicbrainz_data extractor_musicbrainz_logs brainzgraphinator_logs brainztableinator_logs)

usage() {
  cat <<EOF
Usage: $INVOKED_AS [options]

Installs GrooveMap on this Docker host, or picks up where an earlier run stopped.
Checks the host, creates .env and secrets/, sizes Neo4j for this machine, starts the
stack, waits for the Discogs import (it takes about two days), then starts MusicBrainz.
Host-specific settings in docker-compose.override.yml, if present, are checked and applied.

Options:
  --dev                 use the base development stack instead of the production overlay
  --no-wait             start phase 1 and exit; re-run later to continue
  --no-admin            do not offer to create the first admin account
  --admin-email EMAIL   create the first admin with this email (asks for the password)
  -p, --project-name N  Compose project name (default: COMPOSE_PROJECT_NAME or this directory)
  --interval SECONDS    seconds between import status checks while waiting (default 300)
  -y, --yes             go ahead under amd64 emulation on an arm64 host without asking
  --force               go ahead even below the memory and disk minimums
  -h, --help            show this help

Exit codes: 0 done, 1 a step failed, 2 bad arguments, 3 stopped by the host or override
checks, 130 interrupted while waiting (the import keeps running; re-run to resume).
EOF
}

usage_error() {
  echo "deploy: $1" >&2
  echo "Run '$INVOKED_AS --help' for the options." >&2
  exit 2
}

mode=prod
wait_for_import=1
offer_admin=1
assume_yes=0
force=0
project=""
admin_email=""
interval=$DEFAULT_INTERVAL

while (($#)); do
  case "$1" in
    --dev) mode=dev ;;
    --no-wait) wait_for_import=0 ;;
    --no-admin) offer_admin=0 ;;
    -y | --yes) assume_yes=1 ;;
    --force) force=1 ;;
    -p | --project-name | --admin-email | --interval)
      (($# >= 2)) && [[ -n "$2" ]] || usage_error "$1 needs a value"
      case "$1" in
        -p | --project-name) project="$2" ;;
        --admin-email) admin_email="$2" ;;
        --interval) interval="$2" ;;
      esac
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) usage_error "unknown option: $1" ;;
  esac
  shift
done
[[ "$interval" =~ ^[1-9][0-9]*$ ]] || usage_error "--interval must be a whole number of seconds above zero"

if [[ -n "${DEPLOY_PYTHON:-}" ]]; then
  read -r -a PYTHON <<<"$DEPLOY_PYTHON"
elif command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run --quiet python)
else
  PYTHON=(python3)
fi

if ! "${PYTHON[@]}" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  found="$("${PYTHON[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
  echo "deploy: Python $MIN_PYTHON or newer is needed (${PYTHON[*]}: ${found:-not found})." >&2
  echo "Install python3 $MIN_PYTHON+ (Debian 12 and Ubuntu 22.04 ship it), or point DEPLOY_PYTHON at one." >&2
  exit 1
fi

helper() { "${PYTHON[@]}" scripts/deploy_helpers.py "$@"; }

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$@"; }
indent() { sed 's/^/    /'; }

# The command that continues from here, with every option that changes what it does.
resume_command() {
  local command=($INVOKED_AS)
  [[ "$mode" == dev ]] && command+=(--dev)
  [[ -n "$project" ]] && command+=(--project-name "$project")
  ((assume_yes)) && command+=(--yes)
  ((force)) && command+=(--force)
  ((offer_admin)) || command+=(--no-admin)
  ((interval == DEFAULT_INTERVAL)) || command+=(--interval "$interval")
  local quoted
  printf -v quoted '%q ' "${command[@]}"
  printf '%s' "${quoted% }"
}

compose=(docker compose)
gate=("${PYTHON[@]}" scripts/discogs_import_status.py)
[[ -n "$project" ]] && compose+=(--project-name "$project") && gate+=(--project-name "$project")
compose+=(-f docker-compose.yml)
if [[ "$mode" == prod ]]; then
  compose+=(-f docker-compose.prod.yml)
  gate+=(--prod)
fi
# Compose reads docker-compose.override.yml by itself only when no -f is given, so name it, last.
stack_without_override=("${compose[@]}")
if [[ -f "$OVERRIDE" ]]; then
  compose+=(-f "$OVERRIDE")
  gate+=(-f "$OVERRIDE")
fi

status_command() {
  local command=("${PYTHON[@]}" scripts/discogs_import_status.py)
  [[ "$INVOKED_AS" == "just deploy" ]] && command=(just discogs-import-status)
  [[ -n "$project" ]] && command+=(-p "$project")
  [[ "$mode" == prod ]] && command+=(--prod)
  [[ -f "$OVERRIDE" ]] && command+=(-f "$OVERRIDE")
  local quoted
  printf -v quoted '%q ' "${command[@]}"
  printf '%s' "${quoted% }"
}

stack_name() { [[ "$mode" == prod ]] && echo "production stack" || echo "development stack"; }

# --- 1. host checks --------------------------------------------------------------------

say "Checking this host for the GrooveMap $(stack_name)"
preflight_args=(--mode "$mode")
((assume_yes)) && preflight_args+=(--yes)
((force)) && preflight_args+=(--force)
status=0
helper preflight "${preflight_args[@]}" || status=$?
if ((status == 3)); then
  exit 3
elif ((status != 0)); then
  echo "deploy: the host checks failed unexpectedly (exit $status)" >&2
  exit 1
fi

# --- 2. configuration ------------------------------------------------------------------

say "Preparing configuration"
if [[ -f .env ]]; then
  note ".env already exists; keeping it."
else
  note "No .env yet: creating one from .env.example with the reviewed image digests."
  "${PYTHON[@]}" scripts/env-init.py | indent
fi
if [[ "$mode" == prod ]]; then
  note "Creating any missing secrets in secrets/ (existing ones are never replaced):"
  bash scripts/create-secrets.sh | indent
  helper configure --mode prod
else
  note "The development stack uses the fixed local credentials and sizes in docker-compose.yml;"
  note "no secrets or sizing to write."
fi

# The override may add host-specific settings, but not swap an image, flip the musicbrainz
# profile, rename a container or publish a data-tier port: compare Compose's renders without and
# with it, every profile included.
check_override() {
  local before after status=0
  before="$(mktemp)"
  after="$(mktemp)"
  if "${stack_without_override[@]}" --profile '*' config --format json >"$before" &&
    "${compose[@]}" --profile '*' config --format json >"$after"; then
    helper override-check "$before" "$after" || status=$?
  else
    echo "deploy: docker compose could not read the stack with $OVERRIDE (see above)" >&2
    status=3
  fi
  rm -f "$before" "$after"
  return "$status"
}

if [[ -f "$OVERRIDE" ]]; then
  say "Checking your host-specific settings in $OVERRIDE"
  status=0
  check_override || status=$?
  if ((status == 3)); then
    exit 3
  elif ((status != 0)); then
    echo "deploy: the override check failed unexpectedly (exit $status)" >&2
    exit 1
  fi
fi

# Free space is judged on the disk behind each data volume, so it waits for the render that
# includes the override (and .env, which Compose needs to render at all). If Compose cannot
# render, every volume is assumed to be on Docker's data root.
disk_check() {
  local rendered args=() status=0
  rendered="$(mktemp)"
  ((force)) && args+=(--force)
  if "${compose[@]}" --profile '*' config --format json >"$rendered"; then
    helper disk-check "$rendered" ${args[@]+"${args[@]}"} || status=$?
  else
    echo "deploy: docker compose could not render the stack; assuming the data is on Docker's data root" >&2
    helper disk-check ${args[@]+"${args[@]}"} || status=$?
  fi
  rm -f "$rendered"
  return "$status"
}

say "Checking free disk space behind the data volumes"
status=0
disk_check || status=$?
if ((status == 3)); then
  exit 3
elif ((status != 0)); then
  echo "deploy: the disk check failed unexpectedly (exit $status)" >&2
  exit 1
fi

# --- 3. where are we? ------------------------------------------------------------------

musicbrainz_present() {
  local filters=() service volume name volumes
  for service in "${MUSICBRAINZ_SERVICES[@]}"; do
    filters+=(--filter "name=^groovemap-${service}$")
  done
  [[ -n "$(docker ps -a -q "${filters[@]}")" ]] && return 0
  name="$(helper project-name ${project:+--project-name "$project"})"
  volumes="$(docker volume ls --filter "label=com.docker.compose.project=$name" --format '{{.Label "com.docker.compose.volume"}}')"
  for volume in "${MUSICBRAINZ_VOLUMES[@]}"; do
    grep -qx "$volume" <<<"$volumes" && return 0
  done
  return 1
}

stack_running() { [[ -n "$(docker ps -q --filter 'name=^groovemap-postgres$')" ]]; }

if helper profile-enabled; then
  phase=reconcile
  reason="MusicBrainz is already enabled in .env (COMPOSE_PROFILES), so this run only brings the stack up to date."
elif musicbrainz_present; then
  phase=enable
  reason="This host already has GrooveMap's MusicBrainz containers or volumes, so MusicBrainz stays on."
elif stack_running && "${gate[@]}" >/dev/null 2>&1; then
  phase=enable
  reason="The Discogs import has already finished, so MusicBrainz goes next."
else
  phase=first-load
  reason=""
fi

# --- 4. the stack ----------------------------------------------------------------------

start_stack() {
  "${compose[@]}" up -d --wait
}

enable_musicbrainz() {
  helper enable-profile
  # The shell environment outranks .env for Compose; keep an exported value in step.
  if [[ -n "${COMPOSE_PROFILES+set}" ]] && ! [[ ",${COMPOSE_PROFILES}," == *",musicbrainz,"* ]]; then
    export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}musicbrainz"
  fi
}

if [[ "$phase" != first-load ]]; then
  say "Picking up where things stand"
  note "$reason"
  [[ "$phase" == enable ]] && enable_musicbrainz
  say "Starting the full GrooveMap stack, MusicBrainz included"
  note "Containers that are already running and unchanged are left alone."
  start_stack
else
  say "Phase 1 of 2: starting GrooveMap without MusicBrainz"
  note "MusicBrainz waits until the Discogs import has finished; the two must not load at once."
  note "The first start downloads several GB of images, which can take a few minutes."
  start_stack

  if ((!wait_for_import)); then
    say "Phase 1 is running"
    note "The Discogs import is under way in Docker. It takes about two days on the reference"
    note "hardware, longer on a smaller or emulated host. To check on it:"
    note "" "  $(status_command)" ""
    note "When you want to continue (any time; it waits if the import is still going):"
    note "" "  $(resume_command)"
  else
    say "Waiting for the Discogs import to finish"
    note "GrooveMap is downloading the monthly Discogs dump (~11 GB) and loading about 34 million"
    note "records into PostgreSQL and Neo4j. On the reference hardware this takes about two days"
    note "(releases alone take ~40 hours); expect longer on a smaller or emulated host."
    note "" "A status line appears every $interval seconds, and MusicBrainz starts on its own once the"
    note "import is done. It is safe to press Ctrl-C or close this terminal: the import keeps"
    note "running in Docker, and running this again picks up where it left off:"
    note "" "  $(resume_command)" ""
    status=0
    "${gate[@]}" --wait --interval "$interval" || status=$?
    if ((status == 130)); then
      say "Stopped waiting; the Discogs import is still running in Docker"
      note "Continue any time with:" "" "  $(resume_command)"
      exit 130
    elif ((status != 0)); then
      echo "deploy: the Discogs import status check failed (exit $status)" >&2
      exit 1
    fi
    say "Phase 2 of 2: the Discogs import has finished, starting MusicBrainz"
    enable_musicbrainz
    start_stack
  fi
fi

# --- 5. endpoints and the first admin --------------------------------------------------

# The console's session cookie is Secure, so over plain http on a LAN host sign-in silently
# fails. Say so before the console URL is printed; on an interactive "yes", recreate only
# `dashboard` with the same project and files as the rest of the run.
console_sign_in() {
  [[ "$mode" == prod ]] || return 0
  local quoted status=0
  printf -v quoted '%q ' "${compose[@]}" up -d --wait --no-deps dashboard
  helper console-http --mode "$mode" --recreate-command "${quoted% }" || status=$?
  if ((status == 10)); then
    say "Recreating the console with the Secure cookie off"
    "${compose[@]}" up -d --wait --no-deps dashboard
  elif ((status != 0)); then
    echo "deploy: the console sign-in check failed (exit $status)" >&2
    exit 1
  fi
}

console_sign_in

say "Your GrooveMap"
helper endpoints --mode "$mode"

admin_command() {
  local quoted
  printf -v quoted '%q ' "${compose[@]}" exec api catalog-admin-setup --email "${1:-you@example.com}"
  printf '%s' "${quoted% }"
}

create_admin() {
  if ((!offer_admin)); then
    return 0
  fi
  if [[ -z "$admin_email" ]]; then
    if "${compose[@]}" exec -T api catalog-admin-setup --list 2>/dev/null | grep -q '@'; then
      note "An admin account already exists. To add another: $(admin_command)"
      return 0
    fi
    if ! [[ -t 0 && -t 1 ]]; then
      note "To create the first admin account (it asks for a password): $(admin_command)"
      return 0
    fi
    read -r -p "    Email for the first admin account (Enter to skip): " admin_email
    if [[ -z "$admin_email" ]]; then
      note "Skipped. Create it later with: $(admin_command)"
      return 0
    fi
  fi
  say "Creating the admin account $admin_email"
  note "Choose a password of at least 8 characters; it is not echoed."
  if ! "${compose[@]}" exec api catalog-admin-setup --email "$admin_email"; then
    note "That did not work. Try again later with: $(admin_command "$admin_email")"
  fi
}

create_admin

if [[ "$phase" == first-load ]] && ((!wait_for_import)); then
  say "Phase 1 started. Re-run '$(resume_command)' to continue to MusicBrainz."
else
  say "GrooveMap is up with Discogs loaded and MusicBrainz enabled. Re-run '$(resume_command)' any time to bring it up to date."
fi
