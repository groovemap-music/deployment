# Container image standards

The deployment repository consumes immutable images. It does not own service
Dockerfiles, build contexts, package source, or image release workflows.

## Ownership boundary

Each source repository owns its Dockerfile, tests, OCI metadata, version tag,
and GHCR publication. Deployment owns the environment variable that promotes a
released image into the stack.

| Source repository and primary image | Compose service | Container name | Required variable |
| --- | --- | --- | --- |
| [`database-schema`](https://github.com/groovemap-music/database-schema) / `groovemap-music/database-schema` | `schema-init` | `groovemap-schema-init` | `DATABASE_SCHEMA_IMAGE` |
| [`catalog-api`](https://github.com/groovemap-music/catalog-api) / `groovemap-music/catalog-api` | `api` | `groovemap-api` | `CATALOG_API_IMAGE` |
| [`catalog-ingestion`](https://github.com/groovemap-music/catalog-ingestion) / `groovemap-music/catalog-ingestion` | `extractor-discogs`, `extractor-musicbrainz` | `groovemap-extractor-discogs`, `groovemap-extractor-musicbrainz` | `CATALOG_INGESTION_IMAGE` |
| [`discogs-graph-enricher`](https://github.com/groovemap-music/discogs-graph-enricher) / `groovemap-music/discogs-graph-enricher` | `graphinator` | `groovemap-graphinator` | `DISCOGS_GRAPH_ENRICHER_IMAGE` |
| [`musicbrainz-graph-enricher`](https://github.com/groovemap-music/musicbrainz-graph-enricher) / `groovemap-music/musicbrainz-graph-enricher` | `brainzgraphinator` | `groovemap-brainzgraphinator` | `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE` |
| [`discogs-sql-loader`](https://github.com/groovemap-music/discogs-sql-loader) / `groovemap-music/discogs-sql-loader` | `tableinator` | `groovemap-tableinator` | `DISCOGS_SQL_LOADER_IMAGE` |
| [`musicbrainz-sql-loader`](https://github.com/groovemap-music/musicbrainz-sql-loader) / `groovemap-music/musicbrainz-sql-loader` | `brainztableinator` | `groovemap-brainztableinator` | `MUSICBRAINZ_SQL_LOADER_IMAGE` |
| [`operations-console`](https://github.com/groovemap-music/operations-console) / `groovemap-music/operations-console` | `dashboard` | `groovemap-dashboard` | `OPERATIONS_CONSOLE_IMAGE` |
| [`graph-explorer`](https://github.com/groovemap-music/graph-explorer) / `groovemap-music/graph-explorer` | `explore` | `groovemap-explore` | `GRAPH_EXPLORER_IMAGE` |
| [`analytics-engine`](https://github.com/groovemap-music/analytics-engine) / `groovemap-music/analytics-engine` | `insights` | `groovemap-insights` | `ANALYTICS_ENGINE_IMAGE` |

Each primary image therefore has the form
`ghcr.io/groovemap-music/<source-repository>`. An auxiliary image appends its
role to the owning repository name. The performance runner is owned by
`catalog-api` and uses
`ghcr.io/groovemap-music/catalog-api-performance`; deployment does not publish
it.

## Compatibility identifiers

The Compose service keys, hostnames, and `groovemap-*` container names in the
table are retained runtime contracts. They appear in service discovery,
operator commands, volumes, and health checks, so changing them would be an
environment migration. Names such as `graphinator`, `tableinator`, `explore`,
and `insights` describe those compatibility identifiers only; the source
repository and GHCR path are the canonical service and artifact identities.

RabbitMQ exchanges and queues are also retained wire contracts. The
`groovemap-discogs-*`, `groovemap-musicbrainz-*`, `graphinator-*`,
`tableinator-*`, `brainzgraphinator-*`, and `brainztableinator-*` names must
remain compatible across independently released producers and consumers. See
the [message queue architecture](architecture.md#message-queue-architecture)
for the exact topology.

## Promotion requirements

Internal image values in `.env` must use an approved manifest digest:

```dotenv
CATALOG_API_IMAGE=ghcr.io/groovemap-music/catalog-api@sha256:<64-hex-character-digest>
```

The deployment policy rejects:

- mutable `latest` references;
- tag-only references;
- service images built from sibling directories;
- image names that do not match the owning repository;
- missing required image variables.

Tags remain useful for locating a release, but the promoted deployment input is
the resolved manifest digest. This keeps an environment reproducible even if a
registry tag changes.

## Source-repository release requirements

Repositories that publish an image should:

- build only from their own source tree;
- publish only from an approved `v*` release tag;
- test the image before publication;
- publish to `ghcr.io/groovemap-music/<repository>`;
- include standard OCI source, revision, version, license, title, description,
  and created-time annotations;
- run as an unprivileged user where the application permits it;
- define a health check for long-running services;
- avoid embedding credentials or environment-specific configuration.

Implementation details belong in the owning repository so the Dockerfile and
its documentation evolve together.

## Base infrastructure images

Images not built by GrooveMap, such as PostgreSQL, Neo4j, RabbitMQ, and Redis,
are declared directly in `docker-compose.yml` and pinned by digest. Upgrading
one requires reviewing its release notes, updating both the human-readable tag
and digest, and rerunning the deployment gate.

## Validation

Run the credential-free image and Compose policy checks with:

```bash
uv run python scripts/check-images.py
bash scripts/check-compose.sh
```

The full review gate is:

```bash
just check
```

These commands do not pull or start service images. Live smoke and performance
checks require separate operator approval and real environment inputs.
