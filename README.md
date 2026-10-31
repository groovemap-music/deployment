# GrooveMap deployment

Public whole-stack deployment configuration for GrooveMap. This repository
owns Compose topology, production hardening, secret-file bootstrap, runtime
configuration promotion, and stack-level validation. Each service's source,
Dockerfile, release version, and image publication remain in its own source
repository. Environment-specific operational values and secrets remain private
and untracked.

Current source is licensed under the [MIT License](LICENSE). Historical license
states remain in retained Git history.

## Run GrooveMap in your homelab

GrooveMap is a self-hosted music knowledge graph built from the Discogs and MusicBrainz
datasets. On a Docker host with about 16 GB of memory and 100 GB of free disk (200 GB
recommended):

```bash
git clone https://github.com/groovemap-music/deployment.git
cd deployment
mise install
just deploy
```

`just deploy` checks the host, creates `.env` and `secrets/`, starts the stack, waits for the
Discogs import (hours to about two days), then starts MusicBrainz and prints your URLs. It is
safe to interrupt and re-run. The images are `linux/amd64` only, and the operations console
needs HTTPS in front of it to sign in.

Prefer to have an AI agent fit GrooveMap into your reverse proxy, storage and ports? In
Claude Code, run `/plugin marketplace add groovemap-music/deployment` and then
`/plugin install groovemap-homelab@groovemap`. Other
[agentskills.io](https://agentskills.io)-compatible agents can use `skills/groovemap-homelab`.

Read the [homelab guide](docs/homelab.md) for sizing, ports, reverse proxies, upgrades and
known limitations. Already running GrooveMap? See
[Upgrading an existing deployment](docs/homelab.md#upgrading-an-existing-deployment): MusicBrainz
is now behind a Compose profile.

The rest of this README is for maintainers of this repository.

## Setup and validation

```bash
mise install
just setup
just check
```

`just check` is credential-free: it validates formatting, image ownership and
digest policy, base and production Compose merges, deployment regression tests,
and secret leaks. `config/validation.env` supplies syntax-only digest
references, which identify no published image, so static Compose validation
runs without real images or a running stack. The reviewed release digests are
recorded in `RELEASED_IMAGE_DIGESTS` in `scripts/check-images.py`.

`just build` validates the base, production, infrastructure-smoke,
media-smoke, released-fixture-smoke, and erasure-smoke Compose combinations
without starting containers.
The [quick start](docs/quick-start.md) explains the complete local workflow.

## Configure an environment

1. Copy `.env.example` to untracked `.env`.
2. Review the pinned GHCR manifests and update them only when promoting another
   approved release. Tags alone are not accepted.
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
- `discogs-ingestion` owns the editable extraction rules (ADR 0005 assigned
  ownership to it, retiring the combined `catalog-ingestion` repository); this
  repository owns the promoted runtime copy and records producer provenance in
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

Primary GrooveMap images are named after their source repositories. The stack
retains shorter Compose service names such as `api`, `graphinator`, and
`dashboard` because they are internal DNS and operator compatibility
identifiers, not product or image names. See the local [container image and
identifier map](docs/dockerfile-standards.md) and [architecture
guide](docs/architecture.md) for the complete mapping.

The [documentation index](docs/README.md) links configuration, operations,
testing, performance, and troubleshooting guidance.
