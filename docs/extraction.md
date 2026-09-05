# Source extraction

`deployment` was extracted without modifying or deleting content from the
private legacy source repository.

The standalone clone was filtered with `git-filter-repo` to retain root Compose
files, the production secret examples and bootstrap/entrypoint scripts,
deployment tests, the performance environment configuration, and stack-level operations
documents. The filtered history retains the original performance runner, while its
current source and image ownership moved to `catalog-api` to avoid duplicate release
units.
Service source trees and Dockerfiles were intentionally excluded because their
new repositories own builds and image releases.

```bash
: "${LEGACY_SOURCE_REPOSITORY:?Set LEGACY_SOURCE_REPOSITORY to the private source URL}"
: "${LEGACY_SOURCE_BRANCH:?Set LEGACY_SOURCE_BRANCH to the reviewed source branch}"
readonly LEGACY_SOURCE_REPOSITORY LEGACY_SOURCE_BRANCH
git clone --no-local --single-branch --no-tags \
  --branch "${LEGACY_SOURCE_BRANCH}" \
  "${LEGACY_SOURCE_REPOSITORY}" deployment
git filter-repo --force \
  --path docker-compose.yml --path docker-compose.prod.yml \
  --path .dockerignore --path .env.example --path .gitignore \
  --path .yamllint --path LICENSE \
  --path scripts/create-secrets.sh \
  --path scripts/migrate-encryption-key.sh \
  --path scripts/neo4j-entrypoint.sh \
  --path scripts/rabbitmq-entrypoint.sh \
  --path scripts/redis-entrypoint.sh \
  --path scripts/reset-password.sh \
  --path scripts/test-database-resilience.sh \
  --path secrets.example/ --path tests/deploy/ --path tests/perftest/ \
  --path tests/conftest.py \
  --path docs/admin-guide.md --path docs/architecture.md \
  --path docs/configuration.md --path docs/database-resilience.md \
  --path docs/docker-security.md --path docs/dockerfile-standards.md \
  --path docs/maintenance.md --path docs/monitoring.md \
  --path docs/performance-guide.md --path docs/platform-targeting.md \
  --path docs/quick-start.md --path docs/testing-guide.md \
  --path docs/troubleshooting.md --path docs/usage-examples.md \
  --path .github/workflows/docker-compose-validate.yml

```

The filtered source branch contains 288 retained commits before the standalone
establishment commit. `source-main-filtered` preserves the filtered source tip
locally for audit.

A supplemental history-only merge retains the original commits for
`cleanup-implausible-years.sh`, `compute-label-stats.sh`, and
`migrate-master-year-to-int.sh`. Their current versions add read-only defaults, explicit
`--apply` gates, and secret-file support; the merge imports history without replacing the
reviewed current tree.
