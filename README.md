# GrooveMap deployment

Private whole-stack deployment configuration for GrooveMap. This repository
owns Compose topology, production hardening, secret-file bootstrap, runtime
configuration promotion, and stack-level validation. Each service's source,
Dockerfile, release version, and image publication remain in its own source
repository.

Current source is licensed under the [MIT License](LICENSE). Historical license
states remain in retained Git history.

## Setup and validation

```bash
mise install
just setup
just check
```

`just check` is credential-free: it validates formatting, image ownership and
digest policy, base and production Compose merges, deployment regression tests,
and secret leaks. `config/validation.env` contains non-published dummy digests
used only for syntax validation.

`just build` validates both Compose configurations without starting containers.

## Configure an environment

1. Copy `.env.example` to untracked `.env`.
2. Replace every image placeholder with an approved GHCR reference containing
   `@sha256:` and the manifest digest. Tags alone are not accepted.
3. For production, run `just secrets-bootstrap` locally. It creates untracked
   `secrets/` files with restrictive permissions and never prints values.
4. Review `docker compose config` or `just config-prod` in full.
5. Start or change an environment only with operator approval.

```bash
just config
just config-prod
just smoke
just smoke-media
```

`just smoke-media` is the end-to-end proof that ADR 0007's canonical `media` block reaches
both stores. It starts a disposable stack under its own Compose project, publishes the
producers' promoted contract fixtures, asserts the block in PostgreSQL and Neo4j, and
destroys the stack and its volumes. See the
[deployment validation guide](docs/testing-guide.md).

The development Compose defaults contain deliberately non-production
credentials. The production overlay replaces credential values with Docker
file secrets and hardens exposed services. Never deploy the base file alone as
a production configuration.

## One-time data migrations

The retained cleanup and backfill scripts default to read-only counts. Each requires an
explicit `--apply` argument before it can mutate data and accepts
`NEO4J_PASSWORD_FILE` ahead of `NEO4J_PASSWORD`:

```bash
scripts/cleanup-implausible-years.sh
scripts/compute-label-stats.sh
scripts/migrate-master-year-to-int.sh
```

Run them only against an approved environment after reviewing the script, current backup,
affected-record count, and rollback procedure. `just check` and CI never invoke them.

## Boundaries

- Internal service images are required, digest-pinned inputs.
- Database, broker, and cache images are pinned to registry manifest digests.
- `catalog-ingestion` owns the editable extraction rules; this repository owns
  the promoted runtime copy and records producer provenance in
  `config/provenance.json`.
- Runtime secrets, `.env`, Docker authentication, volumes, and performance
  results are untracked.
- `catalog-api` owns the performance-runner source and image. This repository owns its
  environment configuration and an explicitly invoked, digest-pinned `just performance`
  recipe; CI never starts it.
- This repository is intentionally unversioned: deployable source repositories
  own artifact versions, while environments record their exact image digests.
- CI performs source/Compose validation only. It neither starts the production
  stack nor reads deployment secrets.

See the [documentation index](docs/README.md) for architecture, configuration,
operations, testing, and troubleshooting guidance.
