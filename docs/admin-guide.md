# Admin Guide

The operations console is one authenticated shell, signed in through `catalog-api`'s admin
session. This guide covers the deployment side: creating the admin accounts that sign in, the
extraction and DLQ procedures an operator runs, and where history comes from. The console's own
behaviour is documented in `operations-console`, not here:

- [Operations-console administrator guide](https://github.com/groovemap-music/operations-console/blob/main/docs/admin-guide.md):
  signing in, the Overview, topology, run progress, platform health, the attention bar, triggers
  and the history tabs.
- [Operations-console configuration](https://github.com/groovemap-music/operations-console/blob/main/docs/configuration.md):
  every setting the console reads.

The release that introduced this console, and what changed with it, is in the
[October 2026 operations-console rollout](rollout-2026-10-operations-console.md).

## Creating an Admin Account

Admin accounts are created with the `catalog-admin-setup` CLI (the `catalog-api` package's
`[project.scripts]` entry) inside the API container:

```
docker exec -it groovemap-api catalog-admin-setup --email admin@example.com
```

`catalog-admin-setup` never accepts the password as a CLI argument (command-line
arguments are world-readable via `/proc/*/cmdline` and land in shell
history). The command above prompts interactively (input hidden, not
echoed). For scripted/non-interactive use, set `ADMIN_PASSWORD` (or
`ADMIN_PASSWORD_FILE`, pointing at a Docker secret) in the container's
environment instead.

Passwords must be at least 8 characters. If the email already exists, the password is updated.

## Listing Admin Accounts

```
docker exec -it groovemap-api catalog-admin-setup --list
```

## Signing in to the Console

Navigate to `http://<host>:8003` (the `dashboard` service) and sign in with your admin
credentials. Until `catalog-api` confirms an admin session the console shows only the login form;
after sign-in the live **Overview** comes first, followed by the admin tabs. Nothing on the
console is public except `/health` and the login page. The old `/admin` address redirects to `/`.
See the [operations-console administrator guide](https://github.com/groovemap-music/operations-console/blob/main/docs/admin-guide.md#signing-in-to-the-console)
for session lifetime, sign-out and WebSocket behaviour, and put no query-string logging on `/ws`
in any reverse proxy.

## Triggering an Extraction

Use **Trigger Discogs Extraction** or **Trigger MusicBrainz Extraction** in the console. Each
trigger asks `catalog-api` to start the matching source's extractor (`extractor-discogs` or
`extractor-musicbrainz`), which:

- Downloads the latest dump (Discogs monthly data from the Discogs S3 bucket, or the MusicBrainz JSONL dumps)
- Reprocesses all files regardless of existing state markers
- Publishes records to the `groovemap-discogs-{artists,labels,masters,releases}` or `groovemap-musicbrainz-{artists,labels,release-groups,releases}` exchanges for the source's SQL loader and graph enricher consumers

See [Triggering an extraction](https://github.com/groovemap-music/operations-console/blob/main/docs/admin-guide.md#triggering-an-extraction)
in the console guide for the routes and the per-source behaviour.

Use this when:

- A previous extraction failed and you want to retry
- You suspect data corruption and want a clean reprocess
- A new Discogs monthly dump (or MusicBrainz twice-weekly dump) has been published and you don't want to wait for the periodic check

The extraction runs asynchronously. Progress is tracked in the extraction history table, which shows each run's source.

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
| `groovemap-discogs-graphinator-artists.dlq`                  | `discogs-graph-enricher` |
| `groovemap-discogs-graphinator-labels.dlq`                   | `discogs-graph-enricher` |
| `groovemap-discogs-graphinator-masters.dlq`                  | `discogs-graph-enricher` |
| `groovemap-discogs-graphinator-releases.dlq`                 | `discogs-graph-enricher` |
| `groovemap-discogs-tableinator-artists.dlq`                  | `discogs-sql-loader` |
| `groovemap-discogs-tableinator-labels.dlq`                   | `discogs-sql-loader` |
| `groovemap-discogs-tableinator-masters.dlq`                  | `discogs-sql-loader` |
| `groovemap-discogs-tableinator-releases.dlq`                 | `discogs-sql-loader` |
| `groovemap-musicbrainz-brainzgraphinator-artists.dlq`        | `musicbrainz-graph-enricher` |
| `groovemap-musicbrainz-brainzgraphinator-labels.dlq`         | `musicbrainz-graph-enricher` |
| `groovemap-musicbrainz-brainzgraphinator-release-groups.dlq` | `musicbrainz-graph-enricher` |
| `groovemap-musicbrainz-brainzgraphinator-releases.dlq`       | `musicbrainz-graph-enricher` |
| `groovemap-musicbrainz-brainztableinator-artists.dlq`        | `musicbrainz-sql-loader` |
| `groovemap-musicbrainz-brainztableinator-labels.dlq`         | `musicbrainz-sql-loader` |
| `groovemap-musicbrainz-brainztableinator-release-groups.dlq` | `musicbrainz-sql-loader` |
| `groovemap-musicbrainz-brainztableinator-releases.dlq`       | `musicbrainz-sql-loader` |

**Purging** permanently deletes all messages in a DLQ. Do this when:

- Messages are known-bad and will never succeed on retry
- After fixing the root cause and retriggering an extraction

Purging cannot be undone.

DLQ names follow the pattern `{exchange-prefix}-{consumer}-{data-type}.dlq`, using the `DISCOGS_EXCHANGE_PREFIX` and `MUSICBRAINZ_EXCHANGE_PREFIX` env vars as the base. The consumer keys in the names (`graphinator`, `tableinator`, `brainzgraphinator`, `brainztableinator`) are stable wire identifiers, even though the owning repositories have descriptive names.

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
"GrooveMap · Infrastructure" dashboard, gm-deployment-dqh.2) and drain or purge a DLQ before it
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

## Queue and Service History

The console's **Queue Trends** and **System Health** tabs read their history from
VictoriaMetrics (`VICTORIA_METRICS_URL`, set on the `dashboard` service to
`http://victoria-metrics:8428`). `catalog-api` no longer collects or serves metrics history, and
there are no `METRICS_RETENTION_DAYS` or `METRICS_COLLECTION_INTERVAL` settings to tune.

- **Retention:** the `victoria-metrics` service runs with `-retentionPeriod=90d`, which covers the
  longest range the tabs offer (`1h`, `6h`, `24h`, `7d`, `30d`, `90d`). Lowering it silently
  truncates the long ranges.
- **Rate history** (publish and ack rates) comes from the `rabbitmq-detailed` scrape and begins
  when that scrape starts.
- **Old rows:** the PostgreSQL `queue_metrics` and `service_health_metrics` tables were dropped by
  the schema migration; the rollout keeps a dump first
  ([4.5](rollout-2026-10-operations-console.md#45-keep-a-copy-of-the-metrics-history-rows-approval)).

The history routes, the series the tabs depend on, and the tab behaviour are in the console's
[metrics history section](https://github.com/groovemap-music/operations-console/blob/main/docs/admin-guide.md#metrics-history-and-trend-analysis)
and [configuration](https://github.com/groovemap-music/operations-console/blob/main/docs/configuration.md).
