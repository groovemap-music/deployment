# Performance guide

This repository owns stack-level performance configuration and the controlled
invocation of the catalog API performance image. Service algorithms,
benchmarks, profilers, and performance-runner source remain in their owning
source repositories.

[Back to main](../README.md) | [Documentation index](README.md) |
[Observability](observability.md)

## Safety boundary

`just check` and CI never start a stack or a workload. Performance execution is
an explicit, stateful operation against an already-running environment and
requires operator approval for the exact target, image, load, and result path.
The runner can consume database, broker, CPU, memory, and network capacity; do
not aim it at a live environment by inference.

The repository does not contain performance-runner source or a Dockerfile. It
retains only:

- `tests/perftest/config.yaml`, the environment-specific workload selection;
- `tests/perftest/README.md`, the invocation boundary; and
- `scripts/run-perftest.sh`, the validation and Docker adapter.

[`catalog-api`](https://github.com/groovemap-music/catalog-api) owns and
publishes the `catalog-api-performance` image.

## Run an approved workload

Set an immutable source-owned image and, when necessary, the Compose network,
configuration file, or ignored results directory:

```bash
PERFTEST_IMAGE='ghcr.io/groovemap-music/catalog-api-performance@sha256:<digest>' \
PERFTEST_NETWORK='deployment_groovemap' \
PERFTEST_CONFIG='tests/perftest/config.yaml' \
PERFTEST_RESULTS='perftest-results' \
  just performance
```

The adapter rejects a mutable tag, an image outside the
`groovemap-music/catalog-api-performance` repository, a missing config file,
and an invalid network name before invoking Docker. Results are written under
the ignored `perftest-results/` directory by default. They are environment
evidence, not source artifacts; never commit them.

## Executable stack tuning

The Compose files are authoritative. Review their rendered merge with
`just config-prod` before relying on any value below.

### Consumer batches

The base stack presents these settings to the graph and SQL consumers:

| Compose services | Variables | Value |
| --- | --- | ---: |
| `graphinator`, `brainzgraphinator` | `NEO4J_BATCH_MODE` | `true` |
| `graphinator`, `brainzgraphinator` | `NEO4J_BATCH_SIZE` | `500` |
| `graphinator`, `brainzgraphinator` | `NEO4J_BATCH_FLUSH_INTERVAL` | `2.0` seconds |
| `tableinator`, `brainztableinator` | `POSTGRES_BATCH_MODE` | `true` |
| `tableinator`, `brainztableinator` | `POSTGRES_BATCH_SIZE` | `500` |
| `tableinator`, `brainztableinator` | `POSTGRES_BATCH_FLUSH_INTERVAL` | `2.0` seconds |

These are container environment values, not `.env` interpolation inputs. Use
a reviewed Compose override to tune them. Whether a particular image consumes
every variable is a source-repository contract; for example,
`musicbrainz-sql-loader` currently coordinates concurrency through its
PostgreSQL pool rather than batch flushing.

Any batch change must be evaluated with queue depth, acknowledgement rate,
failure/retry count, database latency, and memory together. A higher batch can
improve throughput while increasing redelivery work and shutdown latency.

### RabbitMQ

The base broker sets:

- quorum queues by default;
- `max_message_size` to 128 MiB for large MusicBrainz records;
- `nofile` soft and hard limits to 65,536; and
- management and per-object Prometheus metrics for queue-level dashboards.

Do not change queue type, message size, or acknowledgement behavior as a local
performance experiment. Those are compatibility and durability contracts.

### PostgreSQL

The base stack raises `max_wal_size` to 4 GiB and sets
`checkpoint_completion_target=0.9`. The production overlay additionally sets:

| Area | Production value |
| --- | --- |
| Memory | `shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=64MB`, `maintenance_work_mem=512MB` |
| WAL | `wal_buffers=64MB`, `min_wal_size=1GB`, `max_wal_size=4GB` |
| Parallelism | 4 workers per gather, 8 total workers, 4 maintenance workers |
| Connections | `max_connections=100` |
| Diagnostics | `pg_stat_statements`, queries over 1 second, checkpoints, and temp files logged |
| Autovacuum | 4 workers with 0.05 vacuum and 0.025 analyze scale factors |

These values assume an SSD-shaped host and are not portable promises. Confirm
memory headroom, backend connection budgets, WAL/storage growth, checkpoint
duration, and restore behavior before promotion.

### Neo4j

The base stack uses a 1 GiB initial heap, 2 GiB maximum heap, 1 GiB page cache,
and a 6 GiB transaction-memory cap. When `.env.example` is copied unchanged,
the production overlay receives `NEO4J_HEAP_SIZE=2G`; otherwise its overlay
default is 8 GiB. It defaults the page cache to 16 GiB and the container memory
limit to 48 GiB through `NEO4J_PAGECACHE_SIZE` and `NEO4J_MEMORY_LIMIT`.

The production overlay also raises checkpoint IOPS, expands the Bolt thread
pool, logs queries over five seconds, and sets a ten-minute transaction timeout.
Heap, page cache, transaction memory, and the container limit must fit together;
render the actual environment rather than adding the documented defaults.

### Redis

Redis is bounded to 512 MiB with `allkeys-lru` and append-only persistence on
`redis_data`. Treat eviction and persistence as part of performance evidence:
the cache may evict under pressure, while AOF rewrite and volume I/O still
consume resources.

## Observe before and after

The deployed collector remote-writes application and infrastructure metrics to
VictoriaMetrics, and traces to VictoriaTraces. Grafana provisions the canonical
dashboards from `config/grafana/`. Use the
[observability runbook](observability.md#verification) and record at least:

- exact service image digests and rendered Compose configuration;
- workload configuration, start/end time, and target environment;
- latency distribution and error rate, not only average latency;
- queue depth, publish/ack rates, redeliveries, and consumer count;
- PostgreSQL connections, locks, checkpoints, WAL, and storage;
- Neo4j transaction/query latency, heap/page cache, and storage;
- container CPU, memory, OOM events, filesystem, and network saturation; and
- trace evidence for the slow path being measured.

Keep warm-up and measured intervals separate. Compare like-for-like datasets
and cache state. A result without the image digests and workload configuration
is not reproducible evidence.

## Rollout and rollback

1. Establish a baseline with the currently promoted digests.
2. Change one independent variable in an isolated or approved target.
3. Render and review base and production Compose.
4. Run the same workload and compare the complete evidence set.
5. Promote only after functional checks, capacity limits, and recovery behavior
   remain acceptable.

Record the previous image digest and every changed Compose value before rollout.
Rollback means restoring those exact values, not switching to a tag. Database
format or schema changes may make an image-only rollback unsafe; confirm backup
and restore requirements in [Database resilience](database-resilience.md).

## Ownership routing

- Query and API performance: [`catalog-api`](https://github.com/groovemap-music/catalog-api)
- Discogs parsing/publishing: [`discogs-ingestion`](https://github.com/groovemap-music/discogs-ingestion)
- MusicBrainz parsing/publishing: [`musicbrainz-ingestion`](https://github.com/groovemap-music/musicbrainz-ingestion)
- Neo4j consumers: [`discogs-graph-enricher`](https://github.com/groovemap-music/discogs-graph-enricher) and [`musicbrainz-graph-enricher`](https://github.com/groovemap-music/musicbrainz-graph-enricher)
- PostgreSQL consumers: [`discogs-sql-loader`](https://github.com/groovemap-music/discogs-sql-loader) and [`musicbrainz-sql-loader`](https://github.com/groovemap-music/musicbrainz-sql-loader)
- Stack topology, production overrides, and performance invocation: this repository

Run `just check` after any documentation or configuration change. It validates
the Compose combinations without starting containers; it does not substitute
for an approved workload run.
