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

First per-source producer images (`v0.2.1`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DISCOGS_INGESTION_IMAGE` | `ghcr.io/groovemap-music/discogs-ingestion` | `sha256:4a961aab647bb830074414b30e121d927c8287d2a1b2e4d61a34f42a1b50e94b` |
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

- `just smoke`, `just smoke-infra`, or `just down`;
- `docker compose up`, `restart`, `stop`, `down`, or `scale`;
- database restore, vacuum policy changes, queue deletion, or cache flush;
- data migration with `--apply`;
- secret rotation;
- performance testing.

Capture a pre-change snapshot, exact commands, image digests, validation
results, and rollback outcome in the maintenance record.

## Review checklist

- [ ] Repository gate passes.
- [ ] Every internal image uses its owning repository name and a manifest digest.
- [ ] Infrastructure images remain digest-pinned.
- [ ] Base and production Compose renders were reviewed.
- [ ] No secret, `.env`, auth, volume, backup, or performance artifact is staged.
- [ ] Backup and rollback evidence exists for stateful changes.
- [ ] The operator approved the exact target and action.
- [ ] Post-change health and application behavior were verified.
