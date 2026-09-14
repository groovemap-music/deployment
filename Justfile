set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --dev --frozen

_source-analysis:
    uvx --from ruff==0.16.4 ruff format --check .
    uvx --from ruff==0.16.4 ruff check .
    uv run python scripts/check-images.py
    uv run python scripts/check-dashboards.py

_compose-check:
    bash scripts/check-compose.sh

source-check: _source-analysis license-check _compose-check secret-scan

check: source-check typecheck test

typecheck:
    uv run mypy

test:
    uv run pytest --cov=scripts --cov-report=term-missing --cov-report=xml

coverage: test

audit:
    uv run pip-audit

license-check:
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

# Requires approved, real digest-pinned image values in .env.
smoke:
    docker compose up -d --wait
    docker compose ps

# Starts a disposable stack under its own project name, publishes the producers' promoted
# contract fixtures, and destroys the stack and its volumes on exit.
# Requires approved, real digest-pinned .env images; asserts the ADR 0007 media block end to end.
smoke-media:
    bash scripts/smoke-media.sh

# Runs the digest-pinned Discogs extractor against the packaged v1 tiny dump and
# asserts its RabbitMQ writes in both databases. Operator-gated and disposable.
smoke-released-fixture:
    bash scripts/smoke-released-fixture.sh

# Credential-free infrastructure smoke; uses validation-only service image
# values because Compose resolves all variables before selecting services.
smoke-infra:
    bash scripts/smoke-infra.sh

# Requires a reviewed env file containing approved immutable digests for every
# repository-owned image. Runs only a disposable, non-published stack.
smoke-released:
    bash scripts/smoke-released-stack.sh

down:
    docker compose down
