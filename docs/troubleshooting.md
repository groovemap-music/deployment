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
docker compose logs --timestamps --tail=200 postgres neo4j rabbitmq valkey
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
docker compose exec valkey valkey-cli ping
```

For Neo4j, use the environment's approved secret-access method rather than
placing a production password directly on a shared command line.

## RabbitMQ message rates are empty in the management UI

This is expected. The broker runs with the management metrics collector disabled
(ADR 0017), so the management UI's publish and acknowledgement rate charts and
the API's `message_stats` stay empty. Queue depth, consumers, and rates come from
the `rabbitmq_prometheus` endpoint, which the operations console and the Grafana
dashboards read (the console's history tabs through VictoriaMetrics):

```bash
docker compose exec rabbitmq rabbitmqctl eval \
  'application:get_env(rabbitmq_management_agent, disable_metrics_collector).'   # {ok,true}
curl -s 'http://127.0.0.1:15692/metrics/detailed?family=queue_coarse_metrics&family=queue_delivery_metrics&family=queue_exchange_metrics' \
  | grep -E '^rabbitmq_detailed_queue_(messages_acked_total|exchange_messages_published_total)'
```

If the console's queue view shows no rates while those counters advance, check
that `RABBITMQ_PROMETHEUS_URL` on the dashboard service points at
`http://rabbitmq:15692`. Do not restore the retired
`-rabbitmq_management disable_management_stats false` override; it re-enables a
deprecated collector (`management_metrics_collection`) the broker logs a warning
for at every start.

## Valkey at maxmemory: writes are rejected

Alerts: `ValkeyMemoryHigh` (early warning), `ValkeyWritesRejected` and
`ValkeyCacheWriteFailures`. Valkey runs `--maxmemory-policy noeviction` on
purpose: it holds security state (`revoked:jti:*`, `password_changed:*`) that
must never be evicted. At `maxmemory` it therefore evicts nothing and refuses
every memory-adding command (SET, SETEX, SET NX, INCR) with
`OOM command not allowed`, while reads, DEL, GETDEL, and EXPIRE keep working.

What users see:

- Cache writes fail quietly: the cache hit ratio falls and database load rises.
  This is `groovemap_api_cache_write_failures_total` increasing.
- Login 2FA challenges, logout revocation, password reset and change, and OAuth
  state writes answer `503` with `Retry-After: 30`. Those requests are not
  broken permanently; they succeed once Valkey accepts writes again.

Diagnose with read-only commands, with approved credentials:

```bash
docker compose exec valkey valkey-cli info memory | grep -E 'used_memory_human|maxmemory_human|maxmemory_policy'
docker compose exec valkey valkey-cli info errorstats
docker compose exec valkey valkey-cli --bigkeys
docker compose logs --tail=200 api | grep -E 'valkey_write_rejected|Cache write failed'
```

`errorstat_OOM` confirms rejection by the server. The infrastructure dashboard
panel "Valkey rejected writes and cache write failures" shows when it began.
The log keys (`valkey_write_rejected` and the others) are only readable with
`docker compose logs`; this stack has no log pipeline to alert on them.

Resolution, which changes state and needs approval before it is run:

1. Find what is consuming memory (`--bigkeys`, key patterns by prefix). Cache
   entries are safe to drop; `revoked:jti:*` and `password_changed:*` are not,
   because deleting a revocation marker re-validates a revoked token.
2. Either remove the excess cache keys (cache entries have TTLs and are
   rebuilt from the database) or raise `maxmemory` in the Valkey service
   command and recreate it, sizing the container memory limit to match.
3. Do not switch the policy to an evicting one to make the alert go away; that
   silently drops revocation markers.
4. Confirm recovery: `valkey_memory_used_bytes / valkey_memory_max_bytes` back
   under 85%, `ValkeyWritesRejected` resolved, and the cache hit ratio
   recovering.

## Port conflicts

The base stack publishes ports 3000, 5433, 5672, 6379, 7474, 7687, 8003-8007,
8428, 10428, and 15672, plus 15692 on loopback only. Identify a local listener before changing Compose:

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
enricher or SQL loader repository; for extraction failures, route to
`discogs-ingestion` or `musicbrainz-ingestion`, whichever source is affected.
Include the exact image digest and relevant redacted logs.

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
