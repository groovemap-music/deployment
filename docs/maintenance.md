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

The October 2026 console, broker-stats, trigger-source and health-port releases
are coupled and have their own ordering constraints; release and roll them out
with [the October 2026 operations console rollout](rollout-2026-10-operations-console.md).

### Paired release floors

`PAIRED_IMAGE_FLOORS` in `scripts/check-images.py` ties releases that only work together, and
`just check` refuses a reviewed set that breaks a pair. One row is `discogs-sql-loader` `0.5.0`, the first
release that writes `purge_vetoed` (`gm-discogs-sql-loader-gna2`), with `database-schema` `0.8.0`, the first
that has the column. The other is `musicbrainz-sql-loader` `0.6.0` with `database-schema` `0.9.0`: that loader
writes `purge_vetoed` and its hash `ON CONFLICT` target is the only key from `v0.9.0`. Without that schema the loader declines the latch and reports degraded. Filling in
only one of the two is refused; `UNRELEASED` on both leaves a pair inert.

## Wave-2 image promotion (identity and activity)

The native-identity and first-party-events program
([ADR 0009](https://github.com/groovemap-music/design/blob/main/docs/adr/0009-native-identity-and-provider-aliases.md),
[ADR 0010](https://github.com/groovemap-music/design/blob/main/docs/adr/0010-first-party-events-consent-and-deletion.md))
changed both `catalog-api` and `database-schema`. Both releases are now cut:
`database-schema` `v0.3.0` and `catalog-api` `v0.2.0` first carried the identity and
activity work. `RELEASED_IMAGE_DIGESTS` in `scripts/check-images.py` now records
`database-schema` `v0.6.0` and `catalog-api` `v0.6.0`, and those manifest digests are
recorded as reviewed there and in
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

Reviewed per-source producer images (Discogs `v0.4.0`, MusicBrainz `v0.3.0`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DISCOGS_INGESTION_IMAGE` | `ghcr.io/groovemap-music/discogs-ingestion` | `sha256:881d8f96842e17fa9dc3f2d43fab1a18abcee664e5bc2ec0374b45f09cceaa8d` |
| `MUSICBRAINZ_INGESTION_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-ingestion` | `sha256:7599975c51429dc5bbfa30a9e55a28bed6fd1afef8f3a9a5dfd7c0286d4da89a` |

Matching consumer and schema images (Discogs SQL loader `v0.4.5` and graph enricher `v0.6.1`;
MusicBrainz graph enricher `v0.4.0` and SQL loader `v0.5.0`; `database-schema` `v0.7.0`; see
[the reset release set promotion](#promotion-of-the-reset-release-set-2026-11-05)):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `DATABASE_SCHEMA_IMAGE` | `ghcr.io/groovemap-music/database-schema` | `sha256:da6e53bb865f3172161e07fcca34d8fecef587facdf566ae6efff378e9ebc771` |
| `DISCOGS_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/discogs-sql-loader` | `sha256:dc74e541b444a943f0e155fc07dd779047ab74f892e9074478eee954e0fe434f` |
| `DISCOGS_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/discogs-graph-enricher` | `sha256:0e24f5dc94a6f5d58da35cfe351b877f137e6d928f0b485c4eb64ca22513c1ee` |
| `MUSICBRAINZ_SQL_LOADER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-sql-loader` | `sha256:2de906859c354f5a69ffa3eca20febe51fa4eb043857c3b6b83287b67c63b809` |
| `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE` | `ghcr.io/groovemap-music/musicbrainz-graph-enricher` | `sha256:e9fb87302678e4d8b3f681f9db4a509f2ed0eba0fb2378a8da18f846fd979057` |

Reviewed catalog API image (`v0.7.0`):

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `CATALOG_API_IMAGE` | `ghcr.io/groovemap-music/catalog-api` | `sha256:143d5dd329adfe42a2ba9fe8785df45430603e07aac807af0cc8471263ec927a` |

Reviewed console (`v0.3.0`), explorer (`v0.3.2`) and analytics (`v0.6.0`) images:

| Variable | Image | Manifest digest |
| --- | --- | --- |
| `OPERATIONS_CONSOLE_IMAGE` | `ghcr.io/groovemap-music/operations-console` | `sha256:1d9e480b0404434a48e394d7e062d3f54eff25ac48a6a8d45ba015833b11d691` |
| `GRAPH_EXPLORER_IMAGE` | `ghcr.io/groovemap-music/graph-explorer` | `sha256:da5b49f70a2d8fdd0c6254e6cf9de2b7c20da79ce90d8922a8349aff068a9fb8` |
| `ANALYTICS_ENGINE_IMAGE` | `ghcr.io/groovemap-music/analytics-engine` | `sha256:000a053102c52f377a9f6ff2edd492c205fbb1973d55fcad168ecc7abb81d031` |

`scripts/check-images.py`'s `RELEASED_IMAGE_DIGESTS` is the authority for every entry above.

The 2026-10-31 release review resolved its changed entries from their released
tags for `linux/amd64` (the platform published by these workflows). At that
time, the recorded rollback targets were the previous index digests:
`DISCOGS_SQL_LOADER_IMAGE`
`sha256:dfa00f9ee24d9fab6212b02a272486f70490b741e9556edf0b2fd2c793f3393c`,
`DISCOGS_GRAPH_ENRICHER_IMAGE`
`sha256:933df432732e8f1b863f1b3e3945ff0619a141e1708889a05f9f4dcf2003335b`,
and `CATALOG_API_IMAGE`
`sha256:b236b3ac805e4e92f7d9cc889d0e15d966a293d764af860d02c46b15efc9c5cb`.
Later image promotions below record their current rollback pins.

These are records of what was published, not an instruction to deploy. Verify a
digest against the registry before promoting it, and re-resolve it for any
platform other than the one the release workflow published.

### Promotion of the lagging service images (2026-10-31)

Completes the [October 2026 operations console rollout](rollout-2026-10-operations-console.md)
in the reviewed set. Every digest was resolved from its released tag on GHCR and verified with
`gh attestation verify oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` (exit 0 for all eight images in this section). Each tag is the latest published version of
its repository.

| Image | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `musicbrainz-graph-enricher:v0.4.0` | `sha256:e9fb87302678e4d8b3f681f9db4a509f2ed0eba0fb2378a8da18f846fd979057` | `v0.2.0` — `sha256:541cc5ef9823a970a44af2952e641a6c925011e1d653274e419fbfc72df62b6e` |
| `musicbrainz-sql-loader:v0.5.0` | `sha256:2de906859c354f5a69ffa3eca20febe51fa4eb043857c3b6b83287b67c63b809` | `v0.2.0` — `sha256:cab35264260d6df0e3a86e2022ed3a6b02506b8404aa845921ff7ec18605b027` |
| `operations-console:v0.2.0` | `sha256:61656dfdae0d82d9b413e19404ab29c9ca83cc320e52529b0d69ad1c47736fd0` | `v0.1.1` — `sha256:fa771bc34f5ed69a028587ae62095268301a774426ef90ee18c0b18f2e5f59b1` |
| `discogs-ingestion:v0.4.0` | `sha256:881d8f96842e17fa9dc3f2d43fab1a18abcee664e5bc2ec0374b45f09cceaa8d` | `v0.3.1` — `sha256:db418bfc97d2d364ac0e64045b492ad8492b500c84f8ce105a04cadd400ee17c` |
| `musicbrainz-ingestion:v0.3.0` | `sha256:7599975c51429dc5bbfa30a9e55a28bed6fd1afef8f3a9a5dfd7c0286d4da89a` | `v0.2.1` — `sha256:2b348519450cc9811fe8d194d0ef4b4dd3ead901b2f8e5883dec83a839bd9b37` |
| `graph-explorer:v0.2.2` | `sha256:edbbd476a2d324443453dd4045606350bb6ce3bf7a5bc77c357e8801cc3a0afa` | `v0.1.1` — `sha256:546e3823b811eb9d912c175a28a56153a72c95107089714393b3c21551f6e33b` |

At this 2026-10-31 snapshot, `discogs-graph-enricher` and `discogs-sql-loader` were still at
`v0.3.0` (then the latest tags). Their later `v0.4.0` promotions and gate evidence are recorded
below.
At that snapshot, commits `4d70985` and `204bc85` (graph enricher) and `6b8fa03` and `5faa9b5`
(SQL loader), the rollout's `HEALTH_PORT` override and exit-1 changes, were not in a release tag.
The subsequent v0.4.0 promotions below include the consumer changes. Rollout constraint O5 meant
those earlier releases were not coupled to anything.

Rolling the MusicBrainz consumers or the console back is not independent: they are coupled to the
Compose health ports and to the console's credentials (rollout constraints T1 and T2). Use the
[rollout's coupled-set rollback](rollout-2026-10-operations-console.md#roll-back-the-coupled-set-approval),
not a lone digest swap.

Disposable smokes ran on unmodified Compose files with an arm64 host's emulation only (internal
images as `linux/amd64`, `DOCKER_DEFAULT_PLATFORM` unset), the reviewed `.env` from
`just env-init`, and unique project names:

- `just smoke-released` (`gm-lqar-released`): exit 0, every service healthy, clean stop, and no
  `ACCESS_REFUSED` in any log. With the promoted images the brainz healthchecks on `8001` and
  `8002` pass, and the console starts without AMQP credentials.
- `just smoke-media` (`gm-lqar-media`): 15/15 PASS.
- `just smoke-erasure` (`gm-lqar-erasure`): 32/32 PASS.
- `just smoke-released-fixture` (`gm-lqar-fixture`, `discogs-ingestion` `v0.4.0` still ships the
  v1 extractor-smoke contract): 7/7 PASS.
- Teardown: label-filtered `docker ps -a`, `docker volume ls` and `docker network ls` returned no
  resources for any `gm-lqar-*` project.

### Promotion of catalog-api v0.6.0, analytics-engine v0.5.0 and database-schema v0.6.0 (2026-10-31)

Each digest was resolved from its released tag on GHCR (the index digest, with a `linux/amd64`
image manifest) and verified with `gh attestation verify oci://ghcr.io/groovemap-music/<repo>@<digest>
--owner groovemap-music --signer-repo groovemap-music/automation` (exit 0 for all three). The
unattested `database-schema` build `sha256:c5e65477…` was not used.

| Image | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `catalog-api:v0.6.0` | `sha256:b41e01f57c7c3ac0f52fc26474145508b1755bb6275a08f0a2e41931a9c437d5` | `v0.4.0` — `sha256:4889f1ce04568a335bdfe698e11a3c8133238e0b0b61c91447ea4448a3e3ae43` |
| `analytics-engine:v0.5.0` | `sha256:bf1a3944a9f0b2d2a85f9470423ce0422e4433acb585ff3fd1b4e0763f116bfa` | `v0.1.1` — `sha256:a50a9eb79f58f463f287de379d6a87b68c39c3df84b8aa6a7c80f93294210c69` |
| `database-schema:v0.6.0` | `sha256:a16e4a2c5139e065a6e2a71390279732b349e58347aab8bdef33aa6e0f9b1465` | `v0.5.0` — `sha256:06fcc517d7f41075eff4d7ab202dd928b9069abc0bc39fabead4740672197fda` |

The rollback digests are the previous pins, re-resolved against GHCR. `catalog-api` `v0.6.0`
namespaces every Valkey key under `VALKEY_KEY_PREFIX` (default `groovemap:`) and bounds the upgrade
from unprefixed keys with `VALKEY_LEGACY_KEYS` (default `read`, a 24-hour window), so rolling back
to `v0.4.0` strands the prefixed keys it wrote (cache entries and revocations expire on their own).

Compatibility with the pinned loaders and enrichers: `database-schema` `v0.5.0` to `v0.6.0`
changes no DDL. Its diff touches `src/groovemap_schema/{initializer,postgres}.py` only to add the
`POSTGRES_PREPROVISIONED` mode (the dedicated path is unchanged) and bumps `msgpack`;
`contracts/persistence/v1/compatibility.json` changes only `application_runtime.tested_commit`. The
discogs and musicbrainz SQL loaders and graph enrichers therefore see the schema they were
reviewed against.

The promotion also sets `IDENTITY_AUTO_REATTACH_ENABLED: "true"` on the `api` service and flips
`EXTERNAL_STORE_FLOORS` to database-schema 0.6.0 (PostgreSQL) and catalog-api 0.6.0 with
analytics-engine 0.5.0 (Valkey), so the external-stores mode is supported.

Disposable smokes ran on an arm64 host with the internal images emulated as `linux/amd64`
(`DOCKER_DEFAULT_PLATFORM` unset), the reviewed `.env` from `just env-init`, and a unique project
name each (`gm-ew9k-released5`, `gm-ew9k-media`, `gm-ew9k-erasure2`):

- `just smoke-released`: exit 0 for the whole stack, including `schema-init` twice (idempotent DDL),
  every application service healthy, and a clean `stop`. It needed three local-only adjustments
  that this repository does not carry, because the pinned MusicBrainz and console images predate
  the Compose file: the `brainzgraphinator` and `brainztableinator` healthchecks probe `8011` and
  `8010` (the Compose file probes `8001` and `8002`), the console needs `RABBITMQ_HOST`,
  `RABBITMQ_USERNAME` and `RABBITMQ_PASSWORD` (it otherwise logs `ACCESS_REFUSED` as
  `discogsography`), and a longer `start_period` for emulation. None involves a promoted image;
  promoting those lagging images (`gm-deployment-lqar`) removes the need.
- `just smoke-media`: 15/15 PASS, including the barcode lookup through `catalog-api` `v0.6.0`.
- `just smoke-erasure`: 32/32 PASS. The first run failed `valkey recommendation keys present`
  (9/10) because `catalog-api` `v0.6.0` writes `groovemap:recommend:…`, so the smoke's unprefixed
  `recommend:*<user>*` pattern and seeded `snapshot:usercount:` key matched nothing the erasure
  sweeps. `scripts/smoke_erasure.py` now carries the `groovemap:` prefix.
- Teardown: each exit trap removed its stack; label-filtered `docker ps -a`, `docker volume ls` and
  `docker network ls` returned no resources for any `gm-ew9k-*` project. No other Compose project
  was touched.

### Promotion of graph-explorer v0.3.0 and operations-console v0.3.0 (2026-10-31)

Each digest was resolved from its released tag on GHCR and verified with `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` (exit 0 for both; signer `reusable-release.yml`, built from each
repository's `refs/tags/v0.3.0`).

| Image | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `graph-explorer:v0.3.0` | `sha256:ad1c3b6a7e21beb9393fca5057050ff883475a72972ee11e8c7eed5863b5d31e` | `v0.2.2` — `sha256:edbbd476a2d324443453dd4045606350bb6ce3bf7a5bc77c357e8801cc3a0afa` |
| `operations-console:v0.3.0` | `sha256:1d9e480b0404434a48e394d7e062d3f54eff25ac48a6a8d45ba015833b11d691` | `v0.2.0` — `sha256:61656dfdae0d82d9b413e19404ab29c9ca83cc320e52529b0d69ad1c47736fd0` |

Evidence:

- The published-image theme test for the canonical Explore theme passed against these images.
- The combined-pin Linux run and unmodified released, media, erasure, and released-fixture smoke
  evidence are recorded once under [the Discogs graph-enricher v0.4.0 promotion](#promotion-of-discogs-graph-enricher-v040-2026-11-01).
  That run used the reviewed console and Explore v0.3.0 pins in this section and inspected the
  enabled runtime services as healthy.
- The console's own test pins its default `*_MODE` and `*_URL` targets to this deployment's, so a
  before/after platform-health comparison against v0.2.0 was not run.

`CONSOLE_TARGETS_FLOOR` keeps the reviewed console minimum at v0.3.0. When an external-monitoring
overlay omits local exporters, an older console ignores the configured external targets and
`just deploy` reports a warning; it does not stop the stack.

Rolling back is a pin change to the rollback digest above for the affected image alone.

### Promotion of discogs-sql-loader v0.4.0 (2026-10-31)

The digest was resolved from the released tag on GHCR and verified with `gh attestation verify
oci://ghcr.io/groovemap-music/discogs-sql-loader@sha256:be15077013672f69384bd75fd591801bdabd80400eaa2fd4a686e2d82f6bbb0b
--owner groovemap-music --signer-repo groovemap-music/automation` (exit 0).

| Image | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `discogs-sql-loader:v0.4.0` | `sha256:be15077013672f69384bd75fd591801bdabd80400eaa2fd4a686e2d82f6bbb0b` | `v0.3.0` — `sha256:09f55827f972ec289baad7128acca061739fd9d4d350f23f3d6d22afeafee7e6` |

Why: `v0.3.0` never writes `public.loader_extraction_latch`, so the Discogs gate cannot complete.
`v0.4.0` writes it.

Compatibility: every relation `v0.4.0` requires is declared by `database-schema` `v0.5.0` and
`v0.6.0`, so the pinned `v0.6.0` schema needs no change (evidence: the discogs-sql-loader release
bead `gm-discogs-sql-loader-a8z0`).

Evidence:

- Diagnostic Linux run [41338188034](https://github.com/groovemap-music/deployment/actions/runs/41338188034)
  used this SQL v0.4.0 digest with graph-enricher v0.3.0. It proved the SQL latch was 4/4 durable
  with `refreshed_at`, but the overall gate remained closed because the graph marker was only 2/4.
  This is diagnostic evidence, not a green run of both promoted consumers.

Rolling back is a pin change to the rollback digest above for this image alone.

### Promotion of discogs-graph-enricher v0.4.0 (2026-11-01)

The published tag was built from the approved source commit
`1cf8c3cbd2401cc4b434dc80f17d5d29ab17c6f5` in release run
[41343109766](https://github.com/groovemap-music/discogs-graph-enricher/actions/runs/41343109766).
Its manifest digest was verified with `gh attestation verify
oci://ghcr.io/groovemap-music/discogs-graph-enricher@sha256:6f5dd84d18aa69cc40705bbabde269cefc7d7642e0818f1db2fba2588bde9c39
--owner groovemap-music --signer-repo groovemap-music/automation` (exit 0).

| Image | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `discogs-graph-enricher:v0.4.0` | `sha256:6f5dd84d18aa69cc40705bbabde269cefc7d7642e0818f1db2fba2588bde9c39` | `v0.3.0` — `sha256:e95fabb7633859c94e0913f9122ccbaed18a04a017c486886c38840c230ff80d` |

The published artifact was pulled and inspected for `linux/amd64`: OCI version `v0.4.0`, revision
`1cf8c3cbd2401cc4b434dc80f17d5d29ab17c6f5`, source
`https://github.com/groovemap-music/discogs-graph-enricher`, and image user `1000:1000` all match
the release. Source CI run
[41342658276](https://github.com/groovemap-music/discogs-graph-enricher/actions/runs/41342658276)
passed 567 unit tests and 14 real-Neo4j integration tests; 9 race regressions fail against the
old implementation.

The earlier Linux run [41338188034](https://github.com/groovemap-music/deployment/actions/runs/41338188034)
proved the SQL v0.4.0 latch was 4/4 durable and refreshed, but exposed the independent graph v0.3.0
race: only 2/4 graph completion markers persisted even though the relevant queues drained.

The real combined-pin Linux smoke [41343620122](https://github.com/groovemap-music/deployment/actions/runs/41343620122)
passed on deployment commit `fd649d58e9dbcf954842c1e03ffede1134be00ff` in 725 seconds. Its report
had 29 passing assertions and 77 stack checks. Both v0.4.0 consumers received all four genuine
fixture completion messages for version `20000101` at `2026-11-01T19:57:56Z`; the SQL latch row
durably contained all four signals (`artists`, `labels`, `masters`, `releases`; generation 1) with
`refreshed_at=2026-11-01T19:58:03.492945+00:00`, all four Neo4j graph markers were durable, and
all eight queues drained. The gate moved from exit 3 to `COMPLETE` exit 0 at
`2026-11-01T19:58:16Z`. The three MusicBrainz services were then enabled and each inspected as
running and healthy using the documented producer stand-in; this verifies phase-2 enablement, not
a full MusicBrainz import. No completion rows were inserted manually, the fixture was not
serialized, and the gate logic was unchanged.

The combined run also passed its negative cases: external-store off-network, wrong-CA, plaintext,
and override-rewiring refusals; UID 59000 refusal to read a mode-0600 secret; invalid collector
configuration unhealthy; failed DLQ policy initialization blocking seven dependents; and RabbitMQ
readiness failing with the app stopped despite a live ping, then recovering when the app started.
The legacy UID 1001 named-log failure was also reproduced.

Additional unmodified candidate smokes with the same reviewed graph v0.4.0 and SQL v0.4.0 pins:

- `just smoke-media`: 15/15 passed, exit 0; teardown removed all project containers, volumes and
  networks.
- `just smoke-released-fixture`: 7/7 passed, exit 0; no fake `SOURCE_DUMP_ID` or
  `SOURCE_DUMP_DATE`; teardown removed all project resources.
- `just smoke-released` (`gm-resume-released-20261101`): schema initialization succeeded twice,
  all ten application services became healthy, shutdown completed and teardown removed all project
  containers, volumes and networks.
- `just smoke-erasure`: 32/32 passed with unchanged API and store images; this scenario does not
  run the graph enricher, so the evidence remains applicable.

The final runtime comparison covered 41 candidate runtime, configuration and smoke files against
the rebased Linux-smoke branch. The only difference was `docker-compose.external-postgres.yml`'s
`PGSSLCERTMODE` setting, which these dedicated-store smokes do not use; all tested Compose inputs,
smoke scripts and complete reviewed image digest/version tables were byte-identical. This is why
the unmodified supporting smoke results apply to the reviewed pins above.

These fresh-run results do not reconstruct completion signals already acknowledged by v0.3.0. A
pin-only upgrade leaves a gate fail-closed if those terminal events are gone; do not restart the
extractor or write latch/marker rows. Recovery requires an operator-approved full new Discogs pass
after a read-only baseline, encrypted recovery bundle, headroom checks, an idle producer and
healthy v0.4.0 consumers. Plan and approve that recovery separately; it is not part of this
pin-only promotion.

### Promotion of musicbrainz-sql-loader v0.6.1 (2026-11-07)

One image: `musicbrainz-sql-loader:v0.6.1`, the fix for `gm-musicbrainz-sql-loader-ud7m`. Two MusicBrainz
releases can name one Discogs release, and their two messages wrote the same `graph.issued_on` range from two
transactions. The second insert hit `issued_on_pkey` and the message was dead-lettered to
`groovemap-musicbrainz-brainztableinator-releases.dlq`. v0.6.1 takes a transaction-scoped advisory lock on the
range, so the later message waits and then replaces the earlier one's edges; a real duplicate inside one
record still fails. Nothing here has been run on lux by this repository.

`gh attestation verify oci://ghcr.io/groovemap-music/musicbrainz-sql-loader@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, `org.opencontainers.image.revision`
equals the commit `v0.6.1` points to, `org.opencontainers.image.version` is `v0.6.1`, and the image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `musicbrainz-sql-loader:v0.6.1` | `sha256:c5654945af2f05835139764888893f179242adf3e04868a3c8917b95f037f56c` | `ac1036da6eedfd8f786de864c6ca38789458d113` | `v0.6.0` — `sha256:022fa4d665ac46203cb31b5bf94dd6dc825a3cf69e70a497a1c3a9d0efa2d8d9` |

**Superseded:** the `v0.6.0` pin in the 2026-11-07 release set below; it remains the rollback target.

**Paired floor unchanged.** `musicbrainz-sql-loader` `0.6.0` with `database-schema` `0.9.0` still applies and
`v0.6.1` meets it. No setting, volume or schema change.

**Parked messages.** After the loader is running v0.6.1, the messages in the `releases.dlq` can be moved back
to the releases queue by an operator. Processing is idempotent (the loader's edges for one release are deleted
and re-inserted, and the Discogs loader's rows are never in range), and two replays that collide now serialize
instead of failing. This record does not move them.

### Promotion of analytics-engine v0.6.2 and catalog-api v0.9.0 (2026-11-07)

Two images, and the order matters: **deploy `analytics-engine` v0.6.2 first, then `catalog-api` v0.9.0.**
`catalog-api` v0.9.0 returns `completeness_pct: null` for artists and labels, where v0.8.0 returned `0.0`
(images are no longer scored). `analytics-engine` v0.6.1 declares that field a non-optional float, so it fails
the daily job on the first null. v0.6.2 accepts null and stores it in the nullable `insights.data_completeness`
column (database-schema v0.8.0 or later, which this record's schema `v0.9.0` already is). `graph-explorer`
`v0.3.3` must also be live first, so the null renders as n/a (see the 2026-11-07 record below).

Both digests are the index digests of the released tags on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for both. On `linux/amd64`, `org.opencontainers.image.revision` equals the
commit the release tag points to (`analytics-engine` `v0.6.2` = `b872bb2d49389f5a14090fa1045d9ea75aac9208`,
`catalog-api` `v0.9.0` = `fe4e68c0ff4786718e5bd4bd5d5117720c7780ed`), `org.opencontainers.image.version` equals the
tag, and both image users are `1000:1000`. Both commits contain the null fix: `aabcbd2` in `v0.6.2` and `6affd99`
in `v0.9.0`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `analytics-engine:v0.6.2` | `sha256:a619f5cd4aa67492c35766836db79468a098ba9d2d6f0a074f443131a26d6849` | `b872bb2d49389f5a14090fa1045d9ea75aac9208` | `v0.6.1` — `sha256:a88d82a0486840d9e89ac75cd91e263b642d6b1cb6a33357d7fe2f73e156caf1` |
| `catalog-api:v0.9.0` | `sha256:c5304e52c184f4b3dc25aa0175e5bcc05db650270608f0152d73bd8eecccdeba` | `fe4e68c0ff4786718e5bd4bd5d5117720c7780ed` | `v0.8.0` — `sha256:4b41ea68cafe2817d25f3d17946747d2f1729c6e1039fc4e6943f8e33f809653` |

**Superseded:** the `analytics-engine` `v0.6.1` and `catalog-api` `v0.8.0` pins in the 2026-11-07 release set below.
They remain the rollback targets.

**Rollback order is the reverse:** `catalog-api` back to `v0.8.0` first (it works with either analytics-engine),
then `analytics-engine` back to `v0.6.1`. Rolling `analytics-engine` back while `catalog-api` v0.9.0 is live
brings the daily job back to the null failure. No schema change here: the schema is the `database-schema` v0.9.0
record below, and it is not rolled back.

### Promotion of database-schema v0.10.0, analytics-engine v0.7.0 and catalog-api v0.10.0 (2026-11-07)

Three images, and the order matters: **deploy `database-schema` v0.10.0 first** (it adds
`label_longevity.is_placeholder`), **then `analytics-engine` v0.7.0, then `catalog-api` v0.10.0.**
Nothing is deployed by this record.

This promotion supersedes the unmerged catalog-api v0.9.1 promotion gm-deployment-p03a. That bead
should be closed. Its digest was never pinned, so there is nothing to roll back to from it.

Each digest is the index digest of the released tag on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for all three. On `linux/amd64`, `org.opencontainers.image.revision`
equals the commit the release tag points to, `org.opencontainers.image.version` equals the tag, and every
image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.10.0` | `sha256:0624cda98c4460eff0c25ea905d220bad509099100dbc55cc91f2688e9a76749` | `4252e933e71199be62184752af410542e5894ab8` | `v0.9.0` — `sha256:1076fef32c5b922b9228ab5b257e75143d5ee58d964b75a7c6f37dc9a539461b` |
| `analytics-engine:v0.7.0` | `sha256:eb6eecf916c876ea22bbfb0c669a780e702941b24c55dfacdea4a1f48968bd4f` | `2ea83b9ea25a49a1e1ebe932dde21e471a994216` | `v0.6.2` — `sha256:a619f5cd4aa67492c35766836db79468a098ba9d2d6f0a074f443131a26d6849` |
| `catalog-api:v0.10.0` | `sha256:427f4b71540bc28c598309a92322d11f814aa82b33058d6c328285a4b1d973c6` | `583769f1307e82292a3801974c85a4318152eec6` | `v0.9.0` — `sha256:c5304e52c184f4b3dc25aa0175e5bcc05db650270608f0152d73bd8eecccdeba` |

**Superseded:** the `database-schema` `v0.9.0`, `analytics-engine` `v0.6.2` and `catalog-api` `v0.9.0`
pins in the records above. They are the rollback targets in the table.

**Rollback order is the reverse of deploy order:** `catalog-api` back to `v0.9.0` first, then
`analytics-engine` back to `v0.6.2`. The schema is not rolled back (`v0.10.0` stays applied). This record does
not verify that `catalog-api` `v0.9.0` or `analytics-engine` `v0.6.2` run against the `v0.10.0` schema, so check
that before a rollback.

`check-images.py` passes with these pins, and `RELEASED_IMAGE_VERSIONS` records the new versions.

### Promotion of database-schema v0.11.0 (2026-11-07)

`database-schema:v0.11.0` resolved from its released tag on GHCR (the index digest). Nothing is deployed by
this record: the operator runs the lux schema window below, after `database-schema` `v0.11.0` is pinned.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.11.0` | `sha256:4733f4b710bc1fc05752cb7418340f5657fb28ff156fbd20cfb7c0196031dfef` | `8e1117f8fbf2d3ad419e30851494d6ee9978edf7` | `v0.10.0` — `sha256:0624cda98c4460eff0c25ea905d220bad509099100dbc55cc91f2688e9a76749` |

`gh attestation verify oci://ghcr.io/groovemap-music/database-schema@sha256:4733f4b7…dfef --owner
groovemap-music` exited 0 (1 SLSA provenance v1 attestation verified). On `linux/amd64`,
`org.opencontainers.image.revision` equals `8e1117f8fbf2d3ad419e30851494d6ee9978edf7`, the commit tag `v0.11.0`
points to, `org.opencontainers.image.version` is `v0.11.0`, and the image user is `1000:1000`.

**Supersedes** `database-schema:v0.10.0` in the record above. The `v0.10.0` digest is the rollback target.

**What it adds.** The `release_search` side table and its triggers, the per-day identity ledger rollup, and
`pg_stat_statements` when the server preloads it.

**Operator steps (lux).** Stop the SQL writers (`tableinator`, `brainztableinator`), then run `schema-init` as
the schema owner in a quiet window. Then run `CALL public.release_search_backfill();` and check
`SELECT * FROM public.release_search_drift();`, which must return `(0,0,0)`.
Never run an older `schema-init`. Once `v0.11.0` has run, the schema is not rolled back: the rollback digest
above is for the image pin only.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["DATABASE_SCHEMA_IMAGE"]` as `0.11.0`.
The two too-new schema floor tests now use `0.12.0`, since `0.11.0` is the pinned release.

### Promotion of catalog-api v0.11.0 (2026-11-08)

One image: `catalog-api:v0.11.0`, which ranks unfiltered release search from `public.release_search` (gm-catalog-api-ysh9),
with the label-longevity contract parameters and the label-dna and ledger performance work. Nothing is deployed by
this record. It needs `database-schema` `v0.11.0`, which is already the reviewed pin and applied on lux with
`release_search` backfilled and drift `(0,0,0)`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.11.0` | `sha256:25375d52a1484888b09b0f6daa31f1590672bc18d9d064ece503e9ef689e4830` | `e91f1f3dcfd657578d3e2660f78778e95bb189c9` | `v0.10.0` — `sha256:427f4b71540bc28c598309a92322d11f814aa82b33058d6c328285a4b1d973c6` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@sha256:25375d52…4830 --owner
groovemap-music` exited 0 (1 SLSA provenance attestation verified). On `linux/amd64`,
`org.opencontainers.image.revision` equals `e91f1f3dcfd657578d3e2660f78778e95bb189c9`, the commit the annotated
tag `v0.11.0` peels to, `org.opencontainers.image.version` is `v0.11.0`, and the image user is `1000:1000`.

**Supersedes** `catalog-api:v0.10.0` in the record above. The `v0.10.0` digest is the rollback target.

**Operator step (lux).** After confirming `SELECT * FROM public.release_search_drift();` returns `(0,0,0)`, recreate
only the API: `up -d --no-deps api`. No `schema-init` is needed; the schema is already at `v0.11.0`.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]` as `0.11.0`.
`PAIRED_IMAGE_FLOORS` refuses `catalog-api` `0.11.0` on any `database-schema` older than `0.11.0`, since an older
schema lacks `release_search`.

### Promotion of catalog-api v0.11.1 (2026-11-09)

One image: `catalog-api:v0.11.1`, the windowed ledger works read. It uses the per-work summary when the window holds
every event, and otherwise full days from the per-day rollup (gm-catalog-api-tn5r). Nothing is deployed by this
record. It runs on `database-schema` `0.11.0` and later, so it adds no new paired floor. `database-schema` `v0.12.0`
is applied on lux with the rollup filled, and that does not change this image.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.11.1` | `sha256:426182b6e2023cb4f73e397c31956f53cb963b8c03df37d65f4990f23c15ee84` | `c04b540b8c005612505be99f3b614e0f90e04d3a` | `v0.11.0` — `sha256:25375d52a1484888b09b0f6daa31f1590672bc18d9d064ece503e9ef689e4830` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@sha256:426182b6…5ee84 --owner
groovemap-music` exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals
`c04b540b8c005612505be99f3b614e0f90e04d3a`, the commit the annotated tag `v0.11.1` peels to,
`org.opencontainers.image.version` is `v0.11.1`, and the image user is `1000:1000`.

**Supersedes** `catalog-api:v0.11.0` in the record above. The `v0.11.0` digest is the rollback target.

**Operator step (lux).** Recreate only the API after the release set is reviewed: `up -d --no-deps api`. No
`schema-init` is needed.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]` as `0.11.1`. The
existing `PAIRED_IMAGE_FLOORS` entry for `catalog-api` is unchanged.

### Promotion of catalog-api v0.11.3 (2026-11-09)

One image: `catalog-api:v0.11.3`, the search release from the tag on `56db8b5537a3d514a98992340dea37be73751519`. It is
`v0.11.2` plus only `gm-catalog-api-zi2r`, on the release branch `release/v0.11.x-search`. It carries none of `main`'s
unreleased prototype or Missing/copy source. Searches that exclude releases (for example `types=master`) skip the
release facets, which removes the ~0.6 s floor on master search. Nothing is deployed by this record.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.11.3` | `sha256:4e5a8d7f94d9d985a90d48c31306c903e61a8d7cc87a8412a843dcee3cbf8d57` | `56db8b5537a3d514a98992340dea37be73751519` | `v0.11.2` — `sha256:b0b74d531701e456ef30f8af3e0695cf458aa085a3b2269637679420c4cb6f37` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@sha256:4e5a8d7f…8d57 --owner groovemap-music`
exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals `56db8b5537a3d514a98992340dea37be73751519`, the
commit the `v0.11.3` tag is on, `org.opencontainers.image.version` is `v0.11.3`, and the image user is `1000:1000`.

**Pair.** The image has the same schema requirement as `v0.11.2`: it reads `release_search`'s facet columns and
`master_search`, which `database-schema` `v0.12.0` creates. The existing `PAIRED_IMAGE_FLOORS` row for `CATALOG_API_IMAGE`
`0.11.2` already covers `0.11.3`, so it is unchanged. The reviewed `database-schema` `v0.13.0` meets it.

**Supersedes** `catalog-api:v0.11.2` in the record above. The `v0.11.2` digest is the rollback target.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]` as `0.11.3`.

### Promotion of catalog-api v0.11.4 (2026-11-09)

One image: `catalog-api:v0.11.4`, the search release from the tag on `9f6980346e563eec8eb20eacd9a153e316b15504`. It is
`v0.11.3` plus only `gm-catalog-api-2hrn`, on the release branch `release/v0.11.x-search`. Genre and style trends read
the per-year series that `discogs-graph-enricher` `v0.7.0` stores (live on lux, `stats_version` 2), and fall back to the
live traversal when that series is absent, so the image stays safe on an older enricher. Nothing is deployed by this
record.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.11.4` | `sha256:6593499eeb761c45e0229e91ebfd1bf370587cdbc35f4b06ac3231479db1bb2a` | `9f6980346e563eec8eb20eacd9a153e316b15504` | `v0.11.3` — `sha256:4e5a8d7f94d9d985a90d48c31306c903e61a8d7cc87a8412a843dcee3cbf8d57` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@sha256:6593499e…1bb2a --owner groovemap-music`
exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals `9f6980346e563eec8eb20eacd9a153e316b15504`, the
commit the `v0.11.4` tag is on, `org.opencontainers.image.version` is `v0.11.4`, and the image user is `1000:1000`.

**Pair.** No new paired floor. The image keeps the `v0.11.3` schema requirement, which the existing
`PAIRED_IMAGE_FLOORS` row for `CATALOG_API_IMAGE` `0.11.2` already covers. The reviewed `database-schema` `v0.13.0`
meets it.

**Supersedes** `catalog-api:v0.11.3` in the record above. The `v0.11.3` digest is the rollback target.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]` as `0.11.4`.

### Promotion of catalog-api v0.11.2 (2026-11-09)

One image: `catalog-api:v0.11.2`, the search release. It is `v0.11.1` plus only the search commits from
`gm-catalog-api-2jka` and `gm-catalog-api-4asm`, on the release branch `release/v0.11.x-search`. It carries none of
`main`'s unreleased prototype or Missing/copy source. Facets and filtered release branches read `public.release_search`,
master search reads `public.master_search`, and release facets count every match. Nothing is deployed by this record.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.11.2` | `sha256:b0b74d531701e456ef30f8af3e0695cf458aa085a3b2269637679420c4cb6f37` | `35781fe47667d93afa0da554791d37bfc7a3e150` | `v0.11.1` — `sha256:426182b6e2023cb4f73e397c31956f53cb963b8c03df37d65f4990f23c15ee84` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@sha256:b0b74d53…cb6f37 --owner groovemap-music`
exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals `35781fe47667d93afa0da554791d37bfc7a3e150`, the
commit the `v0.11.2` tag is on, `org.opencontainers.image.version` is `v0.11.2`, and the image user is `1000:1000`.

**Pair.** The image reads `release_search`'s facet columns and `master_search`, which `database-schema` `v0.12.0`
creates. `PAIRED_IMAGE_FLOORS` gains `CATALOG_API_IMAGE` `0.11.2` needs `DATABASE_SCHEMA_IMAGE` `0.12.0`. Older schemas
lack those, so search would fail. The `0.11.0` pair stays in place.

**Supersedes** `catalog-api:v0.11.1` in the record above. The `v0.11.1` digest is the rollback target.

**Operator step (lux).** Recreate only the API after the release set is reviewed: `up -d --no-deps api`. No
`schema-init` is needed. The `v0.11.1` record above has `database-schema` `v0.12.0` applied on lux, which meets the
paired floor; confirm that before recreating the API.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["CATALOG_API_IMAGE"]` as `0.11.2`.

### Promotion of database-schema v0.12.0 (2026-11-09)

`database-schema:v0.12.0` resolved from its released tag on GHCR (the index digest). Nothing is deployed by
this record: the operator runs the lux schema window below, after `database-schema` `v0.12.0` is pinned.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.12.0` | `sha256:2c39b94efd6ad2436e60114435275fd9887ecf49e5a777b6a573763aa9b9f522` | `df17cecead1ec8cfd83f103bb63e7ddb8d5db9b5` | `v0.11.0` — `sha256:4733f4b710bc1fc05752cb7418340f5657fb28ff156fbd20cfb7c0196031dfef` |

`gh attestation verify oci://ghcr.io/groovemap-music/database-schema@sha256:2c39b94f…b522 --owner
groovemap-music` exited 0. On `linux/amd64` (manifest `sha256:d7841999…bfef`), `org.opencontainers.image.revision`
equals `df17cecead1ec8cfd83f103bb63e7ddb8d5db9b5`, the commit the annotated tag `v0.12.0` peels to,
`org.opencontainers.image.version` is `v0.12.0`, and the image user is `1000:1000`.

**Supersedes** `database-schema:v0.11.0` in the record above. The `v0.11.0` digest is the rollback target.

**What it adds.** Search facet and filter columns on `public.release_search` (`genres`, `year`, `country`,
`media_families`, `media_mediums`) and a new `public.master_search` with capture triggers on `public.masters`.
It pins `n_distinct` on `graph.credited_on.release_id` and `graph.track_credited_on.release_id`, and on a populated
identity ledger whose `work_summary_day` is empty or missing it requests one ledger rebuild. All changes are additive.

**Operator steps (lux).** After `schema-init` as the schema owner, outside a transaction:

1. `ANALYZE graph.credited_on;` and `ANALYZE graph.track_credited_on;`
2. `SELECT identity_ledger_refresh();` (a full rebuild, about 6 minutes)
3. `CALL public.master_search_backfill();` (about 1 minute), then `SELECT * FROM public.master_search_drift();`, which must return `(0,0,0)`
4. `CALL public.release_search_backfill();` (about 10 minutes; it rewrites every `release_search` row), then `VACUUM public.release_search;`
5. `SELECT * FROM public.release_search_drift();`, which must return `(0,0,0)`

If a drift check hits `/dev/shm` limits, run `SET max_parallel_workers_per_gather = 0;` first. catalog-api reads the
new columns and `master_search` only after both drift checks pass. Never run an older `schema-init`. Once `v0.12.0`
has run, the schema is not rolled back: the rollback digest above is for the image pin only.

`check-images.py` pins the new digest and records `RELEASED_IMAGE_VERSIONS["DATABASE_SCHEMA_IMAGE"]` as `0.12.0`.
The too-new schema floor tests now use `0.13.0`, since `0.12.0` is the pinned release.

### Promotion of discogs-graph-enricher v0.8.0 (2026-11-09)

The index digest of the released tag on GHCR. Nothing is deployed by this record: on lux it is a `graphinator`-only
recreate (render compare, then `docker compose up -d --no-deps graphinator`), which runs one full stats sweep because
`STATS_VERSION` moves to 3.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `discogs-graph-enricher:v0.8.0` | `sha256:f74eeda853eb0e54c74fa86a20ffb5a0469baee1bd6bc7b06e4e52b917362cf3` | `7ce7e36eed9fdbca791c5c97f650acce19976f39` | `v0.7.0` — `sha256:69634f1473574fec51623d0ba289137be7b0dce65684251357e333a57798d929` |

`gh attestation verify oci://ghcr.io/groovemap-music/discogs-graph-enricher@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals the
commit `v0.8.0` peels to, `org.opencontainers.image.version` is `v0.8.0`, and the image user is `1000:1000`.

**What changes.** Label nodes gain stored longevity fields (`first_year`, `last_year`, `years_active`,
`total_releases`, `peak_decade`) computed in the existing `label_stats` walk (gm-discogs-graph-enricher-70nx).
catalog-api reads them once the newest ExtractionCompletion has `stats_version >= 3` and falls back to a live traversal
otherwise, so the order against catalog-api does not matter for correctness. The `label_stats` step reads about 4-5%
more (an estimated 15-25 s on its 165 s); confirm the real figure from graphinator's step durations after deploy.

**Supersedes** `discogs-graph-enricher:v0.7.0` from the 2026-11-09 set.

### Promotion of the 2026-11-09 set: database-schema v0.13.0, discogs-sql-loader v0.6.0, musicbrainz-sql-loader v0.7.0 and discogs-graph-enricher v0.7.0

Each digest is the index digest of the released tag on GHCR. Nothing is deployed by this record: the operator
runs the lux window in [homelab](homelab.md#lux-upgrade-the-2026-11-09-set) afterwards.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.13.0` | `sha256:f04f4e08f5fa7df3c5613333ce57f92f7ee306b127e26d6387c496b5a82d01af` | `a225c3db6556adf3141ce115b5afb96068d2c44e` | `v0.12.0` — `sha256:2c39b94efd6ad2436e60114435275fd9887ecf49e5a777b6a573763aa9b9f522` (image pin only; never roll the schema back) |
| `discogs-sql-loader:v0.6.0` | `sha256:64e18ea3554d5e3c944a59fd73397c6f5d0d2bb6ad821e939e0a3f5965b9f491` | `2232dd32fa136523fe6b45a0fb2268aa7ea9c4d0` | `v0.5.0` — `sha256:5a77fbdeb44759e3547b52bad7b9193a3d7f36dbb41668b553fedb2a1bff5f17` |
| `musicbrainz-sql-loader:v0.7.0` | `sha256:16345121813c9b85d70d97f3ce28a2364299ff64e46c7accc4b6443424715212` | `a720a86d1c778b19c2a569850e1f40836dd1ecf2` | `v0.6.1` — `sha256:c5654945af2f05835139764888893f179242adf3e04868a3c8917b95f037f56c` |
| `discogs-graph-enricher:v0.7.0` | `sha256:69634f1473574fec51623d0ba289137be7b0dce65684251357e333a57798d929` | `85bea6f2fd4af3042068f9193e3eacee28530337` | `v0.6.2` — `sha256:d13b496070b4deba6756d5f572f9b7c10bff9982257ef2739f99a6038e91b487` |

`gh attestation verify oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for all four. On `linux/amd64`, `org.opencontainers.image.revision` equals the
commit each release tag peels to, `org.opencontainers.image.version` equals the tag, and every image user is
`1000:1000`.

**Supersedes** `database-schema:v0.12.0`, `discogs-sql-loader:v0.5.0`, `musicbrainz-sql-loader:v0.6.1` and
`discogs-graph-enricher:v0.6.2`; their digests are the rollback targets above.

**What it adds.**

- `database-schema` `v0.13.0`: `loader_seen` seen lists and a reordered `graph.issued_on` key. On lux the new index
  `issued_on_pkey_new` was already built `CONCURRENTLY` and is valid (2026-11-09, 1009 MB), so `schema-init`'s key swap is
  catalog-only.
- `discogs-sql-loader` `v0.6.0` and `musicbrainz-sql-loader` `v0.7.0`: seen lists in shadow mode by default (a startup
  probe; touch mode on an older schema). MusicBrainz also gets lock-free no-op upserts.
- `discogs-graph-enricher` `v0.7.0`: per-year Genre and Style series and `STATS_VERSION` 2, so the first start runs
  one full stats sweep.

**Paired floor.** None is added. The loaders fall back to touch mode against a schema without `loader_seen`, so
`PAIRED_IMAGE_FLOORS` is unchanged (a comment beside it says so). `check-images.py` records
`RELEASED_IMAGE_VERSIONS` as `0.13.0`, `0.6.0`, `0.7.0` and `0.7.0` for the four images; no other pin changes. The
too-new schema floor tests now use `0.14.0`, since `0.13.0` is the pinned release.

### Lux recreate of the Dependabot #55 data-tier pins (2026-11-10)

Dependabot #55 moved the third-party data-tier pins on main. They reach lux only when those services are recreated, which
was done on its own at deployment `ebe60cd`, with every extractor waiting for its next check, every RabbitMQ queue at 0
messages and no Neo4j transaction running.

| Service | Before | After |
| --- | --- | --- |
| `neo4j` | `2026.10.0-community` | `2026.10.1-community` |
| `rabbitmq` | `4-management@sha256:c200fd49…` | `4-management@sha256:af6f50d3…` |
| `valkey` | `9.1.2-alpine` | `9.1.3-alpine` |
| `valkey-exporter` | `v1.95.0` | `v1.97.0` |
| `otel-collector` | `0.163.2` | `0.164.0` (multi-arch index, not the `-386` tag) |

Each was pulled, then `docker compose up -d --no-deps <service>`. Neo4j was healthy about 20 s after the recreate. After
it: all 16 work queues had their consumers back (the 16 without consumers are the `.dlq` queues), the `catalog-dlq-cap`
policy was intact, and the api answered healthy. The observability services that lux does not run (Grafana,
VictoriaMetrics, VictoriaTraces, cAdvisor, node-exporter) were left stopped.

The first search after the Valkey recreate took 2,679 ms (all-types `love`); the same search with a fresh cache key a
minute later took 546 ms, and uncached master searches took 12-51 ms. That first-request cost is gm-catalog-api-0n70.

### Promotion of graph-explorer v0.8.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `graph-explorer:v0.8.0` | `sha256:a260a020daff484d817700b8cf3b87ab47768ef1b8427b3b6452872cc12ac63e` | `3fd8df6dd43dc7c18c2f59c91f08e08cf9f5ce2d` | `v0.7.0` — `sha256:a118adab9bd139259d99726d8542c4c7cba12f89ff232e6ce29be03f3a84a54b` |

`gh attestation verify oci://ghcr.io/groovemap-music/graph-explorer@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, the revision label is the commit `v0.8.0` peels to,
the version label is `v0.8.0`, and the image user is `1000:1000`.

**What changes.** Path finding sends `from_id` and `to_id` for label ends, which catalog-api v0.14.2 and later serve
(gm-graph-explorer-zl42). Leaving a path field cancels its pending suggestion request, so a stale list can no longer
cover Connect on tablet layouts. On lux: an `explore`-only recreate.

**Supersedes** `graph-explorer:v0.7.0`.

### Promotion of catalog-api v0.15.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.15.0` | `sha256:94df897773275125fb006372a7a111bbe3bc391f55846277925c39cb030858c2` | `e276256bf9b693ebbb8c4209e6a1ac40c4b693fd` | `v0.14.2` — `sha256:fc5d468235504769827809e6c5a42d01d102f20be8685ff6e15d9ee8debc81eb` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, the revision label is the commit `v0.15.0` peels to,
the version label is `v0.15.0`, and the image user is `1000:1000`.

**What changes.** The NLQ `find_path` tool takes `from_label_id` and `to_label_id` (gm-catalog-api-p4hv). The
python-libraries runtime and agent-tools pin moves from 5f35c03 to f0964cc, which is runtime-identical to 89811f4, the
runtime discogs-sql-loader v0.6.0 runs on lux. catalog-api now gets the PostgreSQL pool checkout-probe TTL (30 s), idle
close (300 s), max lifetime (1800 s), TCP keepalives, Neo4j busy-pool probe handling and the pool metrics. On lux: an
`api`-only recreate, deployed on its own. Watch the `groovemap` connection count, which was 14 idle before the deploy.

**Supersedes** `catalog-api:v0.14.2`.

### Promotion of graph-explorer v0.7.0 and catalog-api v0.14.2 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `graph-explorer:v0.7.0` | `sha256:a118adab9bd139259d99726d8542c4c7cba12f89ff232e6ce29be03f3a84a54b` | `0f449b41aee93a4d555694719a52b2a224dcb5d1` | `v0.6.0` — `sha256:dc984b2f3ba637429dd3e0b94979f960c35978183672cc21ab3da150e24fdad0` |
| `catalog-api:v0.14.2` | `sha256:fc5d468235504769827809e6c5a42d01d102f20be8685ff6e15d9ee8debc81eb` | `feb10735f1299fd04c7ab7f5cb26953d3c9f41fc` | `v0.14.1` — `sha256:b6d9c4a5bc3d28bfab34db2ce91a53628fabb9a40ae849bef601b6974c6e7808` |

For both images, `gh attestation verify oci://ghcr.io/groovemap-music/<repository>@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, each image's revision label is the commit its tag peels to, its version label
is its tag, and its image user is `1000:1000`.

**What changes.** Label explore, expand, the label counts and path match labels by id, so labels that share a name no
longer merge. `/api/explore` and `/api/expand` take `id=` for type=label, and `/api/path` takes `from_id` and `to_id`.
An ambiguous bare name returns 409 plus candidates. Explore cache keys are namespaced by id vs name, so the explore
cache starts cold once (gm-catalog-api-ntnw, gm-catalog-api-jjuh). graph-explorer sends the label id with the name on
explore, expand and path, and lets the user pick when a name is ambiguous (gm-graph-explorer-vag6). On lux: recreate
`explore` first, then `api`.

**Supersedes** `graph-explorer:v0.6.0` and `catalog-api:v0.14.1`.

### Promotion of graph-explorer v0.6.0 and catalog-api v0.14.1 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `graph-explorer:v0.6.0` | `sha256:dc984b2f3ba637429dd3e0b94979f960c35978183672cc21ab3da150e24fdad0` | `10b6588459711c45ed0bbaa14d4f9059c424ed9e` | `v0.5.0` — `sha256:e2166123f8e13092e0da8a0bc640845ff7364a4faee42055d37676f74359644d` |
| `catalog-api:v0.14.1` | `sha256:b6d9c4a5bc3d28bfab34db2ce91a53628fabb9a40ae849bef601b6974c6e7808` | `a822da6f576b80961df59c20ef83ad3625016b3a` | `v0.14.0` — `sha256:5268511037c8a7a618a54c41b77e23b96feb597d984afaf149174efddff74cf9` |

For both images, `gh attestation verify oci://ghcr.io/groovemap-music/<repository>@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, each image's revision label is the commit its tag peels to, its version label
is its tag, and its image user is `1000:1000`.

**What changes.** Label trends match the label by id, so labels that share a name no longer merge (gm-catalog-api-1whr).
An ambiguous bare name returns 409 plus candidates, and trends cache keys are namespaced by id vs name, so the trends
cache starts cold once. graph-explorer sends the label's id with its name and lists the candidates on a 409
(gm-graph-explorer-toyc). On lux: recreate `explore` first, then `api`, so no window shows a blank trends chart for the
4 labels (2 names) that share a name.

**Supersedes** `graph-explorer:v0.5.0` and `catalog-api:v0.14.0`.

### Promotion of graph-explorer v0.5.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `graph-explorer:v0.5.0` | `sha256:e2166123f8e13092e0da8a0bc640845ff7364a4faee42055d37676f74359644d` | `182330ee9ea9864312c5347a2705333c58f53bcb` | `v0.4.0` — `sha256:1cfa53419e888e871e365a0f8f0d827c1a2963a6375cd63d57a92734e4e52ac6` |

`gh attestation verify oci://ghcr.io/groovemap-music/graph-explorer@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, the revision label is the commit `v0.5.0` peels to,
the version label is `v0.5.0`, and the image user is `1000:1000`.

**What changes.** A label's detail panel shows its full-catalog longevity rank as "#N of M", from catalog-api v0.14.0's
`longevity_rank` and `longevity_rank_of` (gm-graph-explorer-ugpg, the last part of gm-graph-explorer-i51p). Placeholder
labels show no rank. When the API returns no rank, the panel falls back to the top-50 lookup, so no paired floor is
needed. On lux: an `explore`-only recreate.

**Supersedes** `graph-explorer:v0.4.0`.

### Promotion of catalog-api v0.14.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.14.0` | `sha256:5268511037c8a7a618a54c41b77e23b96feb597d984afaf149174efddff74cf9` | `a378a885969bb8df6a96bd919be57bb767af70a7` | `v0.13.0` — `sha256:53d3b686b4bf89dab16e74f797d5cc6daf71dd8e29369844c5514d58c14afe36` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, the revision label is the commit `v0.14.0` peels to,
the version label is `v0.14.0`, and the image user is `1000:1000`.

**What changes.** Label details return `longevity_rank` and `longevity_rank_of`, read from discogs-graph-enricher
v0.9.0's stored rank in the existing stored-stats query (gm-catalog-api-gkcu, part of gm-graph-explorer-i51p). They
are null until the newest ExtractionCompletion is maintained with `stats_version >= 4`. No live fallback. On lux: an
`api`-only recreate. Lux's enricher stamped stats_version 4 on 2026-11-10: 929,239 labels ranked, and ranks 1-50 equal
both catalog-api's live label-longevity statement and the stored insights snapshot.

**Supersedes** `catalog-api:v0.13.0`.

### Promotion of discogs-graph-enricher v0.9.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `discogs-graph-enricher:v0.9.0` | `sha256:911f549dc4b39c8ca55388be8efcd4f813122fb7c7fcdada026339e0dbd35f3d` | `538bde9c30f2dadebb6d838ecff853e3555447ff` | `v0.8.0` — `sha256:f74eeda853eb0e54c74fa86a20ffb5a0469baee1bd6bc7b06e4e52b917362cf3` |

`gh attestation verify oci://ghcr.io/groovemap-music/discogs-graph-enricher@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, the revision label is the commit `v0.9.0` peels to,
the version label is `v0.9.0`, and the image user is `1000:1000`.

**What changes.** After the label_stats pages, a rank phase stores each real label's place in catalog-api's
label-longevity order as `l.longevity_rank`. It stores the ranked count M once, on the dump's ExtractionCompletion
node, as `label_longevity_rank_of` (gm-discogs-graph-enricher-wo8n, part of gm-graph-explorer-i51p). STATS_VERSION goes
to 4, so the first start runs one full stats sweep plus the rank phase. The rank read holds about 420 MB of Neo4j
transaction memory (lux's cap is 3g in total) and must finish inside the 600 s transaction timeout. catalog-api serves
the rank once gm-catalog-api-gkcu ships. On lux: a `graphinator`-only recreate. No schema dependency.

**Supersedes** `discogs-graph-enricher:v0.8.0`.

### Promotion of database-schema v0.14.0 and musicbrainz-sql-loader v0.8.0 (2026-11-10)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.14.0` | `sha256:5fecc4c16d5556f4cd614504128a13e412b314721b30766464402b334b32c5ce` | `66e8891b1a05b93502fda3063e1356f532ebb2b6` | `v0.13.0` — `sha256:f04f4e08f5fa7df3c5613333ce57f92f7ee306b127e26d6387c496b5a82d01af` |
| `musicbrainz-sql-loader:v0.8.0` | `sha256:aadae1176b8137ad638cb9e5fc97a99919b46df5c6bfe5c9112fe7904bf08bd1` | `57a9f7e304f8ff1fec55e282f2e2392171909a45` | `v0.7.0` — `sha256:16345121813c9b85d70d97f3ce28a2364299ff64e46c7accc4b6443424715212` |

For both images, `gh attestation verify oci://ghcr.io/groovemap-music/<repository>@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, each image's revision label is the commit its tag peels to, its version label
is its tag, and its image user is `1000:1000`.

**What changes.** The schema adds a transaction-scoped advisory lock to `graph.refresh_artist_member_of()` and
`graph.refresh_vertex_degree()`, so a second refresh owner queues instead of interleaving `TRUNCATE`s
(gm-database-schema-gqs2). Where `embedding_pipeline` exists, it grants that role `USAGE` on `public`. It also revokes
PUBLIC's `EXECUTE` on the SECURITY DEFINER routines the schema owner owns (gm-database-schema-lag5). On a database
where the revoke was already applied by hand, as on lux on 2026-11-10, the sweep is a no-op. When a MusicBrainz
extraction completes, the loader now refreshes `artist_member_of` and `vertex_degree` (gm-musicbrainz-sql-loader-dikx).
It writes only the media edges that changed, and dead-letters a duplicate-medium record instead of requeuing it
(gm-musicbrainz-sql-loader-o0hb). The loader needs schema 0.14.0, and `PAIRED_IMAGE_FLOORS` enforces that. Seen mode
stays shadow. On lux: stop the loaders, run schema-init, then recreate `brainztableinator` and restart the other loaders.

**Supersedes** `database-schema:v0.13.0` and `musicbrainz-sql-loader:v0.7.0`.

### Promotion of operations-console v0.4.0 (2026-11-09)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `operations-console:v0.4.0` | `sha256:9d1601c44decb380434b21dbd2f1bb9dbb3b715396158754f669c4877b66f234` | `ebdf76a35ca41d7e888466230ad6fdfda54509fb` | `v0.3.1` — `sha256:07825a1c1c4ca622a3199364085b5821f2ebb69e004547dc4734de8216156816` |

`gh attestation verify oci://ghcr.io/groovemap-music/operations-console@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0; on `linux/amd64` the revision label is the commit `v0.4.0` peels to, the version
label is `v0.4.0`, and the image user is `1000:1000`.

**What changes.** A dead-letter Replay action beside Purge with exact partial-failure counts (gm-operations-console-7g18,
-se5t) and Identity Ledger alias evidence (gm-operations-console-bg46, operator-authorized for release 2026-11-09). Needs
catalog-api v0.13.0 (live). On lux: a `dashboard`-only recreate.

**Supersedes** `operations-console:v0.3.1`.

### Promotion of graph-explorer v0.4.0 (2026-11-09)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `graph-explorer:v0.4.0` | `sha256:1cfa53419e888e871e365a0f8f0d827c1a2963a6375cd63d57a92734e4e52ac6` | `c0d05dbf9f6696333ed49b5874d43ef7fea3e8a9` | `v0.3.3` — `sha256:885991da887c8f139b9ad5216c2e9b4a8cc95da9171e39b5e8d49ce56c8df2ce` |

`gh attestation verify oci://ghcr.io/groovemap-music/graph-explorer@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 (after the release run finished); on `linux/amd64` the revision label is the commit
`v0.4.0` peels to, the version label is `v0.4.0`, and the image user is `1000:1000`.

**What changes.** Label longevity on a label's detail panel and a longest-running-labels Trends timeline
(gm-graph-explorer-uhnz, -oehx, -yulr), and the Missing preview and physical-copy selection UI (gm-graph-explorer-qgzv,
operator-authorized for release 2026-11-09). Needs catalog-api v0.13.0 (live on lux). On lux: an `explore`-only recreate.

**Supersedes** `graph-explorer:v0.3.3`.

### Promotion of catalog-api v0.13.0 (2026-11-09)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `catalog-api:v0.13.0` | `sha256:53d3b686b4bf89dab16e74f797d5cc6daf71dd8e29369844c5514d58c14afe36` | `5584eb3e7e9b9b445f2afde5ffe64891ed825837` | `v0.11.4` — `sha256:6593499eeb761c45e0229e91ebfd1bf370587cdbc35f4b06ac3231479db1bb2a` |

`gh attestation verify oci://ghcr.io/groovemap-music/catalog-api@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0; on `linux/amd64` the revision label is the commit `v0.13.0` peels to, the version
label is `v0.13.0`, and the image user is `1000:1000`.

**What changes.** The first catalog-api release cut from main since v0.11.1 (main CI green on 5584eb3, including the
integration tier). It carries everything in v0.12.0, which is NOT promoted because it has the fresh-install label/trends
gate bug that v0.13.0 fixes (gm-catalog-api-adq2): the dead-letter replay route with structured failure counts
(gm-catalog-api-axbs, -3uvj) and label longevity on label details (gm-catalog-api-vflb). It adds the Missing preview and
copy listing routes (gm-catalog-api-t3e9/8t16) and the prototype cores (gm-catalog-api-ifqv), which the operator
authorized for release on 2026-11-09. graph-explorer v0.4.0 and operations-console v0.4.0 need it. No schema change: the
existing catalog-api 0.11.2 -> database-schema 0.12.0 floor still covers it. On lux: an `api`-only recreate.

**Supersedes** `catalog-api:v0.11.4`. `release/v0.11.x-search` is retired; releases come from main again.

### Promotion of postgres v18.6.1 (2026-11-09)

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `postgres:v18.6.1` | `sha256:7887f0365c4c36f007bd9238e69051483ed1617fc93f1e2b672b2cccdb10ae10` | `85de3a89fede6a4f9568f87607bde37b87783e37` | `v18.6.0` — `sha256:43fee8e4ebe980dfec290ec46aecfbc78368a2c95d1ba5d7b591e60025ac04e1` |

`gh attestation verify oci://ghcr.io/groovemap-music/postgres@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0; `org.opencontainers.image.revision` equals the commit `v18.6.1` peels to and
`org.opencontainers.image.version` is `v18.6.1`.

**What changes.** Only the base: postgres:18-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395
(Alpine 3.24.4, still PostgreSQL 18.6) instead of 77f58511 (Alpine 3.24.2); pgvector v0.8.7 is unchanged. The collation
proof on groovemap-music/postgres PR #1 built indexes under the previous base and read them under the new image: identical
ordering, amcheck clean, so an existing data directory needs no REINDEX. In this repository the pin feeds the dedicated
stack and store-check; lux's shared PostgreSQL is pinned in homelab.

**Supersedes** `postgres:v18.6.0`.

### Promotion of postgres v18.6.0 (2026-11-09)

One new image, `ghcr.io/groovemap-music/postgres:v18.6.0` (gm-deployment-p9ql): the official
`postgres:18-alpine` (PostgreSQL 18.6, Alpine 3.24,
`sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873`, the digest this stack pinned
before) plus pgvector v0.8.7 (`f37c13f68b57d2c3472b2214fbcff699d6d34876`). It is published from
[groovemap-music/postgres](https://github.com/groovemap-music/postgres). The image name has to match its
repository's name, and this repository publishes nothing. The same image is meant for the shared server
on lux. Nothing is deployed by this record.

| Image | Reviewed digest | Source revision | Rollback |
| --- | --- | --- | --- |
| `postgres:v18.6.0` | `sha256:43fee8e4ebe980dfec290ec46aecfbc78368a2c95d1ba5d7b591e60025ac04e1` | `27b1c26bc608d1cefe9ae0907465cb45d306651a` | `postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873` |

`gh attestation verify oci://ghcr.io/groovemap-music/postgres@sha256:43fee8e4…04e1 --repo
groovemap-music/postgres --signer-repo groovemap-music/automation --source-ref refs/tags/v18.6.0` exited 0 (1 SLSA
provenance attestation, signed by automation's `reusable-release.yml@833cb464`). The digest is the index the tag
resolves to; on `linux/amd64` (manifest `sha256:eabda681…b7f4`), `org.opencontainers.image.revision` equals
`27b1c26bc608d1cefe9ae0907465cb45d306651a`, the commit the annotated tag `v18.6.0` peels to, and
`org.opencontainers.image.version` is `v18.6.0`. Like the base, it sets no image user: `docker-entrypoint.sh`
drops to `postgres` (UID 70). It is published for `linux/amd64` only, where the base was multi-platform.

**Collation is unchanged.** The image keeps the base's musl. In the repository's `just collation-proof`,
a cluster initialised under the old image (libc provider, `en_US.UTF-8`, as this stack and lux are) gets
50,000 mixed-script strings and btree indexes on `t` and `lower(t)`. The same data directory, opened
with the new image, gives identical sort, `lower()` and index-scan order hashes. `amcheck`'s
`bt_index_parent_check(heapallindexed, rootdescend)` is clean on every index. Debian's glibc
`postgres:18`, loaded with the same rows, orders them differently, which shows the probe can detect a
collation change. So switching an existing `postgres:18-alpine` data directory to this image needs no
`REINDEX`, and the rollback is the image change in reverse.

**What it adds.** `vector` 0.8.7 is available, so schema-init creates `public.artist_embeddings` and the
`embedding_pipeline` role (ADR 0013), and database-schema's pgvector integration scenarios run instead
of skipping. `pg_stat_statements` ships with PostgreSQL. `docker-compose.prod.yml` also sets
`pg_stat_statements.track_utility=off`, so `ALTER ROLE ... PASSWORD` is never stored in plaintext.

**Operator steps (dedicated stack).**
1. Run `just env-init`. It adds `POSTGRES_IMAGE` to `.env`, and Compose refuses to start without it.
2. Recreate only PostgreSQL in a quiet window: `up -d --no-deps postgres`. The new image and
   `track_utility=off` both take effect on restart.
3. Run `schema-init` as usual. It creates `vector` and the vector schema, and `pg_stat_statements`,
   because the stack connects as its superuser.
4. `pg_stat_statements` keeps its statistics across a clean restart, so statements stored while
   utility tracking was on survive it. Check with
   `SELECT count(*) FROM pg_stat_statements WHERE query ~* '\m(password|encrypted)\M';`.
   If the count is not zero, remove only those entries, which is a separate approval:
   `SELECT pg_stat_statements_reset(userid, dbid, queryid) FROM pg_stat_statements WHERE query ~* '\m(password|encrypted)\M';`.
   Then rotate the passwords they contained.

**The shared server (lux)** is homelab-xp95. Nothing in this repository changes lux. lux already has
`pg_stat_statements` 1.12 in `groovemap` with `track_utility` off (homelab-v3cq). The administrator pins
the same digest and restarts onto it, then a superuser creates `vector` in `groovemap`
([homelab.md](homelab.md#what-groovemap-needs-from-each-server)). GrooveMap's role gets no
`pg_read_all_stats`: it sees its own statements, and the grant would expose other tenants' query text.

`check-images.py` moves `postgres` from `THIRD_PARTY_REPOSITORIES` to the reviewed GrooveMap images
(`POSTGRES_IMAGE`) and records `RELEASED_IMAGE_VERSIONS["POSTGRES_IMAGE"]` as `18.6.0`. MAJOR.MINOR is
the PostgreSQL release; the last number is that repository's own revision. `store-check-postgres`, the
homelab smoke's stand-in server and `just smoke-infra` run the same reviewed image.

### Promotion of musicbrainz-ingestion v0.4.1 (2026-11-07)

One image: `musicbrainz-ingestion:v0.4.1`, the fix for the first-load download phase. v0.4.0
re-encoded each entity from the dump tarball with a single-threaded xz preset 6, about 3.5 MB/s, roughly
20 hours for `release` before the first record was parsed. v0.4.1 uses a multi-threaded xz encoder at
preset 1 (about 105 MB/s on the repository's measurement; the output is about 30% larger). The output
is still a standard `.jsonl.xz`, and files written by v0.4.0 are still accepted and skipped. Nothing here
has been run on lux by this repository.

`gh attestation verify oci://ghcr.io/groovemap-music/musicbrainz-ingestion@<digest> --owner groovemap-music
--signer-repo groovemap-music/automation` exited 0. On `linux/amd64`, `org.opencontainers.image.revision`
equals the commit `v0.4.1` points to, `org.opencontainers.image.version` is `v0.4.1`, and the image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `musicbrainz-ingestion:v0.4.1` | `sha256:b3f03785faa03b0d445b74a3ab31f82d842bc0e739500b2021ac515fcf6a712c` | `6b8734a923a1f63f1febd527d85b1784f67d5a39` | `v0.4.0` — `sha256:7809c365595421c742703095e6260f54d4b06309bd0b5f8cd9dcae66634ec9a0` |

**Superseded:** the `v0.4.0` pin in the 2026-11-07 release set below; it remains the rollback target.

**Tuning.** `MUSICBRAINZ_XZ_THREADS` sets the encoder thread count (default `min(cores, 6)`; zero or an
invalid value falls back to the default). Compose does not set it, so add it to the `extractor-musicbrainz`
environment only to change it. No other setting, volume or pair floor changes.

### Promotion of database-schema v0.9.0 (2026-11-07)

`database-schema:v0.9.0` resolved from its released tag on GHCR (the index digest). `gh attestation verify
oci://ghcr.io/groovemap-music/database-schema@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0. On `linux/amd64`, `org.opencontainers.image.revision` equals the commit
of tag `v0.9.0`, `org.opencontainers.image.version` equals the tag, and the image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.9.0` | `sha256:1076fef32c5b922b9228ab5b257e75143d5ee58d964b75a7c6f37dc9a539461b` | `37ae56b2be8eb398b674662f6a8938e6257af1c2` | `v0.8.0` — `sha256:43a7bb3299544a076786741b26d7491989c0977bf7d0f5c68522f506b77a6ef5` (image only; see below) |

**Supersedes** `database-schema:v0.8.0` in the 2026-11-07 record below; every other digest there stands.

**Breaking.** `v0.9.0` drops `musicbrainz.relationships_natural_key`, so the hash `relationships_key` is the only
conflict target. Only `musicbrainz-sql-loader` `v0.6.0` or later can write there: an older writer's upsert
fails with "there is no unique or exclusion constraint matching the ON CONFLICT specification". The drop is
guarded and instant on an empty table, so apply it before MusicBrainz is enabled. There is no schema rollback:
the rollback digest above is for the image pin only, and never runs `schema-init` again.

**Paired floors.** `PAIRED_IMAGE_FLOORS` now has two rows: `discogs-sql-loader` `0.5.0` with `database-schema`
`0.8.0` (unchanged), and `musicbrainz-sql-loader` `0.6.0` with `database-schema` `0.9.0`. The second closes the gap
the 2026-11-07 record names: `check-images` now refuses the MusicBrainz loader on an older schema.

### Promotion of the 2026-11-07 release set

Eleven images. Each digest is the index digest of the released tag on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for all eleven. On `linux/amd64`, `org.opencontainers.image.revision` equals the
commit the release tag points to in its repository, `org.opencontainers.image.version` equals the tag,
and every image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.8.0` | `sha256:43a7bb3299544a076786741b26d7491989c0977bf7d0f5c68522f506b77a6ef5` | `b24a01d8537ba7e26e0e812032a1e9f413d2caea` | `v0.7.0` — `sha256:da6e53bb865f3172161e07fcca34d8fecef587facdf566ae6efff378e9ebc771` |
| `catalog-api:v0.8.0` | `sha256:4b41ea68cafe2817d25f3d17946747d2f1729c6e1039fc4e6943f8e33f809653` | `18514b594a0460b5d1cef3813bc2c68af1b58fe2` | `v0.7.0` — `sha256:143d5dd329adfe42a2ba9fe8785df45430603e07aac807af0cc8471263ec927a` |
| `discogs-sql-loader:v0.5.0` | `sha256:5a77fbdeb44759e3547b52bad7b9193a3d7f36dbb41668b553fedb2a1bff5f17` | `f5d2cbfa5f699c76fe9262086f23ad24843f06f4` | `v0.4.5` — `sha256:dc74e541b444a943f0e155fc07dd779047ab74f892e9074478eee954e0fe434f` |
| `discogs-graph-enricher:v0.6.2` | `sha256:d13b496070b4deba6756d5f572f9b7c10bff9982257ef2739f99a6038e91b487` | `5390fbcec50e4eb1b9f2a6a2c817a1e9d288727c` | `v0.6.1` — `sha256:0e24f5dc94a6f5d58da35cfe351b877f137e6d928f0b485c4eb64ca22513c1ee` |
| `analytics-engine:v0.6.1` | `sha256:a88d82a0486840d9e89ac75cd91e263b642d6b1cb6a33357d7fe2f73e156caf1` | `c96e4e05bdf0d184a51ca5aa7ab45281805db975` | `v0.6.0` — `sha256:000a053102c52f377a9f6ff2edd492c205fbb1973d55fcad168ecc7abb81d031` |
| `graph-explorer:v0.3.3` | `sha256:885991da887c8f139b9ad5216c2e9b4a8cc95da9171e39b5e8d49ce56c8df2ce` | `007cb3d0cc22d128d0c8b86f8d7d4a49b20aa775` | `v0.3.2` — `sha256:da5b49f70a2d8fdd0c6254e6cf9de2b7c20da79ce90d8922a8349aff068a9fb8` |
| `operations-console:v0.3.1` | `sha256:07825a1c1c4ca622a3199364085b5821f2ebb69e004547dc4734de8216156816` | `c3cb805d9817bec714ff000a97bb9a907f125909` | `v0.3.0` — `sha256:1d9e480b0404434a48e394d7e062d3f54eff25ac48a6a8d45ba015833b11d691` |
| `discogs-ingestion:v0.4.1` | `sha256:aff3558504b8166ec8fb11a96b48eaee2754a57209b4b57a5f39603000dc74d9` | `941ae4ef6acf53a94a6c362742cf31711864c95b` | `v0.4.0` — `sha256:881d8f96842e17fa9dc3f2d43fab1a18abcee664e5bc2ec0374b45f09cceaa8d` |
| `musicbrainz-ingestion:v0.4.0` | `sha256:7809c365595421c742703095e6260f54d4b06309bd0b5f8cd9dcae66634ec9a0` | `0e03d994bc9bfce0ad58871099ee7f28dc9cc372` | `v0.3.0` — `sha256:7599975c51429dc5bbfa30a9e55a28bed6fd1afef8f3a9a5dfd7c0286d4da89a` |
| `musicbrainz-sql-loader:v0.6.0` | `sha256:022fa4d665ac46203cb31b5bf94dd6dc825a3cf69e70a497a1c3a9d0efa2d8d9` | `3a656c23a0b3d2f010cb64596a2608ca7026946c` | `v0.5.0` — `sha256:2de906859c354f5a69ffa3eca20febe51fa4eb043857c3b6b83287b67c63b809` |
| `musicbrainz-graph-enricher:v0.5.0` | `sha256:f382c6d1497fb598948876acb310ec355f98d99fbffe5820582e30df6d0c5329` | `410fa4e5d0617f656fa9ea353713720b8be6f7e4` | `v0.4.0` — `sha256:e9fb87302678e4d8b3f681f9db4a509f2ed0eba0fb2378a8da18f846fd979057` |

**Superseded:** every previous digest in this file for these eleven images. They remain as rollback
targets, except that the schema never rolls back (see the Lux section in [homelab](homelab.md#lux-upgrade-the-2026-11-07-release-set)).

**Paired floor.** `PAIRED_IMAGE_FLOORS` is now `discogs-sql-loader` `0.5.0` with `database-schema` `0.8.0`
(`gm-discogs-sql-loader-gna2`), so `check-images` refuses the loader against any older schema. The
paired row covers only the Discogs loader: `musicbrainz-sql-loader` `v0.6.0` also writes `purge_vetoed` and
needs the same schema, which the deploy order below honors, but `check-images` does not enforce it.
The other floors are unchanged and met (`EXTERNAL_STORE_FLOORS`, `CONSOLE_TARGETS_FLOOR`).

**Compatibility.**

- `analytics-engine` `v0.6.1` vendors internal-insights contract `1.7.0`; `catalog-api` `v0.8.0` serves
  `1.10.0`. The changes are additive (1.8.0 and 1.9.0 publish 300 s budgets for genre-trends and
  label-longevity, 1.10.0 adds `include_placeholders`) and every path is unchanged, so `1.7.0` keeps working. The
  engine's 900 s default read timeout is above those budgets. One behavior change reaches it: label-longevity now
  leaves placeholder labels such as Not On Label out by default, and the engine does not pass `include_placeholders`.
- `purge_vetoed` is a column on `loader_extraction_latch`, first in `database-schema` `v0.8.0`, written by
  `discogs-sql-loader` `v0.5.0` and `musicbrainz-sql-loader` `v0.6.0` (the schema's compatibility file lists both
  as writers). Both loaders need the schema first; against `v0.7.0` the Discogs loader declines the latch and
  reports degraded.
- `graph-explorer` `v0.3.3` renders a null `completeness_pct` as n/a. `database-schema` `v0.8.0` makes that column
  nullable, so `graph-explorer` goes live before the schema.

### Promotion of the reset release set (2026-11-05)

This promotes six images for the Lux reset (a fresh database, then the load): `database-schema`
`v0.7.0`, `catalog-api` `v0.7.0`, `discogs-sql-loader` `v0.4.5`, `discogs-graph-enricher` `v0.6.1`,
`analytics-engine` `v0.6.0` and `graph-explorer` `v0.3.2`. `operations-console` stays at `v0.3.0`.
Nothing here has been run on lux by this repository.

Each digest is the index digest of the released tag on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for all six. The `linux/amd64` image labels were read back:
`org.opencontainers.image.version`, `.revision` and `.source` match the release, each tag's commit
on GitHub equals the label revision, and each image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.7.0` | `sha256:da6e53bb865f3172161e07fcca34d8fecef587facdf566ae6efff378e9ebc771` | `7937373f328e21efd745fe51357600d564a2ae75` | `v0.6.3` — `sha256:4b4bd73446996751f703a65b8599b9cc2d5d3e76579bf2afd5cdd5524785b7a1` |
| `catalog-api:v0.7.0` | `sha256:143d5dd329adfe42a2ba9fe8785df45430603e07aac807af0cc8471263ec927a` | `9b5e60921db40320375586418c683cc5e9f80a85` | `v0.6.0` — `sha256:b41e01f57c7c3ac0f52fc26474145508b1755bb6275a08f0a2e41931a9c437d5` |
| `discogs-sql-loader:v0.4.5` | `sha256:dc74e541b444a943f0e155fc07dd779047ab74f892e9074478eee954e0fe434f` | `673ac13f29c3d005f60bcac2320ba7433c434e91` | `v0.4.4` — `sha256:ff45d7eaff9c5f506fcd30ce89bd1f1fffcc5b069651b559464b5dd3d6706278` |
| `discogs-graph-enricher:v0.6.1` | `sha256:0e24f5dc94a6f5d58da35cfe351b877f137e6d928f0b485c4eb64ca22513c1ee` | `1c3fd896f6b43ef39e035c47aeacc02d93e65109` | `v0.6.0` — `sha256:705a607de44ccfd8dc039059a05e18ca1b4cc77dc9c6e0caf44d2594caa5b0ab` |
| `analytics-engine:v0.6.0` | `sha256:000a053102c52f377a9f6ff2edd492c205fbb1973d55fcad168ecc7abb81d031` | `20bf802522ddbf086ecf7e3827c0ffd698856fd7` | `v0.5.0` — `sha256:bf1a3944a9f0b2d2a85f9470423ce0422e4433acb585ff3fd1b4e0763f116bfa` |
| `graph-explorer:v0.3.2` | `sha256:da5b49f70a2d8fdd0c6254e6cf9de2b7c20da79ce90d8922a8349aff068a9fb8` | `ca58621a54b8294140051c5289d9ab4765423861` | `v0.3.1` — `sha256:dc3c23e2adaeb8659fa97e92654775d65844a86076ee0a0b635246d5ca7b2c8a` |

**Superseded:** `database-schema` `v0.6.3`, `catalog-api` `v0.6.0`, `discogs-sql-loader` `v0.4.4`,
`discogs-graph-enricher` `v0.6.0`, `analytics-engine` `v0.5.0` and `graph-explorer` `v0.3.1`. Their
records below stay for history; the digests above are the only ones to use for a new deploy. The
older ones remain as rollback targets, with the caveat below for the schema.

**Capability floors.** Checked against every service in the set:

- `catalog-api` `v0.7.0` reads the Identity Ledger by relation name, so it runs on the `v0.6.x`
  materialized views and on the `v0.7.0` tables. Older `catalog-api` answers 503 on the ledger routes once
  the `v0.7.0` schema is applied, so it goes first or together with the schema (together on the fresh
  database).
- `analytics-engine` `v0.6.0` writes `label_ids` and `artist_ids` and needs `catalog-api` `v0.7.0`
  (contract `1.7.0`) and schema `v0.7.0`; before the schema is live every rarity run fails with
  `UndefinedColumn`. It goes last.
- `discogs-sql-loader` `v0.4.5` and `discogs-graph-enricher` `v0.6.1` strip padding from a credit name
  before it keys a Person. Release them together: stripping in one store splits the Person keys.
- `graph-explorer` `v0.3.2` pages every credit and needs `catalog-api` `v0.7.0`: an older API would
  show only the first 100 credits with no error.
- `operations-console` `v0.3.0` (`CONSOLE_TARGETS_FLOOR`, met) and `mcp-server` `v0.2.2` (not a
  pinned image here) each carry a copy of their `catalog-api` route contract. The SHA-256 of each copy
  equals the producer's file at `catalog-api` `v0.7.0` (`api/contracts/operations-console/v1/routes.json`
  `5bb3cad3…`, `mcp-server/v1/routes.json` `cbbfa996…`; `graph-explorer/v1/routes.json` `7a637945…`
  matches `graph-explorer` `v0.3.2` as well), so neither needs a new release.
- `EXTERNAL_STORE_FLOORS` (`database-schema` `0.6.0`; `catalog-api` `0.6.0` and `analytics-engine`
  `0.5.0`) are all met by this set.
- **`PAIRED_IMAGE_FLOORS` stays `UNRELEASED`** for `gm-discogs-sql-loader-gna2` and its schema column
  (`purge_vetoed`): neither `discogs-sql-loader` `v0.4.5` nor `database-schema` `v0.7.0` contains it
  (`gm-deployment-16kv`'s floors are not filled in by this change).

**Rollback.** Set the pin back to the digest in the table (or to the pin in the `.env.<timestamp>.bak`
that `env-init` left, which holds what lux actually ran) and recreate only that service with
`up -d --no-deps`. Never run an older `schema-init`: `v0.7.0` keeps the Identity Ledger as
plain tables (they were materialized views through `v0.6.3`), and an older `catalog-api` answers 503 on them (above), so roll `catalog-api`
back only together with the schema it matches.

Evidence still owed (not produced by this change): the combined Linux `smoke-homelab` run on these
digests reaching gate 0. It is a CI job (`.github/workflows/smoke-homelab.yml`, linux/amd64) that
needs a push, which this change does not do; the dispatcher pushes the branch, and the run URL is
recorded here before merge to main. Run URL: [42125594718](https://github.com/groovemap-music/deployment/actions/runs/42125594718) (passed on `219edf3`). See the
[Lux reset window](homelab.md#lux-reset-the-release-set-on-a-fresh-database) for the order.

### Promotion of discogs-sql-loader v0.4.4 and discogs-graph-enricher v0.6.0 (2026-11-05)

> **Superseded** by the [reset release set promotion](#promotion-of-the-reset-release-set-2026-11-05): `discogs-sql-loader` `v0.4.4` and `discogs-graph-enricher` `v0.6.0` are now rollback targets only.

This replaces the `discogs-sql-loader` `v0.4.3` and `discogs-graph-enricher` `v0.5.0` pins of the
[v0.6.3 record](#promotion-of-database-schema-v063-and-discogs-sql-loader-v043-2026-11-04), which stays as the record of what was reviewed first. `database-schema`
stays at `v0.6.3` (`sha256:4b4bd73446996751f703a65b8599b9cc2d5d3e76579bf2afd5cdd5524785b7a1`).
`v0.4.4` carries the leak fix and `v0.6.0` the stats refresh. **Do not use `v0.4.3` or `v0.5.0`
for a new deploy**; they remain only as rollback targets (below).

Each digest is the index digest of the released tag on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for both. The `linux/amd64` image labels were read back:
`org.opencontainers.image.version`, `.revision` and `.source` match the release, each tag's commit
on GitHub equals the label revision, and each image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `discogs-sql-loader:v0.4.4` | `sha256:ff45d7eaff9c5f506fcd30ce89bd1f1fffcc5b069651b559464b5dd3d6706278` | `01524449a40cba615bb89748ed7d11ab938929db` | `v0.4.3` — `sha256:9bb930d1f5aa6687d774c1e981dfdbae50086a1d88e1d04110a58326114e32c3` |
| `discogs-graph-enricher:v0.6.0` | `sha256:705a607de44ccfd8dc039059a05e18ca1b4cc77dc9c6e0caf44d2594caa5b0ab` | `ac67d919e3f86114a44914824f946bed5b5017b3` | `v0.5.0` — `sha256:0baf478b8203c19d095dda1ccf1cb7fbf7957ad763234da9c6534a660f18f79b` |

**Superseded:** `discogs-sql-loader` `v0.4.3` and `discogs-graph-enricher` `v0.5.0`. Their records
below stay for history. `v0.4.2`, `v0.4.1` and `v0.6.1`/`v0.6.2` remain unusable as before.

Rollback is a pin change back to the digests in the table (or to the pins in the
`.env.<timestamp>.bak` that `env-init` leaves, which hold what lux actually ran), recreating only
the affected service with `up -d --no-deps`. `v0.6.0` writes Neo4j properties (`stats_version`,
`Artist.release_count`, `HAS_STYLE` edges) that `v0.5.0` ignores; rolling back does not remove them.
Never run an older `schema-init`.

Evidence still owed (not produced by this change): the combined Linux `smoke-homelab` run on these
digests reaching gate 0. It is a CI job (`.github/workflows/smoke-homelab.yml`, linux/amd64) that
needs a push, which this change does not do; the dispatcher pushes the branch, and the run URL is
recorded here before merge to main. Run URL: [42077610387](https://github.com/groovemap-music/deployment/actions/runs/42077610387) (passed on `fb902f5`). See the
[Lux upgrade](homelab.md#lux-upgrade-sql-loader-v044-and-graph-enricher-v060-staged-after-schema-v063) for the window.

### Promotion of database-schema v0.6.3 and discogs-sql-loader v0.4.3 (2026-11-04)

> **Partly superseded** by the [v0.4.4 and v0.6.0 promotion](#promotion-of-discogs-sql-loader-v044-and-discogs-graph-enricher-v060-2026-11-05): the `discogs-sql-loader` `v0.4.3` and `discogs-graph-enricher` `v0.5.0` pins below are now rollback targets only. The `database-schema` `v0.6.3` pin is current.

This replaces the pins of the promotion below, which stays as the record of what was reviewed
first. **Do not use `discogs-sql-loader` `v0.4.1`** (`sha256:9a1bc0108afadfc945606098bde4950bc42b53148bf3e9ca07c50f71feea669f`): it has a regression
that closes healthy consumers, and it failed the homelab smoke (run 41895091506). **Do not use
`database-schema` `v0.6.1` or `v0.6.2`**: `v0.6.3` supersedes both. Nothing was deployed on any
of them.

Each digest is the index digest of the released tag on GHCR. `gh attestation verify
oci://ghcr.io/groovemap-music/<repo>@<digest> --owner groovemap-music --signer-repo
groovemap-music/automation` exited 0 for both. The `linux/amd64` image labels were read back:
`org.opencontainers.image.version`, `.revision` and `.source` match the release, and each image
user is `1000:1000`. `discogs-sql-loader` `v0.4.3` was verified the same way (attestation exit 0, revision `c74843c`).

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.6.3` | `sha256:4b4bd73446996751f703a65b8599b9cc2d5d3e76579bf2afd5cdd5524785b7a1` | `6489067d684775821f4802f72dccd55f6583546d` | none: see below |
| `discogs-sql-loader:v0.4.3` | `sha256:9bb930d1f5aa6687d774c1e981dfdbae50086a1d88e1d04110a58326114e32c3` | `c74843c05b45ea047911c324966ee59884745c49` | `v0.4.0` — `sha256:be15077013672f69384bd75fd591801bdabd80400eaa2fd4a686e2d82f6bbb0b` (never `v0.4.1`) |

Unchanged: `discogs-graph-enricher` `v0.5.0` (`sha256:0baf478b8203c19d095dda1ccf1cb7fbf7957ad763234da9c6534a660f18f79b`,
revision `aebfe0bf`) and `graph-explorer` `v0.3.1` (`sha256:dc3c23e2adaeb8659fa97e92654775d65844a86076ee0a0b635246d5ca7b2c8a`,
revision `dc9dbeeb`).

Why: `database-schema` `v0.6.3` carries the role-category speedup and the one-hash credit key, and
`discogs-sql-loader` `v0.4.3` is the first loader after `v0.4.0` without the `v0.4.1` regression, and adds set-based sorted writes.

History: `v0.4.2` (`sha256:5c4f064be5c2a26320f3274ee741275248e1cc60c5f758d8a7666ce605979f13`, revision `2b4340cd6f9ef5d10607a6b054a1b2fb5070c58c`) was the pin first reviewed here, and homelab smoke run [41922703117](https://github.com/groovemap-music/deployment/actions/runs/41922703117) passed on it (gate 0, 37 passes, branch commit `1ee9cb2`). `v0.4.3` superseded it before anything was deployed, so that run is not evidence for the `v0.4.3` digest; `v0.4.2` is not a rollback target either.

`database-schema` `v0.6.3` cannot be rolled back. Once it has run, no older `schema-init` may run
against that database. The previous pin (`v0.6.0`, `sha256:a16e4a2c5139e065a6e2a71390279732b349e58347aab8bdef33aa6e0f9b1465`)
is recorded for history only, and `v0.6.1` and `v0.6.2` are not rollback targets. The SQL loader
rolls back by a pin change to its digest above, on the untested assumption that `v0.4.0` still
runs against the `v0.6.3` schema.

Evidence still owed (not produced by this change): the combined Linux `smoke-homelab` run on these
digests (schema `v0.6.3`, loader `v0.4.3`) reaching gate 0. It is a CI job (`.github/workflows/smoke-homelab.yml`, linux/amd64) that
needs a push, which this change does not do; the dispatcher pushes the branch, and the run URL is
recorded here before merge to main. Run URL: [41928210952](https://github.com/groovemap-music/deployment/actions/runs/41928210952) (passed on `16fb1d1`: gate 0, 37 passes, 0 false alarms, 4 `extraction_complete`). See the
[Lux upgrade](homelab.md#lux-upgrade-schema-init-v063-sql-loader-v044-graph-enricher-v060) for the window.

### Promotion of database-schema v0.6.1, discogs-sql-loader v0.4.1, discogs-graph-enricher v0.5.0 and graph-explorer v0.3.1 (2026-11-04)

> **Superseded** by the [v0.6.3 and v0.4.3 promotion](#promotion-of-database-schema-v063-and-discogs-sql-loader-v043-2026-11-04). Its `discogs-sql-loader` `v0.4.1` and `database-schema` `v0.6.1` pins must not be used.

Each digest was resolved from its released tag on GHCR (the index digest, with a `linux/amd64`
image manifest) and verified with `gh attestation verify oci://ghcr.io/groovemap-music/<repo>@<digest>
--owner groovemap-music --signer-repo groovemap-music/automation` (exit 0 for all three). The
`linux/amd64` image labels were read back: `org.opencontainers.image.version`, `.revision` and
`.source` match the release, and each image user is `1000:1000`.

| Image | Reviewed digest | Source revision | Rollback tag and digest |
| --- | --- | --- | --- |
| `database-schema:v0.6.1` | `sha256:591996b6fb1f07af8e795fe5ebd0cc08592a7702427a7b69c733d80911db1319` | `707b24121c51d1f6458df13e4576bb2ea3cb2792` | none: see below |
| `discogs-sql-loader:v0.4.1` | `sha256:9a1bc0108afadfc945606098bde4950bc42b53148bf3e9ca07c50f71feea669f` | `aa0dfb83129a04551c6223151674b00f14e308d8` | `v0.4.0` — `sha256:be15077013672f69384bd75fd591801bdabd80400eaa2fd4a686e2d82f6bbb0b` |
| `discogs-graph-enricher:v0.5.0` | `sha256:0baf478b8203c19d095dda1ccf1cb7fbf7957ad763234da9c6534a660f18f79b` | `aebfe0bfb8a2a2537afd32e721874934553f2290` | `v0.4.0` — `sha256:6f5dd84d18aa69cc40705bbabde269cefc7d7642e0818f1db2fba2588bde9c39` |
| `graph-explorer:v0.3.1` | `sha256:dc3c23e2adaeb8659fa97e92654775d65844a86076ee0a0b635246d5ca7b2c8a` | `dc9dbeeb71cad7594e2003c3e71833b830c1c679` | `v0.3.0` — `sha256:ad1c3b6a7e21beb9393fca5057050ff883475a72972ee11e8c7eed5863b5d31e` |

Why: the first Discogs load on lux is stalled and Explore hangs on a large artist; these releases
carry the fixes (see each repository's release notes). `graph-explorer` `v0.3.1` was published
after the other three and is verified the same way (attestation exit 0, linux/amd64 labels).

`database-schema` `v0.6.1` cannot be rolled back. Once it has run, no older `schema-init` may run
against that database, so the previous digest (`v0.6.0`, `sha256:a16e4a2c5139e065a6e2a71390279732b349e58347aab8bdef33aa6e0f9b1465`)
is recorded for history only. The two consumers roll back by a pin change to their digest above,
on the untested assumption that SQL loader `v0.4.0` and graph enricher `v0.4.0` still run against the `v0.6.1` schema.

Where the old digests remain on purpose: the earlier promotion records in this file and the
2026-11-01 observed-evidence table in the testing guide describe the images those runs used, and
`KNOWN_SQL_LATCH_IMAGE` in `scripts/smoke-homelab.sh` keeps SQL loader `v0.4.0` as an explicit
smoke candidate. Everything that states the *current* pin was updated, and
`test_released_versions_match_the_comments_beside_the_digests` keeps each digest and version in step.

Evidence produced here: on an arm64 Mac with the images emulated as `linux/amd64`, in a disposable
Docker network with the stack's pinned PostgreSQL 18 and Neo4j, `schema-init` `v0.6.0` built a fresh
database and exited 0; rows were inserted (two `public.artists`, one `loader_extraction_latch`);
`v0.6.1` then ran over that data, exited 0 and left every row intact; a second `v0.6.1` run also
exited 0. Everything was removed afterwards. This does not stand in for the combined smoke.

Evidence still owed (not produced by this change): the combined Linux `smoke-homelab` run on these
digests is a CI job (`.github/workflows/smoke-homelab.yml`, linux/amd64) and needs a push, which
this change does not do. See the [Lux upgrade](homelab.md#lux-upgrade-schema-init-v063-sql-loader-v044-graph-enricher-v060)
for the window.

Rolling back a consumer is a pin change to its rollback digest above for that image alone.

### Identifier lookup verification (2026-09-21)

The GHCR tag manifests for `linux/amd64` were checked before the disposable
smoke. The changed image index digests were:

| Released tag | Reviewed digest | Rollback tag and digest |
| --- | --- | --- |
| `discogs-sql-loader:v0.3.0` | `sha256:09f55827f972ec289baad7128acca061739fd9d4d350f23f3d6d22afeafee7e6` | `v0.2.0` — `sha256:dfa00f9ee24d9fab6212b02a272486f70490b741e9556edf0b2fd2c793f3393c` |
| `discogs-graph-enricher:v0.3.0` | `sha256:e95fabb7633859c94e0913f9122ccbaed18a04a017c486886c38840c230ff80d` | `v0.2.0` — `sha256:933df432732e8f1b863f1b3e3945ff0619a141e1708889a05f9f4dcf2003335b` |
| `catalog-api:v0.4.0` | `sha256:4889f1ce04568a335bdfe698e11a3c8133238e0b0b61c91447ea4448a3e3ae43` | `v0.2.0` — `sha256:b236b3ac805e4e92f7d9cc889d0e15d966a293d764af860d02c46b15efc9c5cb` |

`database-schema:v0.5.0` was pinned at
`sha256:06fcc517d7f41075eff4d7ab202dd928b9069abc0bc39fabead4740672197fda`
until the 2026-10-31 promotion below.
Each tag resolved to its recorded index digest; each index contained a
`linux/amd64` image manifest. The rollback tags were also re-resolved against
GHCR, not just copied from an older document.

Run command, from the issue worktree with an ignored, digest-pinned `.env`:

```sh
env -u DOCKER_DEFAULT_PLATFORM \
  SMOKE_MEDIA_PROJECT=groovemap-id-87i1-smoke4 \
  SMOKE_MEDIA_PROJECT_DIRECTORY=/path/to/shared/checkout \
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
`extractor-discogs`. The MusicBrainz ingestion path (`extractor-musicbrainz` and its two
consumers, `brainzgraphinator` for graph enrichment and `brainztableinator` for PostgreSQL)
therefore sits behind the `musicbrainz` Compose profile, so a plain `up` leaves it out. None
of the three is a dependency of any other Compose service, so nothing else fails to start
without them. Leave `COMPOSE_PROFILES` unset in `.env` for the first load.

`just deploy` automates this whole sequence: it starts the stack without the profile, waits
on the gate described below, then sets `COMPOSE_PROFILES=musicbrainz` and starts MusicBrainz
(see the [homelab guide](homelab.md#the-first-load-discogs-before-musicbrainz)). The manual
steps that follow are what it runs.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait
```

For a local (non-production) stack, drop the two `-f` flags; [Quick
start](quick-start.md#4-start-only-with-operator-approval) shows the production invocation
these follow.

"Extraction finished" is not "import finished": the extractor publishes to RabbitMQ
asynchronously, so the Discogs consumers (`graphinator`, `tableinator`) have to drain too. The
admin panel's extraction history only records admin-*triggered* runs, so it does not cover this
automatic first run. Ask the stack instead (read-only; it only runs queries through
`docker compose exec`, so the host needs no database clients):

```bash
just discogs-import-status --prod                # one report; exit 0 complete, 3 in progress, 2 cannot determine
just discogs-import-status --prod --wait         # poll every 30s (--interval N) until complete
```

Drop `--prod` for a local stack; `-f FILE` (repeatable), `--env-file`, and `-p/--project-name`
pass through to Compose, `--timeout N` bounds `--wait`, and `--json` prints the final report
for a script. Interrupting `--wait` changes nothing, so it is safe to re-run. It reports
complete only when all of these hold for the newest Discogs dump version:

- PostgreSQL `loader_extraction_latch` (`loader = 'discogs'`) has all four `signals`
  (`artists`, `labels`, `masters`, `releases`) **and** `refreshed_at` set. `discogs-sql-loader`
  stamps `refreshed_at` in the transaction that commits its derived-relation pass, so four
  signals without it means that pass is still running or retrying (the report shows the
  `loader_derived_refresh_job` state and last error when there is one).
- Neo4j's `(:ExtractionCompletion {version})` for that same version has all four signals, and
  the graph has not started a newer version than PostgreSQL has.
- The eight `groovemap-discogs-graphinator-*` and `groovemap-discogs-tableinator-*` queues are
  at zero ready and zero unacknowledged messages. Attached consumers are *not* required: both
  consumers cancel their consumers `CONSUMER_CANCEL_DELAY` (300s) after finishing and re-check
  hourly, so a finished import normally shows none. A backlog with no consumer attached is
  called out. Messages in the `.dlq` queues are printed as warnings; they do not gate.

A previous dump's complete rows never count for the current one. A stopped service, a missing
relation or queue, or output the script cannot read is exit 2, never 0. The RabbitMQ
management UI at <http://localhost:15672> (see [Queue monitoring](monitoring.md#queue-monitoring))
and the admin panel's Queue Trends tab show the same queues by hand. Once it reports complete,
deploy the MusicBrainz ingestion path:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile musicbrainz up -d --wait
```

To keep the path enabled on later `up` runs, set `COMPOSE_PROFILES=musicbrainz` in `.env`.

The graph enricher's post-import maintenance (stub cleanup and aggregate stats) runs detached
after its fourth signal and records no durable completion marker, so the gate cannot see it;
look for `Post-import maintenance complete` in the `graphinator` logs if that matters for the
step you are about to take.

**Later cycles.** `extractor-discogs` and `extractor-musicbrainz` each then run on their own
periodic schedule (`PERIODIC_CHECK_DAYS`: 5 days and 3 days) with the same no-ordering
behavior, so nothing in the stack today keeps a later Discogs cycle ahead of the MusicBrainz
one it should precede — that is a known gap, not something this procedure enforces. The
re-attachment job below heals the resulting drift after the fact, and it runs by itself: this
stack sets `IDENTITY_AUTO_REATTACH_ENABLED=true` on the `api` service, so `catalog-api` re-attaches
and projects once per completed Discogs extraction. Do nothing by hand for a routine cycle. Run
the manual steps only for a dry run, a forced re-run, a failed or interrupted automatic run, or
a stack that sets the switch to `false`, and then once the Discogs import has completed and its
consumer queues have drained, using the same `just discogs-import-status` check as above (or,
for an admin-triggered re-run, the extraction history row reaching `completed`).

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
   per-kind outcomes. Either path writes one `admin_audit_log` row under that job id first,
   before any identity write, as `identity.reattach.started`; a run whose row cannot be
   written does not start. The row becomes `identity.reattach.apply` when the run finishes, or
   `identity.reattach.failed` when it fails.
3. Then project `gm_id` onto Neo4j so the graph follows the alias table:

   ```bash
   docker exec groovemap-api catalog-identity-projection
   ```

   Alternative: `POST /api/admin/identity/project`.

The automatic watcher below runs this sequence after the first full load and after every
Discogs import, so run it by hand only in the cases listed under **Later cycles** above.
MusicBrainz publishes twice weekly and Discogs monthly, so new MusicBrainz-to-Discogs links
keep arriving ahead of their Discogs targets between Discogs cycles.

**Automatic by default.** `docker-compose.yml` sets `IDENTITY_AUTO_REATTACH_ENABLED: "true"` on
the `api` service (`groovemap-api`), inherited by the production and smoke overlays and
overridable in `docker-compose.prod.yml`'s, so `catalog-api` runs the re-attachment (apply) and then the
`gm_id` projection by itself, once per completed Discogs extraction, detected the same way as
above from `loader_extraction_latch`. `IDENTITY_AUTO_REATTACH_INTERVAL` (seconds, default 300)
sets how often it polls; the watcher idles until the relation exists.
Every replica polls, but a PostgreSQL advisory lock and a handled marker in `admin_audit_log`
keep a run to exactly once per extraction: the watcher writes its row first, before any
identity write, as `identity.reattach.auto.started` (a run whose row cannot be written does
not start), then to `identity.reattach.auto` when it finishes — that row is the durable
handled marker — or to `identity.reattach.auto.failed` when it fails. Neither started nor
failed is the marker, so a failed or interrupted run is retried under a new job id. The
first-load staged deploy and the MusicBrainz-after-Discogs cycle order above still apply — the
switch only replaces the manual `catalog-identity-reattach` / `catalog-identity-projection`
runs above, not the ordering they depend on. See
[`catalog-api`'s README](https://github.com/groovemap-music/catalog-api/blob/main/api/README.md#running-both-automatically-after-each-discogs-import)
for the full design.

The Identity Ledger's views refresh on their own: inline after every applying re-attachment run,
and after loads by the `IDENTITY_LEDGER_REFRESH_ENABLED` poll, which this stack turns on (see
[Identity ledger refresh](configuration.md#identity-ledger-refresh)). Neither path needs a manual step.

Operator notes:

- Safe to re-run: a second run reports the already-repaired and already-guarded items as
  `unchanged`.
- A row left at `identity.reattach.started` or `identity.reattach.auto.started` means the run
  was interrupted or its finalize update failed — check the API logs. Re-run the manual steps
  above yourself; the watcher retries an automatic run on its own, under a new job id. Either
  way the earlier row is untouched, and every supersession that named it as `decision_ref`
  still resolves.
- The exit code reflects only run-level failure. Read the per-item `reattached`, `guarded`,
  `unchanged`, and `failed` counts from the printed report, not the exit code.
- A guarded item — a split native id holding a `discogs` alias, another row's alias, or an
  alias whose source is not `catalog` (guard reasons `shared_native_id` and
  `non_catalog_alias`) — is a real item, not a load-order orphan, and is not a failure to
  retry. Dependents no longer guard: since catalog-api main `e061f7d`, `artifacts` and
  `owned_copies` rows are re-pointed to the survivor and ledgered in `catalog_item_moves`
  (reversible); `user_collections` and `user_wantlists` hold an alias-derived `gm_item_id`
  cache that is recomputed, not moved or ledgered; `observations` are untouched, since they
  reference copies and artifacts. The census reports a dependent item under `will_move`, not
  `guarded`.

## Monthly embedding refresh

[ADR 0013](https://github.com/groovemap-music/design/blob/main/docs/adr/0013-pgvector-catalog-embeddings.md)
adopts pgvector and FastRP graph embeddings for similar-artist retrieval. `analytics-engine`
ships the pipeline that computes them, `analytics-engine-embeddings`
(`insights/embedding_pipeline.py`), as a one-shot entry point rather than a scheduled loop:
"the deployment layer" invokes it once a month, after that month's dump has loaded — this
section is that trigger. No cron or `CronJob` convention exists in this repository; an operator
(or a CI step standing in for one) runs it by hand, the same shape as
[Post-import identity maintenance](#post-import-identity-maintenance) above. Production
scheduling is the homelab's concern, not this repository's.

### Prerequisite: pgvector

The pipeline reads and writes through `public.artist_embeddings` and authenticates as a member
of database-schema's `embedding_pipeline` role — both exist only once `database-schema`'s
initializer runs against a PostgreSQL instance with the `vector` extension installed. This
stack's `postgres` service runs `ghcr.io/groovemap-music/postgres` (`POSTGRES_IMAGE`): the official
`postgres:18-alpine` plus pgvector, published from
[groovemap-music/postgres](https://github.com/groovemap-music/postgres) (gm-deployment-p9ql).
schema-init connects as this stack's superuser, so it creates `vector`, `artist_embeddings` and
`embedding_pipeline` on its next run. A shared server needs the same image, or an equivalent with
pgvector, and an administrator who creates `vector`
([homelab.md](homelab.md#what-groovemap-needs-from-each-server)); until then the wiring below
cannot complete a real run there — `just embeddings-run` fails at authentication or "relation
does not exist", never silently succeeds.

### Provisioning the pipeline's login (once per stack)

ADR 0013's 2026-09-24 amendment: `database-schema` defines `embedding_pipeline` as a `NOLOGIN`
group role — `SELECT` on every relation in the `graph` schema, `SELECT, INSERT, UPDATE, DELETE`
on `public.artist_embeddings` alone, nothing else — and the LOGIN role that is a *member* of it
is "provisioned where credentials live": `deployment` (this repository) for development and CI,
the homelab for the shared production instance.

1. `just secrets-bootstrap` (production) or copying `.env.example` to `.env` (development)
   creates the `EMBEDDING_PIPELINE_POSTGRES_USERNAME`/`_PASSWORD` credential material, the same
   `_FILE` secret convention as every other login in this stack.
2. `just embeddings-provision-login` creates that LOGIN role, if it does not already exist, and
   grants it membership in `embedding_pipeline`. It is idempotent — safe to re-run after every
   dump load — and it is deliberately a no-op (exit 0, with a logged reason) until
   `embedding_pipeline` itself exists, so it never fails `just check`/CI over the pgvector
   dependency above.

### Running the job

Once the prerequisites above are met, after a month's dump has loaded and its consumer queues
have drained (the same RabbitMQ/Queue Trends signal
[Post-import identity maintenance](#post-import-identity-maintenance) uses):

```bash
SOURCE_DUMP_ID=discogs-2026-09 SOURCE_DUMP_DATE=2026-09-01 just embeddings-run
```

`SOURCE_DUMP_ID`/`SOURCE_DUMP_DATE` have no default — the invoker supplies them, since it is the
one that knows which dump just landed, and they become `artist_embeddings.source_dump_id`/
`source_dump_date` on every row the run writes. The job is idempotent per stored
`model_version` (which composes the algorithm version, an edge-set version, and the dump id):
re-running for a dump already recorded under this algorithm is a no-op.

`docker-compose.yml`'s `embeddings` service runs `analytics-engine-embeddings` against the same
released image the always-on `insights` service uses (`ANALYTICS_ENGINE_IMAGE`), with its
entrypoint overridden and no dependency on `api`/`valkey`/`neo4j` — the pipeline talks to
PostgreSQL only. It sits behind Compose's `jobs` profile so `docker compose up` never starts it.
`just embeddings-run` (`scripts/run-embeddings.sh`) runs it as `docker compose --env-file .env
[-f ...] --profile jobs run --rm --no-deps --name groovemap-embeddings-run embeddings`, so the
one-shot gets the same Compose files as the stack it targets:

- With `COMPOSE_FILE` set (in `.env` or the shell), no `-f` is passed and Compose reads the list.
  `just deploy` writes that list whenever an overlay is in use, so on an external-store host
  (lux) the job runs with the production overlay, the external PostgreSQL overlay and the
  override, and gets the `embedding_pipeline` `_FILE` secrets and the `postgres_ca` secret.
- Otherwise it is a dedicated production stack: `docker-compose.yml` +
  `docker-compose.prod.yml`, plus `docker-compose.override.yml` when the host has one, the same
  pair `just deploy` runs.
- `RUN_EMBEDDINGS_STACK=dev` (used only when `COMPOSE_FILE` is unset) runs the development
  stack instead: the base file alone, plus the override when present.

A bare `docker compose` run of the base file is never the production job: it drops the prod
overlay's `_FILE` secrets. The script is covered by `tests/deploy/test_run_embeddings.py`, which
renders the embeddings service rather than running it.

### The post-load operator index step

`embedding_pipeline` holds no DDL privilege and no ownership of `public.artist_embeddings` — it
never builds or rebuilds an index itself. Instead it logs the exact statement an operator (a
human, or an automation step on a different, more privileged credential) runs after the load's
transaction commits:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS <index_name>
ON public.artist_embeddings USING hnsw (embedding halfvec_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE model_version = '<model_version>';
```

`<index_name>` and `<model_version>` come from the job's own log line (`_index_name` /
`stored_model_version` in `insights/embedding_pipeline.py`) — copy them verbatim rather than
re-deriving them; `database-schema`'s own `build_artist_embeddings_version_index` computes the
identical statement byte-for-byte and is the function an operator with programmatic access
should prefer over raw SQL. Run it as the stack superuser, never the `embedding_pipeline` login:

Run it detached, not in the foreground. A `CREATE INDEX CONCURRENTLY` build on a ~9M-row model
version runs for over an hour, and an interrupted one (dropped SSH session, killed client) leaves
an `INVALID` index behind that must be dropped before the build can be retried. `nohup` keeps the
psql client alive after the terminal closes, and the log records the outcome:

```bash
nohup docker exec groovemap-postgres psql -U groovemap -d groovemap -v ON_ERROR_STOP=1 \
  -c "SET maintenance_work_mem = '10GB'" \
  -c "SET max_parallel_maintenance_workers = 0" \
  -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS <index_name> ON public.artist_embeddings USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE model_version = '<model_version>'" \
  > hnsw-<index_name>.log 2>&1 &
```

`SET max_parallel_maintenance_workers = 0` keeps the build serial; the "Memory and `/dev/shm`"
paragraph below says when parallel workers are safe. The session-level
`SET`s end with the connection, so there is no `RESET` to run.

When the job finishes, confirm the index is valid. `indisvalid` must be `t`:

```bash
docker exec groovemap-postgres psql -U groovemap -d groovemap -tAc \
  "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = '<index_name>'"
```

If it returns `f`, or the log shows an error, drop the broken index with
`DROP INDEX CONCURRENTLY IF EXISTS <index_name>` and rerun the build. Do not query
`catalog-api` against the new `model_version` until `indisvalid` is `t`.

Once `catalog-api` has switched its kNN retrieval to the new `model_version`, retire the
superseded one — index before rows, so the bulk `DELETE` has nothing left to maintain row by
row — via `retire_artist_embeddings_version`, or the equivalent:
`DROP INDEX CONCURRENTLY IF EXISTS <old_index_name>` then
`DELETE FROM public.artist_embeddings WHERE model_version = '<old_model_version>'`.

**Memory and `/dev/shm`.** ADR 0013's own documented `maintenance_work_mem` figure (2 GB) is a
code default, not a sizing recommendation — `gm-analytics-engine-ieu.3` measured a full-scale
build (6.87M rows, `halfvec(128)`, `m = 16`, `ef_construction = 64`) falling into a
disk-spilling slow mode at both settings tried below full speed:

| `maintenance_work_mem` | Result |
| --- | --- |
| 2 GB | Slowed to 15,000–18,000 tuples/minute past about 3.4M of 6,869,453 rows (52.3%); killed after ~1h25m. |
| 4.5 GB | Slowed past about 5.3M rows (77%). |
| 8 GB, with `max_parallel_maintenance_workers = 4` on a 6-CPU host | Full speed throughout: 552.8 s (6,869,453 rows) and 596.1 s (6,896,892 rows). |
| 8 GB, serial (`max_parallel_maintenance_workers = 0`), lux production, 9,152,168 rows | Outgrew memory at 8,847,504 tuples. Total `CREATE INDEX CONCURRENTLY` time 1 h 10 m 56 s (4,255,909 ms). Index 4,976 MB, valid. |

Recommended `maintenance_work_mem` for a model version of about 9M rows is **10GB**, and the build
is **serial** (`max_parallel_maintenance_workers = 0`) unless `/dev/shm` is at least that size.
The lux run above is the reason: at 8 GB, serial, the graph no longer fit in memory near the end
of the build. 10GB has not yet been measured on lux; treat it as the next setting to confirm, and
record the wall time and `indisvalid` result when it runs.

A parallel HNSW build keeps the graph in `/dev/shm`, and it needs `/dev/shm` (or Docker's
`--shm-size`) at least as large as the `maintenance_work_mem` it runs with. The stack's `postgres`
service sets `shm_size` from `POSTGRES_SHM_SIZE` (default `2g`, see
[configuration.md](configuration.md#postgresql-configuration)). Lux's postgres has 4 GB of shm, so
a 10GB build there must stay serial. Only enable parallel workers (`max_parallel_maintenance_workers`
above 0) when `POSTGRES_SHM_SIZE` is raised to at least the chosen `maintenance_work_mem`. These
figures describe this row count on a two-CPU-class host (the `gm-analytics-engine-ieu.3` bench)
and lux's production host, not a value to plan other hosts around. Re-measure on each host.

**First production run (lux, 2026-11-10).** The pipeline ran end to end against a real PostgreSQL
+ pgvector instance for the first time: analytics-engine `eb6eecf9`, deploy `c55e2f9`,
`SOURCE_DUMP_ID=discogs-2026-11`.

| Measure | Result |
| --- | --- |
| Rows written | 9,152,168 |
| Wall time | About 37.5 min |
| Peak container memory | 15.8 GiB (limit 60 GiB) |
| Host available memory, lowest | Never below 43 GiB |
| `artist_embeddings` size | 6,739 MB |

The measured container peak (15.8 GiB) is about twice the 7.6–8.5 GB estimate from
`insights/embeddings.py::estimate_peak_bytes`, so the estimate is too low for this catalog size.
The figure is container memory, not process RSS alone. Report any further run's peak RSS and wall
time to `analytics-engine` (`docs/embeddings.md`), per gm-analytics-engine-ieu.3's follow-up note.

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
5. For an `activity.events` partition, immediately run
   `SELECT activity.purge_idempotency_keys('<dropped partition upper bound>');`,
   for example `'2026-02-01 00:00:00+00'` after dropping `activity.events_y2026m01`
   (the bound is the exclusive first instant of the next month). The statement
   returns how many keys it removed. It is needed because
   `activity.event_idempotency`, the table that dedupes a retry across partitions,
   is a plain non-partitioned table: `DROP TABLE` fires no row trigger, so the keys
   the dropped month's events held stay behind. The function deletes only keys with
   `occurred_at` before the bound whose event is no longer stored, so it can never
   release a key that still dedupes. Until it runs, a retry under a dropped
   event's key is stored afresh, so the dedup window is the retention window.
   Dropping an `activity.impressions` partition needs no purge: impressions hold
   no idempotency key.
6. Record the dropped partition, its date range, its row count if known, the
   number of keys purged, and the approval in the maintenance record.

This procedure is independent of the per-user erasure path (see
`just smoke-erasure` below): erasure hard-deletes a single subject's rows under
a session-local trigger bypass regardless of retention status, while a
partition drop removes a whole month for every subject once retention allows
it.
Erasure also releases the subject's idempotency keys without any step here:
`database-schema` deletes a key when the event holding it is deleted, and removes
the subject's keys that name no stored event when the pseudonym link goes
(`just smoke-erasure` asserts `activity.event_idempotency` holds no rows for the
erased subject). The key table and the purge function arrive with `database-schema` `v0.5.0`
(the pinned release; earlier releases lack them).

## Update an infrastructure image

Neo4j, RabbitMQ, and Valkey images are declared directly in `docker-compose.yml`
with a readable tag and immutable digest. PostgreSQL is the exception: `postgres` runs
`ghcr.io/groovemap-music/postgres` (`POSTGRES_IMAGE`), the official `postgres:18-alpine`
plus pgvector. Its base digest is bumped in
[groovemap-music/postgres](https://github.com/groovemap-music/postgres), which publishes a
new release; deployment then promotes that release like any service release
([Promote a service release](#promote-a-service-release)). That repository's CI runs its
`just collation-proof` on every change: a data directory built under the pinned base must read
unchanged. Keep it on Alpine (musl), because a glibc image reorders every `en_US.UTF-8` text
index. An update should include:

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
needs nothing. Valkey upgrades should be validated against a copy of `valkey_data`.

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

## Grafana Viewer token for the console

The operations console shows firing Grafana alerts in its attention bar only when it
holds a read token. It is optional: with no token the console shows the problems it
derives from its live feed and says nothing about Grafana; its deep links into Grafana
and its Grafana health row need no token. The console reads the token server-side from
a file, so it never reaches the browser.

Create the token on the Grafana that evaluates GrooveMap's alert rules: this stack's own
Grafana, or the shared one when `GROOVEMAP_MONITORING=shared` (below).

1. In that Grafana, open Administration > Users and access > Service accounts and add a
   service account (for example `operations-console`) with the **Viewer** role.
2. Add a token to it. Choose an expiry you will remember to rotate; Grafana shows the
   token once.
3. Write it to `secrets/grafana_viewer_token.txt` without a trailing newline or shell
   history (`pbpaste > secrets/grafana_viewer_token.txt` or an editor), mode 600.
   `just secrets-bootstrap` creates this file empty when it is missing and never
   overwrites it.
4. Set `GRAFANA_URL` in the production environment to the Grafana address operators'
   browsers can reach (the console builds its deep links on it). `TRACES_URL` defaults to
   it. The console fetches alerts from the server-side `GRAFANA_API_URL`: the in-network
   `http://grafana:3000` for this stack's own Grafana, which compose already sets, or
   `SHARED_GRAFANA_URL` for a shared one.
5. Restart the `dashboard` service and open the console: `GET /api/alerts` reports
   `configured: true, available: true`.

An empty (or missing) token file reports `configured: false` and the bar says nothing
about Grafana; that is the expected state until step 3. A token file that exists but
cannot be read or decoded, or a token Grafana rejects or cannot be reached with, reports
`configured: true, available: false` with a note. To rotate,
create a new token, replace the file, restart `dashboard`, then delete the old token in
Grafana. The development stack mounts no token, so alerts stay off there.

### On this stack's own Grafana

Sign in to it with the `admin` user and the password in `secrets/grafana_admin_password.txt`
to create the service account.

### On a shared Grafana

With shared monitoring this stack runs no Grafana and has no Grafana login of its own, so
nothing here holds a password for it. Ask whoever administers the shared Grafana to create the
**Viewer** service account and its token (steps 1 and 2), in the organization that holds
GrooveMap's imported alert rules, and hand you the token through your secret store; then
continue at step 3. The file is the same `secrets/grafana_viewer_token.txt`, and the console
reaches the shared Grafana at `SHARED_GRAFANA_URL` (or `GRAFANA_URL` when that is unset).

A Viewer sees every firing alert of its organization, and the console lists all of them. Import
GrooveMap into an organization of its own on the shared Grafana
([Shared Grafana](observability.md#shared-grafana)) so the attention bar shows only GrooveMap's
alerts; a Viewer of an organization other stacks share would show theirs too.

## Console sign-in over HTTPS

The console's session cookie is Secure by default. The dev stack opts out
(`SESSION_COOKIE_SECURE=false` on `dashboard` in `docker-compose.yml`); the production overlay
must not, so the production console has to be reached over HTTPS. Over plain http the cookie is
dropped and sign-in silently returns to the login form. Check how operators reach the console
before any console image bump, and see
[Console session cookie and HTTPS](configuration.md#console-session-cookie-and-https) for what the
TLS endpoint must forward. After upgrading from a console that kept its bearer in
`localStorage`, every operator signs in once; rolling back to that image also needs a new
sign-in.

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

### Shared PostgreSQL checklist

Reusing an existing PostgreSQL does not add GrooveMap's database to its backup allowlist. Check,
read-only, and record the answers next to the backup evidence above:

1. Is GrooveMap's database (`EXTERNAL_POSTGRES_DATABASE`, default `groovemap`) in the backup's
   protected-database list?
2. Which schemas does the backup exclude? Keep the operator's policy. An excluded
   `musicbrainz` schema is reconstructible, but a restore then has no MusicBrainz data until the
   import runs again: plan for a long re-import (days of loading and about 100 GB of disk, an
   estimate; MusicBrainz sizing is unmeasured, `gm-deployment-vc5s`).
3. Does restore verification include the GrooveMap database, and when was it last restored to a
   scratch target?
4. Dedicated stores (Neo4j, RabbitMQ, Valkey, and PostgreSQL when not external): is each volume
   backed up? Neo4j, RabbitMQ and Valkey contents are rebuilt by re-import. **User accounts,
   collections, the identity ledger and the admin audit log are not reconstructible from dumps**,
   so the PostgreSQL backup is the only copy.

### Recovery bundle

The bootstrap-generated secrets (JWT key, encryption master key, PostgreSQL, Neo4j, RabbitMQ and
Valkey passwords) cannot be re-created: a new value leaves the restored data unreadable.
`just recovery-manifest` lists what to capture without printing any value: `.env`,
`docker-compose.override.yml`, each `secrets/` file by name, mode, owner and SHA-256 fingerprint,
the deployment commit and its dirty state, the promoted image digests, and the external stores
that need their own backups. It needs python3 3.10 or newer (uv is optional) and writes nothing.
Put those files in one bundle and encrypt it (SOPS with age) before it leaves the host; keep it
separate from the database backups and out of this repository.

Rules: never reset an existing secret (`just secrets-bootstrap` skips existing files, and
`just deploy` stops with exit 3 if one changed); never expose a value; never purge persistent keys
or volumes (`docker compose down -v`, `docker volume rm`, deleting `secrets/`) after a failed
staging. To recover, restore `secrets/` (directory 0700, files 0600), `.env` and the override from
the decrypted bundle before any container starts and before `just secrets-bootstrap`, check out the
pinned commit, restore the data, then run `just deploy --no-wait`.

With an external PostgreSQL or Valkey (`GROOVEMAP_POSTGRES=external`, `GROOVEMAP_VALKEY=external`),
that server's backups, upgrades, restarts and monitoring belong to its operator, and the
PostgreSQL row above is their evidence to record. This stack neither backs it up nor measures
its disk. See [Existing PostgreSQL and Valkey](homelab.md#existing-postgresql-and-valkey).

## Catalog reset

A full reset of imported data with one fresh ingest is a separate, operator-approved procedure:
see [Catalog reset and fresh ingest](catalog-reset.md).

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
Neo4j, and Valkey before tearing the stack and its volumes down. It needs an
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
