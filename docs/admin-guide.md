# Admin Guide

## Creating an Admin Account

Admin accounts are created via the `admin-setup` CLI tool inside the API container:

```
docker exec -it groovemap-api admin-setup --email admin@example.com
```

`admin-setup` never accepts the password as a CLI argument (command-line
arguments are world-readable via `/proc/*/cmdline` and land in shell
history). The command above prompts interactively (input hidden, not
echoed). For scripted/non-interactive use, set `ADMIN_PASSWORD` (or
`ADMIN_PASSWORD_FILE`, pointing at a Docker secret) in the container's
environment instead.

Passwords must be at least 8 characters. If the email already exists, the password is updated.

## Listing Admin Accounts

```
docker exec -it groovemap-api admin-setup --list
```

## Accessing the Admin Panel

Navigate to `http://<host>:8003/admin` and log in with your admin credentials.

The monitoring dashboard at `http://<host>:8003` remains public — no login required.

## Triggering an Extraction

Click **Trigger Extraction** in the admin panel. This forces a full reprocessing of all Discogs data files:

- Downloads the latest monthly data from the Discogs S3 bucket
- Reprocesses all files regardless of existing state markers
- Publishes records to RabbitMQ for graphinator and tableinator consumers

The admin panel also supports triggering a **MusicBrainz extraction**, which downloads the latest MusicBrainz JSONL dumps and publishes records to the `groovemap-musicbrainz-{artists,labels,release-groups,releases}` exchanges for brainzgraphinator and brainztableinator consumers.

Use this when:

- A previous extraction failed and you want to retry
- You suspect data corruption and want a clean reprocess
- A new Discogs monthly dump (or MusicBrainz twice-weekly dump) has been published and you don't want to wait for the periodic check

The extraction runs asynchronously. Progress is tracked in the extraction history table.

If an extraction is already running, the trigger returns an error — wait for it to complete first.

