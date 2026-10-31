set shell := ["bash", "-euo", "pipefail", "-c"]

# Operator recipes (deploy, env-init, discogs-import-status, the embeddings steps, secrets-bootstrap)
# need only Docker, git and python3 >= 3.10; just is the only optional convenience. They run
# stdlib scripts through this runner, which uses uv when present and python3 otherwise, the same
# selection scripts/deploy.sh makes (DEPLOY_PYTHON overrides). Exit codes pass through unchanged.
# Maintainer recipes (setup, check, test, typecheck, audit, smoke-*) require uv and say so.
operator_python := "bash scripts/operator-python.sh"

default:
    @just --list

# Fails with a clear message when uv is missing; maintainer recipes depend on it.
_require-uv:
    @command -v uv >/dev/null 2>&1 || { echo "error: this is a maintainer recipe and needs uv (https://docs.astral.sh/uv/), which is not on PATH." >&2; echo "Operator recipes (deploy, env-init, discogs-import-status) work without it." >&2; exit 1; }

setup: _require-uv
    uv sync --dev --frozen

_source-analysis: _require-uv
    uvx --from ruff==0.16.4 ruff format --check .
    uvx --from ruff==0.16.4 ruff check .
    uv run python scripts/check-images.py
    uv run python scripts/check-dashboards.py
    uv run python scripts/check-rabbitmq-dlq-policy.py

_compose-check: _require-uv
    bash scripts/check-compose.sh

source-check: _source-analysis license-check _compose-check secret-scan

check: source-check typecheck test

typecheck: _require-uv
    uv run mypy

test: _require-uv
    uv run pytest --cov=scripts --cov-report=term-missing --cov-report=xml

coverage: test

audit: _require-uv
    uv run pip-audit

license-check: _require-uv
    uv run python scripts/check-licenses.py
    uv run pip-licenses --fail-on "GPL-2.0-only;GPL-3.0-only;AGPL-3.0-only"

secret-scan:
    gitleaks git --config .gitleaks.toml --redact --no-banner
    gitleaks dir . --config .gitleaks.toml --redact --no-banner

build: _compose-check

install-check: _compose-check

# Requires an approved, published catalog-api performance image and a running environment.
performance:
    bash scripts/run-perftest.sh

config:
    docker compose config

config-prod:
    docker compose -f docker-compose.yml -f docker-compose.prod.yml config

secrets-bootstrap:
    bash scripts/create-secrets.sh

# The one-command install for a homelab: checks the host, writes .env and secrets/, sizes Neo4j
# for this machine, starts the stack without MusicBrainz, waits for the Discogs import, then
# enables the musicbrainz profile. Idempotent: re-run to resume or to bring the stack up to date.
# Starts containers, so it is operator-run only and never part of `check`. `just deploy --help`.
# Without mise or just, the same command is `bash scripts/deploy.sh` on the host's own python3.
[positional-arguments]
deploy *args:
    DEPLOY_INVOKED_AS="just deploy" bash scripts/deploy.sh "$@"

# Writes .env from .env.example, filling every image digest from the reviewed release set in
# scripts/check-images.py. Refuses to replace an existing .env unless --force is given, which
# keeps the old file as a timestamped backup.
env-init *args:
    {{operator_python}} scripts/env-init.py {{args}}

# One-time (per stack), idempotent: grants the embedding pipeline's login
# membership in database-schema's embedding_pipeline role. Run once that role
# exists (PostgreSQL 19 + pgvector — see docs/maintenance.md#monthly-embedding-refresh),
# before the first embeddings-run.
embeddings-provision-login:
    bash scripts/provision-embedding-pipeline-login.sh

# Requires SOURCE_DUMP_ID and SOURCE_DUMP_DATE, and a running dev/CI stack whose
# Discogs consumer queues have drained. See docs/maintenance.md#monthly-embedding-refresh.
embeddings-run:
    bash scripts/run-embeddings.sh

# Requires approved, real digest-pinned image values in .env.
smoke:
    docker compose up -d --wait
    docker compose ps

# Starts a disposable stack under its own project name, publishes the producers' promoted
# contract fixtures, and destroys the stack and its volumes on exit.
# Requires approved, real digest-pinned .env images; asserts the ADR 0007 media block end to end.
smoke-media: _require-uv
    bash scripts/smoke-media.sh

# Starts a disposable stack under its own project name, registers a throwaway account,
# exports it, erases it, and asserts every store is empty of it before destroying the
# stack and its volumes. Requires approved, real digest-pinned .env images; asserts the
# ADR 0010 erasure and export boundary end to end.
smoke-erasure: _require-uv
    bash scripts/smoke-erasure.sh

# Runs the digest-pinned Discogs extractor against the packaged v1 tiny dump and
# asserts its RabbitMQ writes in both databases. Operator-gated and disposable.
smoke-released-fixture: _require-uv
    bash scripts/smoke-released-fixture.sh

# Credential-free infrastructure smoke; uses validation-only service image
# values because Compose resolves all variables before selecting services.
smoke-infra:
    bash scripts/smoke-infra.sh

# Requires a reviewed env file containing approved immutable digests for every
# repository-owned image. Runs only a disposable, non-published stack.
smoke-released:
    bash scripts/smoke-released-stack.sh

# Read-only. Reports whether the Discogs import has finished from the durable completion
# signals in PostgreSQL, Neo4j, and RabbitMQ: exit 0 complete, 3 in progress (normal while the
# import runs, not a failure), 2 cannot determine. --prod addresses the production overlay; --wait [--interval N] polls.
# See docs/maintenance.md#post-import-identity-maintenance.
[positional-arguments]
discogs-import-status *args:
    {{operator_python}} scripts/discogs_import_status.py "$@"

down:
    docker compose down
