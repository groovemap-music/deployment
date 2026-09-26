# Maintenance guide

Maintenance in this repository means promoting immutable service images,
updating Compose topology and pinned infrastructure images, rotating runtime
configuration, and preserving recoverability. Service source and Dockerfiles
remain in their owning repositories.

## Routine repository maintenance

Before review, run the credential-free gate:

```bash
mise install
just setup
just check
```

Dependabot proposes dependency and workflow updates. Review lockfile changes,
upstream release notes, license changes, and the complete Compose render before
merging.

## Promote a service release

1. Confirm that the owning source repository released the intended `v*` tag.
2. Resolve its GHCR manifest digest for the target platform.
3. Update the corresponding image value in the environment's untracked `.env`.
4. Run `just config` and, for production, `just config-prod`.
5. Review the rendered diff and rollback digest.
6. Obtain approval for the target environment.
7. Apply the reviewed Compose change and verify health.

Never replace a digest with `latest` or a tag-only reference. Do not add a
sibling build context to this repository.

## Wave-2 image promotion (identity and activity)

The native-identity and first-party-events program
([ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md),
[ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md))
changed both `catalog-api` and `database-schema`. Both releases are now cut:
`database-schema` `v0.3.0` and `catalog-api` `v0.2.0` carry the identity and
activity work, and their manifest digests are recorded as reviewed in
`scripts/check-images.py`'s `RELEASED_IMAGE_DIGESTS` and in
[Recorded release digests](#recorded-release-digests) below.

Recording a digest as reviewed is not an instruction to deploy it. Applying
either image to a live environment is a separate operator step: follow
[Promote a service release](#promote-a-service-release) above to update the
target environment's `.env` and obtain approval for that environment.

## Per-source extractor cutover

`extractor-discogs` and `extractor-musicbrainz` consume separate, source-owned
images. `DISCOGS_INGESTION_IMAGE` promotes `discogs-ingestion` and
`MUSICBRAINZ_INGESTION_IMAGE` promotes `musicbrainz-ingestion`. The retired
combined `catalog-ingestion` image and its single `CATALOG_INGESTION_IMAGE`
variable no longer exist. The per-source entrypoints take no `--source`
argument, and the MusicBrainz container no longer polls Discogs health: the two
sources ingest concurrently with no cross-container ordering, lock, or mutual
exclusion. Compose service names, container names, hostnames, the `8000` health
port, data volumes, exchanges, queues, and durable state markers are unchanged
by the split, which is what makes a source-local rollback possible.

The environment `.env` is untracked and is the only place real image values
live. Every value in it must be an immutable `@sha256:<manifest-digest>`
reference; a tag, `latest`, or a floating reference is not a deployment input
and is not a rollback target.

Cut over **one source at a time**, and never run an old and a new producer for
the same source against production exchanges at once — duplicated data and
completion events make parity evidence ambiguous and burn retry budgets.

1. Rehearse the new image against an isolated broker with source-matching
   consumers; compare fixtures, exchange and queue declarations, completion
   ordering, marker restart, health, trigger, and shutdown behavior.
2. Stop that source's extractor and confirm it is no longer publishing.
3. Record the currently deployed digest for that source as the rollback target.
4. Set only that source's variable in `.env` to the new manifest digest.
5. Run `just config` and `just config-prod`, and review the rendered diff.
6. Obtain approval, start the service, and verify health and its consumers.
7. Only then begin the other source's cutover.

Rollback is source-local. Quiesce the new producer for the affected source,
retain its data volume and durable state marker, then restore the recorded
known-good digest for that source alone. A Discogs rollback does not stop
MusicBrainz, or vice versa, unless an independent incident calls for both.

### Recorded release digests

Reviewed per-source producer images (Discogs `v0.3.1`, MusicBrainz `v0.2.1`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DISCOGS_INGESTION_IMAGE` | `ghcr.io/groovemap-music/discogs-ingestion` | `sha256:db418bfc97d2d364ac0e64045b492ad8492b500c84f8ce105a04cadd400ee17c` |
| `MUSICBRAINZ_INGESTION_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-ingestion` | `sha256:2b348519450cc9811fe8d194d0ef4b4dd3ead901b2f8e5883dec83a839bd9b37` |

Matching consumer and schema images (Discogs SQL loader and graph enricher
`v0.3.0`; other consumers `v0.2.0`; `database-schema` `v0.3.0`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DATABASE_SCHEMA_IMAGE` | `ghcr.io/groovemap-music/database-schema` | `sha256:6fba747ff353d6f4639b566a33ba73ab79515daaee756980704c88e2c6f32b1c` |
| `DISCOGS_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/discogs-sql-loader` | `sha256:09f55827f972ec289baad7128acca061739fd9d4d350f23f3d6d22afeafee7e6` |
| `DISCOGS_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/discogs-graph-enricher` | `sha256:e95fabb7633859c94e0913f9122ccbaed18a04a017c486886c38840c230ff80d` |
| `MUSICBRAINZ_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-sql-loader` | `sha256:cab35264260d6df0e3a86e2022ed3a6b02506b8404aa845921ff7ec18605b027` |
| `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-graph-enricher` | `sha256:541cc5ef9823a970a44af2952e641a6c925011e1d653274e419fbfc72df62b6e` |

Reviewed catalog API image (`v0.4.0`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `CATALOG_API_IMAGE` | `ghcr.io/groovemap-music/catalog-api` | `sha256:4889f1ce04568a335bdfe698e11a3c8133238e0b0b61c91447ea4448a3e3ae43` |

The three changed entries resolve from their released tags for `linux/amd64`
(the platform published by these release workflows). Their reviewed rollback
targets are the previous index digests: `DISCOGS_SQL_LOADER_IMAGE`
`sha256:dfa00f9ee24d9fab6212b02a272486f70490b741e9556edf0b2fd2c793f3393c`,
`DISCOGS_GRAPH_ENRICHER_IMAGE`
`sha256:933df432732e8f1b863f1b3e3945ff0619a141e1708889a05f9f4dcf2003335b`,
and `CATALOG_API_IMAGE`
`sha256:b236b3ac805e4e92f7d9cc889d0e15d966a293d764af860d02c46b15efc9c5cb`.

These are records of what was published, not an instruction to deploy. Verify a
digest against the registry before promoting it, and re-resolve it for any
platform other than the one the release workflow published.

### Identifier lookup verification (2026-09-21)

The GHCR tag manifests for `linux/amd64` were checked before the disposable
smoke. The changed image index digests were:

| Released tag | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `discogs-sql-loader:v0.3.0` | `sha256:09f55827f972ec289baad7128acca061739fd9d4d350f23f3d6d22afeafee7e6` | `v0.2.0` — `sha256:dfa00f9ee24d9fab6212b02a272486f70490b741e9556edf0b2fd2c793f3393c` |
| `discogs-graph-enricher:v0.3.0` | `sha256:e95fabb7633859c94e0913f9122ccbaed18a04a017c486886c38840c230ff80d` | `v0.2.0` — `sha256:933df432732e8f1b863f1b3e3945ff0619a141e1708889a05f9f4dcf2003335b` |
| `catalog-api:v0.4.0` | `sha256:4889f1ce04568a335bdfe698e11a3c8133238e0b0b61c91447ea4448a3e3ae43` | `v0.2.0` — `sha256:b236b3ac805e4e92f7d9cc889d0e15d966a293d764af860d02c46b15efc9c5cb` |

`database-schema:v0.3.0` remained at
`sha256:6fba747ff353d6f4639b566a33ba73ab79515daaee756980704c88e2c6f32b1c`.
Each tag resolved to its recorded index digest; each index contained a
`linux/amd64` image manifest. The rollback tags were also re-resolved against
GHCR, not just copied from an older document.

Run command, from the issue worktree with an ignored, digest-pinned `.env`:

```sh
env -u DOCKER_DEFAULT_PLATFORM \
  SMOKE_MEDIA_PROJECT=groovemap-id-87i1-smoke4 \
  SMOKE_MEDIA_PROJECT_DIRECTORY=/Users/Robert/workspaces/github/groovemap-music/deployment \
  SMOKE_MEDIA_ENV_FILE="$PWD/.env" just smoke-media
```

The shared project directory was used only to resolve two read-only Compose
binds (`config/rabbitmq-enabled-plugins` and `config/otel-collector.yaml`),
whose SHA-256 values matched the issue worktree byte-for-byte. The Compose
files and `.env` came from the issue worktree. The smoke reported **15/15 PASS**,
including `GET /api/lookup/barcode/5%20012394%20144777` (printed value
`5 012394 144777`), normalized `5012394144777`, resolved to Discogs release
`999000001` and `gm_id` `01a0c525-38a2-70bf-845f-f6e193f6516c`.
`bh work check gm-deployment-87i.1` passed with 535 tests.

An earlier attempt with global `DOCKER_DEFAULT_PLATFORM=linux/amd64` exited
before probes because the pinned Neo4j image has no amd64 variant. The
successful run instead used the overlay's existing per-service amd64 settings
for internal images while infrastructure stayed native. The smoke's exit trap
removed each attempt's containers, volumes, and network. Label-filtered
`docker ps -a`, `docker volume ls`, and `docker network ls` returned no resources
for projects `groovemap-id-87i1-smoke2`, `groovemap-id-87i1-smoke3`, or
`groovemap-id-87i1-smoke4` after teardown. No live Compose project was changed.

## Post-import identity maintenance

`musicbrainz-sql-loader` attaches a MusicBrainz release, release group, artist, or label to
its Discogs counterpart's native id only when the Discogs alias already resolves; otherwise it
mints a separate native id, and no later reload heals the split. **The Discogs import must
finish — not merely start — before the MusicBrainz import starts**, on the first load and on
every cycle. A read-only measurement on the predecessor system's database (2026-09-25)
found 3,661,734 MusicBrainz entities naming a Discogs counterpart (releases 1,877,974, artists
1,272,211, release groups 347,129, labels 164,420): loading MusicBrainz first could split most
of them across two native ids, while loading Discogs first leaves about 23,409 whose Discogs
target is not yet in the dump, plus new drift every cycle, which the re-attachment job below
heals.

**First load.** Both extractors start extracting immediately when their container starts, and
[Per-source extractor cutover](#per-source-extractor-cutover) is explicit that the two ingest
concurrently with no cross-container ordering, lock, or mutual exclusion — bringing up the full
Compose stack in one `docker compose up` starts `extractor-musicbrainz` alongside
`extractor-discogs`. The MusicBrainz ingestion path is therefore not deployed at all for the
first load: bring up the stack without `extractor-musicbrainz` and its two consumers,
`brainzgraphinator` (MusicBrainz graph enrichment) and `brainztableinator` (MusicBrainz to
PostgreSQL). None of the three is a dependency of any other Compose service (each only depends
on shared infrastructure — RabbitMQ, PostgreSQL, Neo4j, `schema-init`), so leaving them out of
the service list is enough; nothing else in the stack fails to start because they are absent.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait \
  $(docker compose -f docker-compose.yml -f docker-compose.prod.yml config --services \
    | grep -vE '^(extractor-musicbrainz|brainzgraphinator|brainztableinator)$')
```

For a local (non-production) stack, drop the two `-f` flags; [Quick
start](quick-start.md#4-start-only-with-operator-approval) shows the production invocation
these follow.

"Extraction finished" is not "import finished": the extractor publishes to RabbitMQ
asynchronously, so the Discogs consumers (`graphinator`, `tableinator`) have to drain too. The
admin panel's extraction history only records admin-*triggered* runs, so it does not cover this
automatic first run — use the RabbitMQ management UI at <http://localhost:15672> (see
[Queue monitoring](monitoring.md#queue-monitoring)) or the admin panel's Queue Trends tab
instead, and confirm the Discogs consumer queues
(`groovemap-discogs-graphinator-*` and `groovemap-discogs-tableinator-*`) are at zero ready and
zero unacknowledged messages with their consumers still attached, not merely idle because
nothing is running. Only then deploy the MusicBrainz ingestion path:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait \
  extractor-musicbrainz brainzgraphinator brainztableinator
```

**Later cycles.** `extractor-discogs` and `extractor-musicbrainz` each then run on their own
periodic schedule (`PERIODIC_CHECK_DAYS`: 5 days and 3 days) with the same no-ordering
behavior, so nothing in the stack today keeps a later Discogs cycle ahead of the MusicBrainz
one it should precede — that is a known gap, not something this procedure enforces. What
follows instead heals the resulting drift after the fact: run the re-attachment job below once
the Discogs import for a cycle has completed and its consumer queues have drained, using the
same RabbitMQ signal as above (or, for an admin-triggered re-run, the extraction history row
reaching `completed`).

The re-attachment job itself is owned by `catalog-api`
([ADR 0014 section 8](https://github.com/groovemap-music/design/blob/main/docs/adr/0014-cross-catalog-edition-candidates.md#8-split-linked-items-catalog-re-attachment-outside-the-matcher),
amended
[2026-09-25](https://github.com/groovemap-music/design/blob/main/docs/adr/0014-cross-catalog-edition-candidates.md#2026-09-25-dependents-guard-replaced-by-the-native-id-merge)).
Then run the `gm_id` projection
([ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md),
amended
[2026-09-25](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md#2026-09-25-superseded-catalog-items-and-native-id-merge)).
See
[`catalog-api`'s README](https://github.com/groovemap-music/catalog-api/blob/main/api/README.md#re-attaching-load-order-split-catalog-items)
for the job's full rule, guards, and lock order.

1. Dry run the census first:

   ```bash
   docker exec groovemap-api catalog-identity-reattach
   ```

   Read the printed per-kind census before applying: `split` and `stale_gm_item_id` items,
   `eligible` (candidates minus guarded), `guarded` by reason, `dependents` by table, and
   `identifier_aliases` (held by candidates, and how many are `contested` — also held by the
   Discogs record).
2. Apply the repair, naming an existing active admin (see
   [Creating an Admin Account](admin-guide.md#creating-an-admin-account)):

   ```bash
   docker exec groovemap-api catalog-identity-reattach --apply --admin-id <admin-uuid>
   ```

   Alternative: `POST /api/admin/identity/reattach` (dry run by default; add `?apply=true` to
   write). Both return `202` with a job id and log the census and, for an applying run, the
   per-kind outcomes.
3. Then project `gm_id` onto Neo4j so the graph follows the alias table:

   ```bash
   docker exec groovemap-api catalog-identity-projection
   ```

   Alternative: `POST /api/admin/identity/project`.

Run this sequence after the first full load and after every monthly Discogs import.
MusicBrainz publishes twice weekly and Discogs monthly, so new MusicBrainz-to-Discogs links
keep arriving ahead of their Discogs targets between Discogs cycles.

**Automatic alternative.** Set `IDENTITY_AUTO_REATTACH_ENABLED=true` on the `api` service
(`groovemap-api`) — in `docker-compose.yml`'s `api` environment block, overridable in
`docker-compose.prod.yml`'s — and `catalog-api` runs the re-attachment (apply) and then the
`gm_id` projection by itself, once per completed Discogs extraction, detected the same way as
above from `loader_extraction_latch`. `IDENTITY_AUTO_REATTACH_INTERVAL` (seconds, default 300)
sets how often it polls; the watcher is off by default and idles until the relation exists.
Every replica polls, but a PostgreSQL advisory lock and a handled marker in `admin_audit_log`
(`action = identity.reattach.auto`; a failed run is `identity.reattach.auto.failed` and is
retried) keep a run to exactly once per extraction. The first-load staged deploy and the
MusicBrainz-after-Discogs cycle order above still apply — the switch only replaces the manual
`catalog-identity-reattach` / `catalog-identity-projection` runs above, not the ordering they
depend on. See
[`catalog-api`'s README](https://github.com/groovemap-music/catalog-api/blob/main/api/README.md#running-both-automatically-after-each-discogs-import)
for the full design.

Operator notes:

- Safe to re-run: a second run reports the already-repaired and already-guarded items as
  `unchanged`.
- The exit code reflects only run-level failure. Read the per-item `reattached`, `guarded`,
  `unchanged`, and `failed` counts from the printed report, not the exit code.
- A guarded item — a split native id holding a `discogs` alias, another row's alias, or an
  alias whose source is not `catalog` (guard reasons `shared_native_id` and
  `non_catalog_alias`) — is a real item, not a load-order orphan, and is not a failure to
  retry. Dependents no longer guard: since catalog-api main `e061f7d`, a dependent
  (`artifacts`, `owned_copies`, and what hangs off them, or a `user_collections` /
  `user_wantlists` row) is merged into the survivor instead — moved, ledgered in
  `catalog_item_moves`, and reversible — and the census reports it under `will_move`, not
  `guarded`.

## Media-aware loader upgrade

Promoting the media-aware SQL loader and graph enricher images does not populate
the ADR 0007 canonical `media` block for data that is already stored. Both
release upsert paths gate the rewrite on a changed content hash, and a
re-ingested release that has not changed upstream hashes identically, so
pre-existing rows keep `media` NULL and their release nodes gain no `Medium`,
`MediaFamily`, or `ISSUED_ON`. Nothing backfills them on its own.

After promoting the media-aware images, backfill deliberately:

1. Confirm the promoted `discogs-sql-loader` carries the media backfill fix
   (bead `gm-discogs-sql-loader-3ce.1`). Without it the Discogs path still skips
   hash-identical releases and their `media` stays NULL no matter how often the
   extractor republishes them. The MusicBrainz loader's release upsert always
   writes `media = EXCLUDED.media`, so that source needs no loader change.
2. Record the current row counts of `releases` and `musicbrainz.releases` with a
   non-NULL `media`, as the before figure for the backfill.
3. Obtain approval: a full reprocess republishes every record, so it is a live
   state change with real broker, loader, and store load.
4. Trigger a **force_reprocess** extraction on **both** extractors, one source
   at a time, from the admin panel (see [Admin guide](admin-guide.md)). Forcing
   the run is what matters: it reprocesses every file regardless of the existing
   state markers, so the media-aware loaders and enrichers see each pre-existing
   release again and write its media block.
5. Verify the backfill: `releases.media` and `musicbrainz.releases.media` are
   non-NULL for a sample of pre-upgrade releases, the counts from step 2 have
   risen to the expected totals, and Neo4j carries `Medium`, `MediaFamily`,
   `IN_FAMILY`, `ISSUED_ON`, and `Release.media_families` for those releases.

Run one source's force_reprocess to completion before starting the other, so a
failure is attributable and the retry budget is not spent twice at once.

## Identifier lookup smoke prerequisites

`just smoke-media` asserts two records against one published release event. The
[ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md)
media block reaching both stores is the first, and the catalog-identifiers path
of
[ADR 0011](https://github.com/groovemap-music/design/blob/main/docs/adr/0011-catalog-identifiers-and-manufacturing-credits.md)
is the second: the event's `identifiers` and `companies` blocks and its country
have to become a `provider_aliases` row keyed on the release's native id, a
`CREDITED_TO` edge to a `Company` node, a `Release.country` property, and an
answer from `GET /api/lookup/barcode/{value}`.

The required identifier-capable images are now recorded in
[Recorded release digests](#recorded-release-digests). Run `just smoke-media`
against an untracked digest-pinned environment file in its disposable project
to verify the probes before promoting any image to a live environment.

| Wave | Variable | What the probes need from it |
| --- | --- | --- |
| 2 | `DATABASE_SCHEMA_IMAGE` | The `data->'identifiers'` and `data->'companies'` GIN indexes, and the `Release.country` range index. |
| 2 | `DISCOGS_SQL_LOADER_IMAGE` | Minting the `barcode`, `catalog_number`, and `matrix` aliases against the release's `gm_item_id`. |
| 2 | `DISCOGS_GRAPH_ENRICHER_IMAGE` | The `Company` nodes, the `CREDITED_TO` edges, and `Release.country`. |
| 3 | `CATALOG_API_IMAGE` | `GET /api/lookup/{provider}/{value}` for the `barcode` and `catalog_number` namespaces. |

Two things this run does **not** need. It needs no producer image: it publishes
the producers' promoted contract fixtures itself rather than extracting a dump,
so the identifiers and companies blocks it asserts are the ones the promoted
fixture in `config/media-smoke/` carries, recorded in `config/provenance.json`.
And it needs no MusicBrainz-side change: ADR 0011's MusicBrainz work writes
`mb_country` on releases the Discogs enricher already created, which these
probes do not assert.

The run publishes the catalog API on `127.0.0.1:18005` for the duration, because
the lookup hop is the only thing that shows a minted alias actually resolves.
Override the port with `SMOKE_MEDIA_API_PORT` when 18005 is taken. As with every
smoke recipe here, the stack is disposable, runs under its own Compose project
name, and is destroyed with its volumes on exit; recording a digest as reviewed
is not an instruction to deploy it.

## Activity partitions and retention

`catalog-api` owns partition creation for the two append-only, month-partitioned
tables [ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md)
adds, `activity.events` and `activity.impressions`. At startup it ensures the
current and next calendar month's partitions exist for both tables
(`api.activity.ensure_startup_partitions`), and on every write it ensures the
partition for that event's or impression's own month
(`api.activity._ensure_partition`); both call the
`activity.ensure_month_partition` function `database-schema` declares, so a
write into a month with no partition creates it rather than failing or landing
in the `_default` partition. No scheduler is involved: deployment has no
periodic-job primitive today, and partition creation needs none.

### Retention is deferred

ADR 0010 explicitly defers retention periods per table and per purpose, and the
partition-drop schedule that would implement them, to a future decision. Until
that decision is filed and a bead cuts it, no partition is dropped on a
schedule, and none should be dropped manually except by operator approval under
the procedure below.

### Manual partition drop procedure

Once a retention period is decided, or a partition needs manual removal for
another operator-approved reason:

1. Identify the month partition by name: `activity.events_y<YYYY>m<MM>` or
   `activity.impressions_y<YYYY>m<MM>` — the format
   `activity.ensure_month_partition` builds, for example
   `activity.events_y2026m01`.
2. Verify the partition carries no retention hold: confirm the month is fully
   outside the approved retention period and that no open erasure, export, or
   legal-hold request references data in it.
3. Obtain operator approval for the exact partition and environment.
4. `DROP TABLE activity.<table>_y<YYYY>m<MM>;`. This is a DDL statement, not a
   row-level `DELETE`, so the `activity_events_reject_mutation` /
   `activity_impressions_reject_mutation` immutability trigger
   (`BEFORE UPDATE OR DELETE ... FOR EACH ROW`, declared in
   `database-schema`'s `postgres.py`) does not fire and does not block it: the
   trigger guards row-level mutation of live rows, and dropping a whole
   partition table is a schema operation it was never declared to intercept.
   Partition-level removal, not row deletion, is the retention mechanism ADR
   0010 describes.
5. Record the dropped partition, its date range, its row count if known, and
   the approval in the maintenance record.

This procedure is independent of the per-user erasure path (see
`just smoke-erasure` below): erasure hard-deletes a single subject's rows under
a session-local trigger bypass regardless of retention status, while a
partition drop removes a whole month for every subject once retention allows
it.

## Update an infrastructure image

PostgreSQL, Neo4j, RabbitMQ, and Redis images are declared directly in
`docker-compose.yml` with a readable tag and immutable digest. An update should
include:

- upstream release-note and compatibility review;
- backup and restore validation appropriate to the data store;
- updated tag and matching manifest digest;
- `just check` results;
- a documented rollback image digest;
- an approved maintenance window for the live change.

Major database upgrades may require a purpose-built migration plan. A passing
Compose render does not prove on-disk compatibility.

RabbitMQ 4.3 dropped the Mnesia metadata store: a node whose `rabbitmq_data`
volume was created under an earlier 4.x release must have every feature flag
from 4.0 through 4.2 enabled (`rabbitmqctl enable_feature_flag all`, which
includes `khepri_db`) before its image is moved to a 4.3 digest. A fresh volume
needs nothing. Redis 8 loads 7.x AOF and RDB files unchanged, but the upstream
licence changed to a tri-licence (RSALv2, SSPLv1, or AGPLv3) with 8.0.

## Secrets

`just secrets-bootstrap` creates missing local files but deliberately does not
overwrite existing values. Rotation is an operator procedure:

1. inventory every producer and consumer of the secret;
2. confirm whether dual-key overlap is supported;
3. back up the current approved secret store;
4. generate and stage the replacement without printing it;
5. obtain approval for the exact environment;
6. roll dependent services in a safe order;
7. verify behavior and revoke the old credential;
8. remove temporary local material.

Never commit `.env`, `secrets/`, Docker authentication, or copied production
configuration.

## Backups and restore drills

Backups are environment-specific and must be tested, not merely scheduled.
Record:

| Item | Evidence |
| --- | --- |
| PostgreSQL backup | Tool/version, timestamp, size, checksum, retention location |
| Neo4j backup | Tool/version, timestamp, size, checksum, retention location |
| Restore drill | Isolated target, duration, integrity checks, operator |
| Recovery objectives | Measured RPO and RTO against the agreed targets |

Do not store backup archives in this repository. See
[Database resilience](database-resilience.md) for failure-mode planning.

## One-time data migrations

The retained scripts default to read-only counts and require `--apply` to
mutate data:

```bash
scripts/cleanup-implausible-years.sh
scripts/compute-label-stats.sh
scripts/migrate-master-year-to-int.sh
```

Before approval, review the script, target, backup, affected-record count,
expected duration, validation query, and rollback. CI never invokes mutation.

## Promoted extraction rules

`discogs-ingestion` owns the editable extraction rules. ADR 0005 assigned this
ownership to it, retiring the combined `catalog-ingestion` repository. Deployment
carries the runtime copy and `config/provenance.json`, which records the producer
commit and source/promoted hashes. Update them together from a reviewed producer
commit; `scripts/check-images.py` rejects a mismatched promoted hash.

## Environment changes

The following operations require explicit approval because they change live
state:

- `just smoke`, `just smoke-infra`, `just smoke-media`, `just smoke-erasure`, `just smoke-released`, `just smoke-released-fixture`, or `just down`;
- `docker compose up`, `restart`, `stop`, `down`, or `scale`;
- database restore, vacuum policy changes, queue deletion, or cache flush;
- data migration with `--apply`;
- a `force_reprocess` extraction trigger, including the media backfill above;
- secret rotation;
- performance testing.

Capture a pre-change snapshot, exact commands, image digests, validation
results, and rollback outcome in the maintenance record.

`just smoke-erasure` starts its own disposable Compose stack (own project
name, own subnet), registers a throwaway account, exports and erases it, and
asserts the ADR 0010 export and deletion claims hold across PostgreSQL,
Neo4j, and Redis before tearing the stack and its volumes down. It needs an
operator-approved `.env` with every `*_IMAGE` variable pinned to an approved
digest, the same requirement `just smoke-media` has. See
[The erasure and export assertion](testing-guide.md#the-erasure-and-export-assertion)
for what it seeds, asserts, and its optional `SMOKE_ERASURE_*` knobs.
`just check` and CI never run it.

## Review checklist

- [ ] Repository gate passes.
- [ ] Every internal image uses its owning repository name and a manifest digest.
- [ ] Infrastructure images remain digest-pinned.
- [ ] Base and production Compose renders were reviewed.
- [ ] No secret, `.env`, auth, volume, backup, or performance artifact is staged.
- [ ] Backup and rollback evidence exists for stateful changes.
- [ ] The operator approved the exact target and action.
- [ ] Post-change health and application behavior were verified.