This trigger has no cross-source lock: it only rejects a second trigger for the *same* source.
The Discogs import must still finish, including its consumers draining, before the MusicBrainz
one starts; see [Post-import identity maintenance](maintenance.md#post-import-identity-maintenance)
for why, the first-load bring-up procedure, and the re-attachment and projection steps that
follow every cycle.

## DLQ Management

Dead-letter queues (DLQs) collect messages that consumers failed to process. Each data type has a DLQ per consumer:

| Queue                                                             | Consumer          |
| ----------------------------------------------------------------- | ----------------- |
| `groovemap-discogs-graphinator-artists.dlq`                  | Graphinator       |
| `groovemap-discogs-graphinator-labels.dlq`                   | Graphinator       |
| `groovemap-discogs-graphinator-masters.dlq`                  | Graphinator       |
| `groovemap-discogs-graphinator-releases.dlq`                 | Graphinator       |
| `groovemap-discogs-tableinator-artists.dlq`                  | Tableinator       |
| `groovemap-discogs-tableinator-labels.dlq`                   | Tableinator       |
| `groovemap-discogs-tableinator-masters.dlq`                  | Tableinator       |
| `groovemap-discogs-tableinator-releases.dlq`                 | Tableinator       |
| `groovemap-musicbrainz-brainzgraphinator-artists.dlq`        | Brainzgraphinator |
| `groovemap-musicbrainz-brainzgraphinator-labels.dlq`         | Brainzgraphinator |
| `groovemap-musicbrainz-brainzgraphinator-release-groups.dlq` | Brainzgraphinator |
| `groovemap-musicbrainz-brainzgraphinator-releases.dlq`       | Brainzgraphinator |
| `groovemap-musicbrainz-brainztableinator-artists.dlq`        | Brainztableinator |
| `groovemap-musicbrainz-brainztableinator-labels.dlq`         | Brainztableinator |
| `groovemap-musicbrainz-brainztableinator-release-groups.dlq` | Brainztableinator |
| `groovemap-musicbrainz-brainztableinator-releases.dlq`       | Brainztableinator |

**Purging** permanently deletes all messages in a DLQ. Do this when:

- Messages are known-bad and will never succeed on retry
- After fixing the root cause and retriggering an extraction

Purging cannot be undone.

DLQ names follow the pattern `{exchange-prefix}-{consumer}-{data-type}.dlq`, using the `DISCOGS_EXCHANGE_PREFIX` and `MUSICBRAINZ_EXCHANGE_PREFIX` env vars as the base.

### DLQ Size-Cap Policy

A RabbitMQ policy bounds every catalog DLQ so a wedged consumer can never again grow one
unbounded — the release-groups incident parked 3.5M messages in a single classic DLQ before
this policy existed. RabbitMQ cannot change an existing classic queue's type
(`PRECONDITION_FAILED` at startup), and every catalog consumer (discogs-sql-loader,
discogs-graph-enricher, musicbrainz-sql-loader, musicbrainz-graph-enricher) deliberately keeps
its DLQ classic, so the cap is applied from the broker side as a policy matched by name —
`^groovemap-.*\.dlq$` — never by redeclaring a queue. That pattern matches all 16 DLQs listed
above (and any future consumer/entity that follows the same `.dlq` suffix) and no live queue,
since a live queue never ends in `.dlq` and the dead-letter exchanges end in `.dlx`, not `.dlq`.

The policy is provisioned automatically with the stack, but not at broker boot. RabbitMQ's
`load_definitions` boot-time import was tried first and rejected: on a blank node, importing a
definitions file skips creating the default vhost and user that `RABBITMQ_DEFAULT_USER` /
`RABBITMQ_DEFAULT_PASS` / `RABBITMQ_DEFAULT_VHOST` would otherwise create ("Nuances of
Boot-time Definition Import", [rabbitmq.com/docs/definitions](https://www.rabbitmq.com/docs/definitions)),
which would break every service's authentication on a fresh install. Instead, a one-shot,
idempotent compose service, `rabbitmq-dlq-policy-init`
(`scripts/rabbitmq-dlq-policy-init.sh`), waits for `rabbitmq` to report healthy and then
declares the policy over the management HTTP API with `rabbitmqadmin` (bundled in the
`rabbitmq:*-management` image, so this reuses the same image rather than adding a new one).
The script only ever calls `rabbitmqadmin declare policy` — it never declares a queue or
exchange, so a catalog service's DLQ is never redeclared.

**Overflow behaviour: `drop-head` (default).** At the cap, RabbitMQ drops the oldest
messages in the DLQ to admit new ones, rather than `reject-publish`, which would refuse the
dead-letter delivery itself. A refused dead-letter delivery pushes back onto the very
redelivery path that caused the incident (a wedged consumer nacking the same poison message
in a loop) and risks stalling the source queue instead of the broker just holding steady;
`drop-head` guarantees bounded disk/memory unconditionally and keeps the newest failures —
the ones most relevant to an incident in progress — at the cost of losing the oldest, already
undiagnosed entries once a DLQ is truly overflowing. Operators who need full retention should
watch the per-queue depth panels (`rabbitmq_queue_messages_ready` and friends on the
"GrooveMap Infrastructure" dashboard, gm-deployment-dqh.2) and drain or purge a DLQ before it
approaches the cap.

**Defaults**, each overridable via an env var read by `scripts/rabbitmq-dlq-policy-init.sh` — set
in `.env` or the shell before `docker compose up`, never by editing a catalog service:

| Env var                         | Default     | Meaning                                |
| -------------------------------- | ----------- | --------------------------------------- |
| `RABBITMQ_DLQ_MAX_LENGTH`        | `50000`     | Max messages retained per DLQ           |
| `RABBITMQ_DLQ_MAX_LENGTH_BYTES`  | `536870912` | Max total body bytes retained per DLQ (512 MiB) |
| `RABBITMQ_DLQ_OVERFLOW`          | `drop-head` | `drop-head` or `reject-publish`         |

**Inspecting or changing the live policy** (no restart needed — RabbitMQ applies a policy
change to matching queues immediately):

```
# List the policy and the values currently in effect
docker exec groovemap-rabbitmq rabbitmqctl list_policies

# Change a value at runtime (until rabbitmq-dlq-policy-init next reruns from the
# env vars above and overwrites it)
docker exec groovemap-rabbitmq rabbitmqctl set_policy catalog-dlq-cap \
  '^groovemap-.*\.dlq$' '{"max-length":100000,"max-length-bytes":1073741824,"overflow":"drop-head"}' \
  --apply-to queues --priority 10

# Or via the management UI: http://<host>:15672/#/policies
```

To make a change durable, set the corresponding env var and rerun the idempotent init service
(`docker compose up -d rabbitmq-dlq-policy-init`) so it re-declares the policy from it — this
does not touch or restart the `rabbitmq` service itself.

## Phase 3: Metrics History and Trend Analysis

### Queue and Health History Endpoints

Two new endpoints expose time-series metrics for queue depths and service health:

```
GET /api/admin/queues/history?range=<range>
GET /api/admin/health/history?range=<range>
```

Both endpoints require admin authentication (Bearer token).

**Valid range values:**

| Range  | Description             | Data Granularity  |
| ------ | ----------------------- | ----------------- |
| `1h`   | Last 1 hour             | 5-minute buckets  |
| `6h`   | Last 6 hours            | 5-minute buckets  |
| `24h`  | Last 24 hours (default) | 15-minute buckets |
| `7d`   | Last 7 days             | 1-hour buckets    |
| `30d`  | Last 30 days            | 6-hour buckets    |
| `90d`  | Last 90 days            | 1-day buckets     |
| `365d` | Last 365 days           | 1-day buckets     |

Granularity is selected automatically based on the requested range. Omitting the `range` parameter defaults to `24h`.

### Background Metrics Collector

A background collector runs inside the API service and periodically samples queue depths and service health. Collected data is stored in PostgreSQL for historical querying.

The collector interval is controlled by the `METRICS_COLLECTION_INTERVAL` environment variable (default: 300 seconds / 5 minutes).

### New Environment Variables

| Variable                      | Default | Description                                                                              |
| ----------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `METRICS_RETENTION_DAYS`      | `366`   | How many days of metrics to retain in the database. Older rows are pruned automatically. |
| `METRICS_COLLECTION_INTERVAL` | `300`   | Seconds between each metrics collection cycle in the background collector.               |

Set these in your `docker-compose.yml` or environment file:

```
METRICS_RETENTION_DAYS=366
METRICS_COLLECTION_INTERVAL=300
```

### New Database Tables

Metrics are stored in two PostgreSQL tables:

**`queue_metrics`** — RabbitMQ queue depth snapshots:

| Column                    | Type         | Description                                      |
| ------------------------- | ------------ | ------------------------------------------------ |
| `id`                      | bigint       | Primary key (generated always as identity)       |
| `recorded_at`             | timestamptz  | When the sample was taken                        |
| `queue_name`              | varchar(100) | Name of the RabbitMQ queue                       |
| `messages_ready`          | integer      | Number of ready messages at sample time          |
| `messages_unacknowledged` | integer      | Number of unacknowledged messages at sample time |
| `consumers`               | integer      | Number of active consumers at sample time        |
| `publish_rate`            | real         | Message publish rate                             |
| `ack_rate`                | real         | Message acknowledgement rate                     |

**`service_health_metrics`** — Per-service health check results:

| Column             | Type        | Description                                             |
| ------------------ | ----------- | ------------------------------------------------------- |
| `id`               | bigint      | Primary key (generated always as identity)              |
| `recorded_at`      | timestamptz | When the sample was taken                               |
| `service_name`     | varchar(50) | Name of the service (e.g. `graphinator`, `tableinator`) |
| `status`           | varchar(20) | Health status (`healthy`, `unhealthy`, `unknown`)       |
| `response_time_ms` | real        | Health check response time in milliseconds              |
| `endpoint_stats`   | jsonb       | Per-endpoint latency statistics (API service only)      |

Both tables are indexed on `recorded_at` for efficient range queries. Rows older than `METRICS_RETENTION_DAYS` are pruned automatically.

### Dashboard: Queue Trends and System Health Tabs

The admin panel (`http://<host>:8003/admin`) exposes two new tabs backed by the history endpoints:

- **Queue Trends** — Line charts showing message depth over time for each RabbitMQ queue. Use the range selector (1h / 6h / 24h / 7d / 30d / 90d / 365d) to zoom in or out.
- **System Health** — Status timeline showing per-service health over the selected range. Unhealthy periods are highlighted in red; response time is shown as a secondary series.

Both tabs auto-refresh every 60 seconds and respect the currently selected time range.
