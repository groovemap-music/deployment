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
changed both `catalog-api` and `database-schema`, but neither has a promoted
image carrying that work yet. The recorded
`CATALOG_API_IMAGE` digest in `scripts/check-images.py` is `catalog-api`
`v0.1.1`, and the recorded `DATABASE_SCHEMA_IMAGE` digest in
[Recorded release digests](#recorded-release-digests) is `database-schema`
`v0.2.0`; both tags predate the identity and activity changes and are stale
relative to them.

Promoting either service to a build that includes this work is a separate
release step, not part of this documentation change: the owning repository
cuts a new `v*` tag under its own release approval, and the
[Promote a service release](#promote-a-service-release) procedure above
resolves its digest and updates the environment `.env`. `scripts/check-images.py`'s
`RELEASED_IMAGE_DIGESTS` and this file's recorded-digest tables are updated
from that reviewed release, not before.

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

Matching consumer and schema images (`v0.2.0`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DATABASE_SCHEMA_IMAGE` | `ghcr.io/groovemap-music/database-schema` | `sha256:35e1ef9fbd7506dd67f93f6733dbf689ac5f1bda4f2b7ff24859b8a2115218de` |
| `DISCOGS_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/discogs-sql-loader` | `sha256:dfa00f9ee24d9fab6212b02a272486f70490b741e9556edf0b2fd2c793f3393c` |
| `DISCOGS_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/discogs-graph-enricher` | `sha256:933df432732e8f1b863f1b3e3945ff0619a141e1708889a05f9f4dcf2003335b` |
| `MUSICBRAINZ_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-sql-loader` | `sha256:cab35264260d6df0e3a86e2022ed3a6b02506b8404aa845921ff7ec18605b027` |
| `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-graph-enricher` | `sha256:541cc5ef9823a970a44af2952e641a6c925011e1d653274e419fbfc72df62b6e` |

These are records of what was published, not an instruction to deploy. Verify a
digest against the registry before promoting it, and re-resolve it for any
platform other than the one the release workflow published.

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
