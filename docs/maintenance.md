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

`catalog-ingestion` owns the editable extraction rules. Deployment carries the
runtime copy and `config/provenance.json`, which records the producer commit and
source/promoted hashes. Update them together from a reviewed producer commit;
`scripts/check-images.py` rejects a mismatched promoted hash.

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
