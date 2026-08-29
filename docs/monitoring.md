# Monitoring guide

This guide covers observation of a running GrooveMap Compose environment. The
deployment repository defines health checks and topology; application metrics,
alerts, and logging behavior are owned by the corresponding source repository.

## Safe first checks

The following commands are read-only against the running environment:

```bash
docker compose ps
docker compose config --services
docker compose logs --tail=100
docker compose logs --tail=100 api
```

Add timestamps when correlating events:

```bash
docker compose logs --timestamps --since=30m
```

Interactive log following is useful during an incident, but it remains attached
until interrupted:

```bash
docker compose logs --follow --timestamps api
```

## Health endpoints

The base configuration publishes these probes:

| Service | Endpoint |
| --- | --- |
| Operations console | <http://localhost:8003/health> |
| Catalog API | <http://localhost:8005/health> |
| Graph explorer | <http://localhost:8007/health> |
| Neo4j HTTP | <http://localhost:7474> |
| RabbitMQ management | <http://localhost:15672> |

Compose also evaluates container-native checks for PostgreSQL, Neo4j,
RabbitMQ, Redis, the schema initializer, and every long-running application.
Use `docker compose ps` as the canonical summary of those checks.

## Dependency inspection

Only run `exec` probes when the target environment and credentials are
approved. These examples do not intentionally mutate data:

```bash
docker compose exec postgres pg_isready -U groovemap -d groovemap
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1"
docker compose exec rabbitmq rabbitmq-diagnostics -q ping
docker compose exec redis redis-cli ping
```

In production, obtain credentials through the environment's approved secret
access process. Do not print file-backed secrets or place them on a shared
command line.

## Queue monitoring

RabbitMQ's management interface at <http://localhost:15672> shows queue depth,
consumer count, publish rate, and acknowledgement rate. A useful incident
snapshot records:

| Queue | Messages | Consumers | Publish rate | Ack rate | Observation time |
| --- | ---: | ---: | ---: | ---: | --- |
| Example queue | 1,234 | 2 | 45.2/s | 45.1/s | ISO-8601 timestamp |

A growing queue with no consumers is usually a consumer health or dependency
problem. A growing queue with active consumers may instead indicate throughput
pressure. Correlate queue state with the relevant service logs before taking
action.

## Database observation

For PostgreSQL, monitor connection count, long-running queries, lock waits,
storage, WAL growth, and checkpoint behavior. For Neo4j, monitor heap,
page-cache pressure, transaction duration, query latency, and storage. Keep
queries read-only unless a separately reviewed runbook authorizes mutation.

The source repositories own query-specific diagnostics:

- [`catalog-api`](https://github.com/groovemap-music/catalog-api) for API query
  and authentication behavior;
- [`discogs-graph-enricher`](https://github.com/groovemap-music/discogs-graph-enricher)
  and [`musicbrainz-graph-enricher`](https://github.com/groovemap-music/musicbrainz-graph-enricher)
  for Neo4j consumers;
- [`discogs-sql-loader`](https://github.com/groovemap-music/discogs-sql-loader)
  and [`musicbrainz-sql-loader`](https://github.com/groovemap-music/musicbrainz-sql-loader)
  for PostgreSQL consumers.

## Operations console and analytics

The operations console is published at <http://localhost:8003> in the base
configuration. Its implementation and alert behavior belong to
[`operations-console`](https://github.com/groovemap-music/operations-console).
Precomputed analytics behavior belongs to
[`analytics-engine`](https://github.com/groovemap-music/analytics-engine).

## Incident snapshot

Before restarting or stopping anything, capture:

```bash
docker compose ps > compose-ps.txt
docker compose logs --timestamps --since=1h > compose-last-hour.log
docker compose config > compose-rendered.yaml
```

Treat the rendered configuration and logs as potentially sensitive. Store and
share them according to the incident's data-handling requirements, then remove
local copies when no longer needed.

Record at least:

- observation time and timezone;
- environment name;
- exact service image digests;
- unhealthy or restarting containers;
- first observed error and correlated dependency errors;
- queue depth and consumer state;
- disk and memory pressure;
- any approved actions and their outcomes.

## Changes require approval

Restarting services, scaling consumers, clearing queues or caches, running
migrations, changing image digests, and invoking `just smoke`, `just down`, or
`just performance` all change environment state. Diagnose first, then obtain
operator approval for the exact action and target.
