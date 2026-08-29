# Troubleshooting guide

Start with read-only evidence. Restarts, teardown, cache or queue clearing,
migrations, and image changes require approval for the exact environment.

## 1. Validate the repository

Confirm that the stack definition itself is sound:

```bash
just check
```

For an operator-provided `.env`, render both supported configurations:

```bash
just config
just config-prod
```

A render failure commonly means an image variable is missing, still contains a
placeholder, lacks `@sha256:`, or references a missing production secret path.

## 2. Capture current state

```bash
docker compose ps
docker compose logs --timestamps --since=30m
```

Then narrow to the first unhealthy service and its dependencies:

```bash
docker compose logs --timestamps --tail=200 schema-init
docker compose logs --timestamps --tail=200 api
docker compose logs --timestamps --tail=200 postgres neo4j rabbitmq redis
```

Avoid starting with the last visible symptom. Compose services often remain
pending because a dependency is unhealthy or the one-shot schema initializer
did not complete.

## Image configuration failures

Symptoms include required-variable errors, image pull failures, or architecture
mismatches.

Check that every internal image follows this form:

```text
ghcr.io/groovemap-music/<owning-repository>@sha256:<manifest-digest>
```

Then verify:

- the repository name matches [Container image standards](dockerfile-standards.md);
- the digest is a manifest available to the target platform;
- Docker is authenticated to GHCR when the package is private;
- the approved release actually published that digest.

Do not work around a pull failure by switching to `latest` or adding a local
build context.

## Schema initializer failures

`schema-init` must finish successfully before application services start.
Inspect its logs and the database health checks:

```bash
docker compose ps schema-init postgres neo4j
docker compose logs --timestamps --tail=200 schema-init postgres neo4j
```

The initializer source and schema behavior belong to
[`database-schema`](https://github.com/groovemap-music/database-schema). This
repository owns only its image promotion, connections, dependencies, and
production secret wiring.

Common causes are invalid database credentials, incompatible database versions,
insufficient storage, and a failed database health check.

## Secret-file failures

The production overlay expects files under untracked `secrets/`. Check names
and permissions without printing values:

```bash
find secrets -maxdepth 1 -type f -exec stat -f '%Sp %N' {} \;
```

Expected permissions are `700` for the directory and `600` for each file. On
Linux, use the platform's `stat` format. `just secrets-bootstrap` creates only
missing files; it does not rotate or overwrite existing credentials.

If a service reports authentication failure, confirm that the same approved
credential is mounted into every required producer and consumer. Do not copy a
secret into logs, tickets, shell history, or a committed `.env`.

## Dependency health

With approved credentials, use non-mutating probes:

```bash
docker compose exec postgres pg_isready -U groovemap -d groovemap
docker compose exec rabbitmq rabbitmq-diagnostics -q ping
docker compose exec redis redis-cli ping
```

For Neo4j, use the environment's approved secret-access method rather than
placing a production password directly on a shared command line.

## Port conflicts

The base stack publishes ports 5433, 5672, 6379, 7474, 7687, 8003-8007, and
15672. Identify a local listener before changing Compose:

```bash
lsof -nP -iTCP -sTCP:LISTEN
```

Prefer an environment-specific override file for local port remapping. Review
the rendered configuration before starting anything.

## Unhealthy application service

Correlate the service log with dependency state and its published health probe:

| Service | Health endpoint | Source owner |
| --- | --- | --- |
| `api` | <http://localhost:8005/health> | [`catalog-api`](https://github.com/groovemap-music/catalog-api) |
| `dashboard` | <http://localhost:8003/health> | [`operations-console`](https://github.com/groovemap-music/operations-console) |
| `explore` | <http://localhost:8007/health> | [`graph-explorer`](https://github.com/groovemap-music/graph-explorer) |

For consumer failures, route source-level diagnosis to the appropriate graph
enricher, SQL loader, or catalog-ingestion repository. Include the exact image
digest and relevant redacted logs.

## Before an approved corrective action

Record:

- target environment and observation time;
- exact image digests;
- current `docker compose ps` output;
- the earliest relevant error;
- database, broker, cache, and storage health;
- backup status for any stateful action;
- proposed command and rollback.

Only then request approval for a restart, rollback digest, restore, migration,
queue operation, cache flush, or teardown. Afterward, repeat the same health
checks and record the outcome.
