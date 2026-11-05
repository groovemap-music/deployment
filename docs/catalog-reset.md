# Catalog reset and fresh ingest

This is the runbook for the one-off reset of GrooveMap's imported data on lux, followed by one
fresh ingest. The operator decided on it on 2026-11-05 for three reasons: it is the only proof of
the loader's leak fix against a real backlog for a month, it removes the need for a one-off Person
merge, and it gives a clean baseline on final code.

**Nothing here has been run on lux.** The real run needs the operator's explicit go-ahead, and the
commands below need a person on lux: no agent deletes on a host. It has been rehearsed on a
disposable stack ([Rehearsal](#rehearsal)).

Run everything as `datum` in `/opt/groovemap`, **inside `tmux`**: a dropped session must not take a
stage with it. Lux sets `COMPOSE_FILE` in `.env`, so write no `-f` list. Each stage below is `just catalog-reset <stage>` (`scripts/catalog_reset.py`). Without
`--apply` a stage prints what it would do and changes nothing, so read that output first.

## What happens to each store

Lux's PostgreSQL server is shared. Only the database `groovemap` is touched. The tool cannot be
pointed at another database: each SQL statement is a literal in the script, no database name comes
from an argument or the environment, and `psql` refuses to connect to anything but `postgres` and
`groovemap`. Each forward stage also compares every other database, every role and the server's
start time with the capture, before it acts where that matters, and stops on a difference. That
comparison detects a change, it cannot undo one. A neighbour's new database is only noted, and a
neighbour's own legitimate change or a server restart stops the run until you have looked and pass
`--accept-shared-change`. A rollback reports a difference and never stops on one.
`tests/deploy/test_catalog_reset.py` fails if a statement that changes the server is not on this page.

| Store | What is dropped | Exact target and how | What is kept |
| --- | --- | --- | --- |
| PostgreSQL | All of the old catalog, at `finish` | Database `groovemap`, renamed then replaced ([statements](#every-destructive-command)) | The rows in [What is kept](#what-is-kept). Every other database and every role |
| Neo4j | The whole graph | Volume `groovemap_neo4j_data`: copied to `groovemap_neo4j_data_pre_reset`, then removed | `groovemap_neo4j_plugins`, `groovemap_neo4j_import`, `secrets/neo4j_password.txt` |
| RabbitMQ | Every message, dead letters included | Each queue matching `^groovemap-(discogs\|musicbrainz)-[a-z-]+(\.dlq)?$`, purged by name | Queues, exchanges, bindings, users, the DLQ policy. The volume stays |
| Valkey | Every key but session revocations | `UNLINK` by key name in the dedicated instance. Never `FLUSHALL` or `FLUSHDB` | Keys under `revoked:jti:`, `password_changed:` and `legacy-keys:` |
| Extraction state | Nothing is deleted | The forced run replaces `.extraction_status_<version>.json` in `groovemap_discogs_data` | The dumps and `.discogs_metadata.json`, which is what prevents a re-download |

Not touched: `/opt/groovemap`, `.env`, the override, `secrets/`, the `groovemap-ops` project and its
volumes, every backup, the shared monitoring and `groovemap_musicbrainz_data`. The Compose volumes
are named by exact name only. Never select by a `groovemap_` prefix:
`groovemap_grafana_provision_state` belongs to `groovemap-ops`.

Valkey is cleared by keep-list on purpose. Its caches hold catalog ids this reset re-mints, and
anything added to them later is removed by default, while a revocation that was dropped could make
a signed-out token valid again until it expires.

### What is kept

Measured read-only on lux on 2026-11-05:

| Table | Rows on lux | At the reset |
| --- | --- | --- |
| `public.users` | 2: the admin, and the inactive system actor `6d3c1f0a-5e1b-4c7a-9a4f-1d0e1d3a7a11` | Both restored, with their ids and password hashes |
| `public.admin_audit_log` | 2: one `admin.login`, one `identity.reattach.auto` for `discogs:20261101` | The login restored. **The `identity.reattach.auto*` rows are not restored** |

`identity.reattach.auto` with target `discogs:<version>` is catalog-api's durable "this extraction
is handled" marker. The re-ingest is the same version, so a restored marker would stop the watcher
re-attaching and projecting `gm_id`, silently, with a complete-looking catalog. The row survives in
the retained database and in the dump.

`preflight` stops if a row exists in any table with a foreign key to `public.users`, read from the
catalog so a new table cannot be missed, or in `app_config`, `artifacts`,
`catalog_item_supersessions`, `identity_link_events`, `activity.erasures`,
`activity.event_idempotency`, `activity.events` or `activity.impressions`. The tool does not carry
those across, and their rows can reference catalog ids that the reset re-mints or hold what a
re-import cannot rebuild. All are empty on lux today. If one is not on the day, stop and extend this
runbook. The two exceptions are `admin_audit_log`, restored above, and `extraction_history`, the
history of admin-triggered runs, which ends with the old catalog and stays in the retained database.

### Every destructive command

These are the only destructive commands in the procedure. On the shared server, in the `postgres`
maintenance database, one session:

```sql
ALTER DATABASE groovemap RENAME TO groovemap_pre_reset;
ALTER DATABASE groovemap_pre_reset WITH ALLOW_CONNECTIONS false;
CREATE DATABASE groovemap OWNER groovemap TEMPLATE template0 ENCODING 'UTF8' LOCALE 'en_US.UTF-8' CONNECTION LIMIT 60;
REVOKE ALL ON DATABASE groovemap FROM PUBLIC;
```

Then in the new `groovemap`, matching what homelab provisions:

```sql
CREATE EXTENSION pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO groovemap;
```

Neo4j, RabbitMQ and Valkey, where `<queue>` is each matching queue and the volume name is read from
Compose's render and printed by `preflight`:

```bash
docker compose rm --force <writers>   # each group that `stop` stops, after it stops; never a store
docker compose rm --force --stop neo4j
docker volume rm groovemap_neo4j_data
docker compose exec -T rabbitmq rabbitmqctl -q purge_queue <queue>
# In the valkey container, by exact key name, in batches of 500: UNLINK <key> ...
```

Only at `finish`, after the proof, which is the point of no return:

```sql
DROP DATABASE IF EXISTS groovemap_pre_reset;
DROP DATABASE IF EXISTS groovemap_reset_failed;
```

```bash
docker volume rm groovemap_neo4j_data_pre_reset
```

And in a rollback, in the same `postgres` session:

```sql
ALTER DATABASE groovemap RENAME TO groovemap_reset_failed;
ALTER DATABASE groovemap_reset_failed WITH ALLOW_CONNECTIONS false;
ALTER DATABASE groovemap_pre_reset WITH ALLOW_CONNECTIONS true;
ALTER DATABASE groovemap_pre_reset RENAME TO groovemap;
```

There is no `WITH (FORCE)` and no `pg_terminate_backend`: a session left on `groovemap` stops the
rename, and that is the guard. Renaming first means the old data stays whole, and rolling PostgreSQL
back takes seconds (the graph takes as long as its copy). `pg-backup` and `groovemap-ops` name `groovemap` explicitly, so they do not touch the
renamed copy, and no connection can reach it.

## Before the day

Each of these must hold, or do not start:

- **The operator's go-ahead for this window**, recorded on the bead.
- **The next releases are promoted here, with a merged commit:** `database-schema` `v0.7.0`, the new
  `discogs-sql-loader` and `discogs-graph-enricher`, `catalog-api` and `analytics-engine`. Three
  things depend on that set:
  - The leak fix: `discogs-sql-loader` `v0.4.4` or later.
  - **Person names:** the loader and enricher must both strip names through
    `common.person_names.strip_person_name` (`gm-discogs-sql-loader-kj93` and
    `gm-discogs-graph-enricher-wem5`). Both write names verbatim today, so without those two a
    reset re-creates every padded Person and the merge it was meant to avoid is still owed.
    `graph.bootstrap_fill` is not part of this: nothing in the ingest calls it.
  - **Rarity:** `catalog-api` `v0.6.0` and `analytics-engine` `v0.5.0` still use the single-response
    call that failed on lux on 2026-11-04 with `ReadTimeout` after 30 minutes, which is why
    `insights.release_rarity` is empty there. Confirm that the promoted `analytics-engine` has
    `rarity-scores/page` and that the promoted `catalog-api` serves it.
- **Lux's tableinator cap is in configuration** (`gm-deployment-3qs7`), so the leak proof runs under
  the same 30 GiB limit that a recreate keeps. It shares the host with the neighbours' PostgreSQL.
- **The rehearsal has passed on exactly this commit and these images** ([Rehearsal](#rehearsal)):
  `just rehearse-catalog-reset --evidence <directory>` from a clean checkout of the commit being
  deployed. Above all, that proves the kept rows load into the `v0.7.0` schema. Its `REPORT.json`
  records the commit and every image, and `preflight` refuses without a report that matches, so copy
  it to lux.
- **Check whether the commit moves a store's digest.** `neo4j`, `rabbitmq` and `valkey` are pinned in
  the Compose files, not `.env`, so a rollback's pins do not move them back:

  ```bash
  git diff <lux commit> <merge commit> -- docker-compose.yml docker-compose.prod.yml | grep -E '^[-+] +image: (neo4j|rabbitmq|valkey)'
  ```

  Not known yet, because the promoted commit does not exist. `preflight` warns as well. A moved Neo4j
  is harmless forward, on a fresh volume. It matters on rollback, where the new image opens the old
  store, so if it moved, read that release's store-format notes and record on the bead before the
  go-ahead whether rollback can run on it.
- **Agents and read-only tools stay off from step 4 to step 6.** `gm-lux-sql` opens a session on
  `groovemap`, which stops the rename, so a stray audit query becomes an aborted stage. Say so in the
  channels the agents use.
- **The checkout and pins move by hand**, as in step 1 of the
  [Lux window](homelab.md#lux-upgrade-schema-init-v063-sql-loader-v044-graph-enricher-v060):
  `git fetch origin && git checkout --detach <merge commit>`, then `python3 scripts/env-init.py`.
  Do not run the homelab playbook or `just deploy`: both end in a whole-stack `up`, which would start
  `insights` and the extractor in the wrong order. Advancing `groovemap_ref` is a separate homelab
  change. `preflight` refuses a render that is not on the reviewed digests, below the release floor
  or without the cap.
- **Silence the expected-container alerts** for the stack on the homelab side, and expect
  `GrooveMapBackupFailed` (critical) during the load: the nightly dump of a database under heavy
  writes can miss its 5-second lock wait. It is not a reason to stop, but do confirm that the
  failure is that lock wait.
- **Start after the night's dumps finish, some time after 10:00 UTC.** No stage from `stop` to
  `start` starts in 07:30 to 10:00 UTC (08:00 to 10:00 is the nightly dump window, and the half hour
  before it keeps a stage from running into it). `groovemap-ops` starts at 01:00 Pacific and may run
  2 hours, so on standard time it can pass 10:00 UTC: `preflight` then reports a running dump, so
  wait for it. The load itself crosses at least two dump windows, and the operator accepts that
  (step 13).

### Release order on a fresh database

`database-schema` at `v0.7.0` needs the new `catalog-api`, because from that schema on the old
release's relkind filter is wrong, so on a live database `catalog-api` must move first or together
with it. Here the two move together, and no old `catalog-api` ever reaches the new schema:

1. `env-init` moves every pin in `.env`, and starts nothing. The old images keep running.
2. Step 4 stops the old `api` before anything changes in PostgreSQL.
3. Step 10 runs `schema-init` `v0.7.0` on the empty database, with no client connected.
4. Step 11 starts the new `catalog-api` and the other clients on that schema.

So do not start or recreate `api` between steps 4 and 11, and do not start a schema client with
`--no-deps` in steps 10 and 11: `schema-init` reaches the database through `depends_on`, which also
holds every client behind it. The tool does this. A `--no-deps` start is correct in two places only:
`after-load`, on a schema that has already run, and [Rollback](#rollback), where the old database
keeps the schema it has.

## Procedure

### 1. Recovery bundle

```bash
just recovery-manifest
```

Capture the listed files in the encrypted bundle
([Recovery bundle](maintenance.md#recovery-bundle)). The secrets do not change in this procedure,
and must not: `encryption_master_key` and `jwt_secret_key` are what make the kept accounts usable.

### 2. Backups

The retained database and volume are the first line of rollback. The dump is the second, and it is
the only copy that survives `finish`.

1. Confirm last night's dumps succeeded (`homelab_groovemap_backup_ok` and `pg-backup`'s health).
2. Copy the newest `groovemap` dump out of the rotation. `groovemap-ops` prunes `last/` after 7 days
   and `pg-backup` keeps 3 days, and the load outlasts neither by much, while every night of it
   replaces the newest dump with a partial one. The paths below are from homelab's configuration;
   confirm them on the host:

   ```bash
   B=/mnt/data/archive/postgres-dumps/groovemap
   mkdir -p "$B/pre-reset-$(date -u +%Y%m%d)"
   cp -p "$(readlink -f "$B/last/groovemap-latest.sql.gz")" "$B/pre-reset-$(date -u +%Y%m%d)/"
   gzip -t "$B/pre-reset-$(date -u +%Y%m%d)/"*.sql.gz
   ```

3. Record the baseline on the bead: `just discogs-import-status --prod -f docker-compose.override.yml
   --json` and `just status`. They print no credentials. Do not paste a Compose render anywhere: it
   holds every secret in clear text.

### 3. Pre-flight

```bash
umask 077; W="$HOME/gm-catalog-reset-$(date -u +%Y%m%d)"; mkdir -m 0700 "$W"
just catalog-reset preflight --state-dir "$W" --rehearsal-report <path to REPORT.json>
```

Write `$W` down: the run takes days, and later stages and rollback need it in a new shell. `stop`
refuses without a pre-flight from the last 6 hours in that directory, and the pre-flight also records
what each service is running, which is rollback's record of the old pins.


Read-only, with the stack still up. It checks the window, the admin session (`docker exec postgres
psql -U datum`), that the running stack really is connected to that server, that `groovemap` is owned
by `groovemap` and no retained copy exists, no dump is running, the reviewed digests, the
[release floor](#before-the-day), the tableinator cap, a rehearsal report for this commit and these
images, the [empty tables](#what-is-kept), the
four dump files with their checksums, and at least 400 GB free. It prints every target and the other
databases by name. Stop on anything unexpected.

### 4. Stop the writers

```bash
just catalog-reset stop --state-dir "$W" --apply
```

In order: `extractor-discogs` first, so nothing new is published; then `tableinator`, `graphinator`
and any MusicBrainz consumers; then `insights`; then `api`, `dashboard` and `explore`, the last
PostgreSQL clients. **Each is removed as well as stopped.** Their restart policy is `always`, which
brings a merely stopped container back when the Docker daemon restarts, and here that would be an
old `api` on a new database. The consumers' logs and restart and OOM state go to `$W` first, because
removing them loses that evidence. It then requires no session on `groovemap`. RabbitMQ, Valkey and Neo4j
stay up for the next stages. Their data is on volumes, which this does not touch.

### 5. Capture what is kept

```bash
just catalog-reset capture --state-dir "$W" --apply
```

After the stop, so nothing can change between the copy and the reset. It refuses a second capture in
the same directory and any capture once the reset has started.

`$W` holds password hashes and must stay outside the checkout at 0700; the tool refuses otherwise.
It also holds the extractor's markers for a rollback, the catalog's row estimates for the proof, the
fingerprint of the other databases, and a record of each completed stage, which is how rollback knows
what it can trust. Do not edit those records.

### 6. PostgreSQL

```bash
just catalog-reset reset-postgres --state-dir "$W"
just catalog-reset reset-postgres --state-dir "$W" --apply
```

Takes seconds on any database size. It stops if a session is still on `groovemap` or a retained
copy exists. If it stops part way, see [Rollback](#rollback).

### 7. Neo4j

```bash
just catalog-reset reset-neo4j --state-dir "$W" --apply
```

Refuses until `reset-postgres` has completed. Stops Neo4j, copies the volume, compares file count and
bytes, records the copy, and only then removes the container and the volume. Safe to run again: an
interrupted copy starts over, a recorded copy is not repeated. A fresh volume takes its password from `secrets/neo4j_password.txt` at
first boot. "Verified" here and below means the same file count and the same total bytes. It is not a
content hash.

### 8. RabbitMQ

```bash
just catalog-reset reset-rabbitmq --state-dir "$W" --apply
```

Purges rather than removing the volume. **The queues and bindings must survive:** the extractor
publishes mandatory messages, so a publish with no queue bound fails the run with `312 NO_ROUTE`
(found in rehearsal). It stops if a consumer is still attached.

### 9. Valkey

```bash
just catalog-reset reset-valkey --state-dir "$W" --apply
```

### 10. Schema and kept rows

```bash
just catalog-reset bootstrap --state-dir "$W" --apply
```

Starts Neo4j on its fresh volume, runs `schema-init` from the new release on the empty database
through its `depends_on` (so `store-check-postgres` goes first), then restores the kept rows in one transaction with the capture's own
column list. A release that drops a captured column fails the restore, so nothing is lost silently.
It refuses to start until all four reset stages have completed. It then verifies the row counts,
that no handled marker arrived, that nine catalog tables, Neo4j and the catalog queues are empty, and
that `.discogs_metadata.json` is unchanged, and compares the neighbours again.

### 11. Start the stack, without `insights`

```bash
just catalog-reset start --state-dir "$W" --apply
```

`tableinator`, `graphinator`, `api`, `dashboard` and `explore` in one `up` without `--no-deps`, so
`depends_on` holds them behind `schema-init`, which runs again and finds nothing to do. Then
`extractor-discogs`, once the consumers' queues are bound. **`insights` stays stopped** until the gate exits 0: it starts
every computation 30 seconds after it starts, and its rarity transaction blocks vacuum for the
whole database. The extractor logs `already processed, skipping`, which is expected, and waits.

**The ledger poll is off for the load.** While writes continue, it would rebuild the Identity Ledger
on a partial catalog every 6 hours, in one transaction (about 6 to 7 minutes each at full size), and
a transaction like that holds back vacuum across the database during the heaviest writes. `start`
passes `IDENTITY_LEDGER_REFRESH_ENABLED=false` for its `up` only, so `.env` does not change, and it
refuses to continue if the poll is on. The watcher's own refresh after the re-attachment builds the
ledger at the end. Until then the works list and density answer `503`, which nobody can use during
a load anyway.

If its log instead shows a new version, a newer dump has been published and the extractor is already
downloading and processing it without a trigger. That is a valid run: skip step 12.

### 12. Force the extractor, without a re-download

Sign in to the console and press **Discogs**, or call the admin endpoint it uses:
`POST /api/admin/extractions/trigger` with `{"source":"discogs"}`. Expect `202`, and exactly once: a
`409` means a run is active, so stop and look.

The forced run replaces the state marker and reprocesses every file. It still fetches the listing
and the published `CHECKSUM` file from `data.discogs.com`, so the host needs outbound HTTPS, but it
hashes each local file and downloads nothing that matches `.discogs_metadata.json` (rehearsal: 4
data-file requests before and 4 after).

Do not set `FORCE_REPROCESS`. It is hard-coded `false` in `docker-compose.yml` for a reason: a
crash-restart with it set would start the whole run again, where the trigger resumes from the marker.
Do not delete the marker or `.discogs_metadata.json` to get the same effect.

### 13. Watch

```bash
just catalog-reset status --interval 300 --duration 3600
```

Read-only. Each sample prints queue depth, drain rate, consumers, dead letters, sessions on
`groovemap`, its size, free disk (`free=`), and `docker stats` for `tableinator`, `graphinator` and Neo4j. Grafana's
ingestion and consumers dashboards show the same series over time.

| Watch | Healthy | Stop and report when |
| --- | --- | --- |
| `tableinator` memory | Levels off well under the cap and falls between files | It climbs steadily past 20 GiB, or `docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' groovemap-tableinator` shows a kill or a restart |
| Task count | `pids` stays flat; `unacked` stays at the prefetch | `pids` or `unacked` climbs while throughput is steady |
| Queue rate | `drain` above zero while `queued` is above zero | `drain` is 0 for 30 minutes with `queued` above zero and consumers attached |
| DLQs | `dlq=0` | It grows at all. Each DLQ is capped at 50,000 messages or 512 MiB with drop-head, so a long run can lose dead letters |
| `insights` | No container | One appears. Remove it with `docker compose rm --force --stop insights` |
| Disk | `free=` at least 200 GB | Below 200 GB |
| Shared server | Neighbours' latency unchanged | Any neighbour reports trouble: stop the loader, not the neighbour |

**Task count:** the leak was retained batch handlers, and the loader exports no asyncio task count.
`pids` is threads, so it only catches a thread leak; `unacked` is deliveries held by handlers, and
memory slope remains the direct signal. Exporting a real count belongs to the loader.

The nightly dumps run in this time, against a partly loaded database, and the operator accepts that
by starting: do not stop them, and expect them to take longer, add I/O, and possibly raise
`GrooveMapBackupFailed`. Do not run a schema
change, `schema-init` or `just deploy` while the load runs.

If the extractor crashes, `restart: always` brings it back and it resumes from its marker. Do not
press the trigger again.

### 14. The gate, then the rarity run

```bash
just discogs-import-status --prod -f docker-compose.override.yml --wait --interval 300
```

Exit 0 is complete, exit 3 in progress, exit 2 cannot determine and not a pass. Keep MusicBrainz off
until exit 0. The watcher then re-attaches and projects `gm_id` by itself within its 5-minute poll.
Look for the new `identity.reattach.auto` row and for `Post-import maintenance complete` in the
`graphinator` logs. That pass is detached from the gate and writes to the graph, so wait for it.

The watcher fires on the SQL latch alone, and its projection only reaches nodes already in the graph.
On lux the graph finished first, but the leak fix may change that order. If `gm_id` is missing on
`Release` nodes after the graph is complete (see the proof), run
`docker exec groovemap-api catalog-identity-projection` once.

Only then:

```bash
just catalog-reset after-load --state-dir "$W" --apply
```

It refuses until PostgreSQL's load, the graph's completion and the watcher's row are all there. It
recreates `api` so the ledger poll is back on, confirms that, and starts `insights`.

It runs every computation in turn, 30 seconds after starting, rarity near the end. Expect 1 to 1.5
hours for rarity alone. Confirm with `💾 Release rarity scores stored` in its log and:

```sql
SELECT status, started_at, completed_at, error_message FROM insights.computation_log
WHERE insight_type = 'release_rarity' ORDER BY started_at DESC LIMIT 1;
```

Any other computation that failed on lux before (`label_longevity`, `style_cotags`,
`style_landmarks`) should now pass. If not, record it; those failures are not caused by the reset.

## Proof criteria

The Person checks use `strip_person_name`'s rule, not `trim`, which misses format characters. The
SQL names the rule's characters explicitly, so if the rule changes, change this:

```cypher
MATCH (p:Person) WHERE p.name =~ '(?s)^[\\p{Z}\\p{Cc}\\p{Cf}\\s].*|.*[\\p{Z}\\p{Cc}\\p{Cf}\\s]$' RETURN count(p);
```

```sql
SELECT count(*) FROM graph.person WHERE name ~ E'^[\\s\\u0001-\\u001f\\u007f-\\u00a0\\u00ad\\u0600-\\u0605\\u061c\\u06dd\\u070f\\u1680\\u180e\\u2000-\\u200f\\u2028-\\u202f\\u205f-\\u2064\\u2066-\\u206f\\u3000\\ufeff\\ufff9-\\ufffb]|[\\s\\u0001-\\u001f\\u007f-\\u00a0\\u00ad\\u0600-\\u0605\\u061c\\u06dd\\u070f\\u1680\\u180e\\u2000-\\u200f\\u2028-\\u202f\\u205f-\\u2064\\u2066-\\u206f\\u3000\\ufeff\\ufff9-\\ufffb]$';
```

All of these, recorded on the bead with before and after numbers, before `finish`:

| Criterion | How | Pass |
| --- | --- | --- |
| Load complete | `just discogs-import-status --prod -f docker-compose.override.yml` | Exit 0 |
| Accounts survive | Sign in to the console with the original password. `SELECT id, is_active, is_admin FROM public.users` | Same two ids, admin signs in, the system actor is inactive |
| Everything else rebuilt | Row counts against `catalog_rows` in `$W/capture.json`: lux has artists 10.2M, labels 2.4M, masters 2.6M, releases 19.3M, Neo4j Release 19.6M | Same dump, so the same counts within the estimates' error, and a new `catalog_items` id set |
| Identity ran | One new `identity.reattach.auto` for this version, then `MATCH (r:Release) WHERE r.gm_id IS NULL RETURN count(r)` | The row is there with `failed` 0 in `details`, and the count is 0 (lux: 0 of 19,575,942, and 16,727 `Artist` nodes without one) |
| Leak fix under a real backlog | The watch table's samples for `tableinator` | No OOM kill, restart count 0, memory level, peak recorded |
| No dead letters | `status` | `dlq=0` |
| Person merge not needed | The two queries above | 0 in both. Lux today, measured 2026-11-05: 181 in Neo4j and 294 in `graph.person` |
| Rarity | `computation_log` and `count(*)` on `insights.release_rarity` | `completed`, row count near the release count |
| Neighbours untouched | The `ok` line from each stage, and each neighbour's own health | No `SHARED SERVER CHANGED`. A `note` about a neighbour's new database is not a failure |

## Finish

**Once the operator accepts the proof, and at a quiet moment for the neighbours.** `DROP DATABASE`
forces a checkpoint and unlinks 185 GB on the shared server, so pick a time when nothing else there
is busy, and not 07:30 to 10:00 UTC.

```bash
just catalog-reset finish --state-dir "$W"
just catalog-reset finish --state-dir "$W" --apply
rm -rf "$W"
```

It refuses unless `groovemap` has a complete Discogs load and Neo4j has a complete extraction, and it
refuses after a rollback that did not finish. If it dies part way, run it again: it is safe to repeat,
and `rollback` refuses in that state, because the retained database is gone. Keep the pre-reset dump until the next
good monthly restore drill. Follow the Lux window's note on the first full-stack deploy afterwards.

## Rollback

Works at any point after `capture` and before `finish`, and can be run again if it stops part way.
Once it has started, every forward stage refuses that `$W`.
It changes nothing in the stores until it has checked the old pins and the retained graph.

1. **Put the old pins back, in `.env` only.** Replace the `*_IMAGE` lines from the backup that
   `env-init` left, and nothing else: an older `.env` may lack a value the current Compose files
   need. **Leave the checkout where it is until after `finish`:** an older commit does not have this
   tool, and you need it for `finish` and for any re-run.

   ```bash
   sed -i '/_IMAGE=/d' .env && grep '_IMAGE=' .env.<timestamp>.bak >> .env
   ```

2. Run it. It refuses if the pins are not what the stack ran at pre-flight, since a new image on the
   old schema is what the release order forbids:

   ```bash
   just catalog-reset rollback --state-dir "$W" --apply
   ```

It stops and removes the same services, sets the new database aside as `groovemap_reset_failed`,
renames the retained one back and reopens it, restores the graph, restores the extractor's markers,
purges the queues, clears Valkey again and starts Neo4j.

- **The graph is restored only from a copy that this reset recorded as verified**, and only while the
  retained volume still matches that record. With no record, as after an interrupted `reset-neo4j`,
  the live volume was never removed, so it is left alone, whatever a partial retained volume holds.
- **The markers are what make this safe.** The forced run replaced them, so an untouched extractor
  would resume that run into the old database on its next start. If a newer dump's marker appeared
  during the run, the tool warns and leaves it. Then do not start `extractor-discogs` until you have
  decided: starting it would process that newer dump into the old database.
- It stops if a session is still on `groovemap`. Autovacuum does not count.
- A change on the shared server is reported and never stops it.

Then start what was stopped, consumers first, and include `insights`. These use `--no-deps` on
purpose: the old database keeps its schema, so no `schema-init` runs for it:

```bash
docker compose up --detach --no-deps --wait tableinator graphinator
docker compose up --detach --no-deps --wait api dashboard explore insights
docker compose up --detach --no-deps --wait extractor-discogs
```

Confirm the gate exits 0 and sign in. Then `finish` removes the failed attempt and the retained
volume, and only then move the checkout back. A later reset starts from `preflight` with a new `$W`.

After `finish`, the only way back is the dump in `pre-reset-<date>/`, which holds PostgreSQL without
MusicBrainz data, and a re-import for the graph. That is a restore, not a rollback: see
[Database resilience](database-resilience.md).

## Time and disk

| Step | Estimate on lux | Basis |
| --- | --- | --- |
| Stop, capture | 2 to 5 minutes | Extrapolated; the rehearsal took 8 s, and the stop timeout is 120 s per group |
| `reset-postgres` | Seconds | Measured in rehearsal, under 1 s. Renames and an empty `CREATE DATABASE` do not depend on size |
| `reset-neo4j` | 5 to 15 minutes for 57 GB | Extrapolated: a plain copy at NVMe rates. The rehearsal's 14 s was a tiny graph |
| RabbitMQ, Valkey | Seconds to a minute | Measured in rehearsal, 9 s and under 1 s, at small scale |
| `bootstrap` | Under 5 minutes | Measured in rehearsal, 40 s under emulation: `schema-init` on an empty database does no key swaps |
| Start | About 5 minutes | Extrapolated; the rehearsal took 96 s under emulation, and Lux's Neo4j page cache is larger |
| SQL load | About 35 hours | **Measured on lux**, once: first load 2026-11-03 21:32 to 2026-11-05 08:25 UTC on loaders that predate the leak fix, with a window in it. Treat it as indicative |
| Graph load | About 15 hours, plus 11 hours of post-import maintenance | **Measured on lux**, same run: complete 2026-11-04 12:42 UTC, maintenance done 23:46 UTC |
| Identity | Minutes | **Measured on lux**: marker 2 minutes after the latch, but on a load order that split nothing |
| Rarity | 1 to 1.5 hours | Extrapolated by analytics-engine from sampled pages. Never completed on lux |
| Rollback | 10 to 20 minutes | Extrapolated: seconds for PostgreSQL plus one graph copy. The rehearsal's 48 s was a tiny graph |
| All of it | About 2 days | Extrapolated |

| Disk | Size | Basis |
| --- | --- | --- |
| Retained `groovemap_pre_reset` | 185 GB | Measured on lux |
| New `groovemap` | About 185 GB, plus rarity about 5 GB | Measured size of the old one; rarity is analytics-engine's estimate |
| Retained graph volume | 57 GB | Measured on lux |
| New graph | About 57 GB | Measured size of the old one |
| Dumps, queues at peak | Already on disk; queues about 0.3 GB | Measured: `groovemap_rabbitmq_data` was 228 MB during the first load |
| WAL | 8 GB (`max_wal_size`), shared server | Measured setting |
| Peak total | About 500 GB, against 1.2 TB free | Sum of the above. `preflight` requires 400 GB free beside what is already used |

The new database may come out smaller, since it is unbloated. That is not measured.

## Rehearsal

```bash
just rehearse-catalog-reset --evidence <directory>
```

Run 2026-11-05 on an arm64 Mac under emulation, on the pins of the time, at commit `19c4aee` with a
clean checkout. **65 of 65 checks passed, in 20 minutes.** It uses the operator's own stages.
Its `REPORT.json` records the commit, whether the checkout was clean, and each image.
The stack is the Lux file set against a shared PostgreSQL 18 with TLS, holding two neighbour databases
and a neighbour session that stays open throughout. A local server answers as `data.discogs.com`, so
the released extractor's real listing, checksum and reuse logic ran against a dump of 304 records.

| Proved | Before | After |
| --- | --- | --- |
| The accounts survive | Admin and system actor | Byte-identical, password hash included. The admin signs in with its original password |
| Everything else is rebuilt | artists 80, labels 24, masters 40, releases 160, aliases 624, Person 80, 160 `gm_id`, 304 `catalog_items` | The same counts, per Neo4j label too. A new database, and a new set of ids in both stores |
| The handled marker stays out | 1 row | The watcher ran again on the same version, and `gm_id` reached Neo4j |
| A forced run does not download | 4 data-file requests | 4. It did fetch `CHECKSUM` again |
| Stale state goes | A dead letter, catalog cache keys | Queues empty, caches gone, revocation keys still there |
| Writers cannot come back | Running containers | None remain after `stop` |
| The neighbours are untouched | Dump hash, session pid and start | The same at every check, through all three resets and both rollbacks |
| Rollback works | The old ids, marker and counts | All restored, and the extractor logs `already processed, skipping` |
| Rollback can run again | A completed rollback with its last record removed, so it re-ran the graph copy | Finished, and the same catalog |
| Rollback ignores an unverified copy | Simulated: a hand-made partial retained volume with no copy recorded | The intact graph was kept and the partial volume ignored |
| The guards stop | Rows in a table it cannot carry, a straggler session, a second capture, `finish` before the load | Each refused, and nothing changed |
| The ledger poll | On, then off for the load | Off during the load, the ledger built by the watcher anyway, on again after `after-load` |
| Rarity | No rows | `completed`, 160 rows |

It found three real problems that reading had not: the dump check matched its own query text, an
extractor that publishes before consumers bind fails with `312 NO_ROUTE`, and the graph's maintenance
pass writes nodes after the gate reports complete.

**What it does not prove.** Read these before relying on it:

- It ran on today's pins, not the promoted set, so `database-schema` `v0.7.0` (including the kept rows
  loading into it), the [release order](#release-order-on-a-fresh-database), name stripping and the
  paged rarity path are unexercised. That is why [re-running it on the promoted set](#before-the-day)
  is a precondition.
- Scale. 304 records say nothing about the leak, memory, duration or the real order of the SQL and
  graph loads. Its pre-flight skips the floor and cap checks, so those have run only against a
  scripted host, and on no real stack. The first real run of them is the day's own pre-flight.
- Real interruptions. No copy was killed part way: the partial cases above are constructed, and the
  crash paths are covered by the scripted tests.
- Lux's real override: its proxy, shared network and collector were not used.
- Its report vouches for that run's commit and images only. The one behind these numbers is not the
  release set, so `preflight` on lux refuses it, as intended.
- A real Docker daemon restart. Stopped writers are removed so that none can return, and the
  rehearsal checks that none remain, but the daemon was not restarted. `insights` and the stores were
  not tested through one, and a restart during the load would restart whatever is running.
- The nightly dumps running alongside, and a neighbour changing during the run.

## Reviewed

An independent adversarial review (2026-11-05) tried to reach another database, interrupted each
stage on paper, checked the runbook's claims against the source and lux read-only, and mutated the
guards, in two passes. **It found no way to reach another database, role or queue**, and after the
first round no remaining way to destroy the only good graph. It confirmed the fresh database matches
the old one's owner, locale, access lists and connection limit, that the kept rows are enough, and
the claims about the extractor, Valkey and `insights`.

It rejected both earlier versions. Fixed since:

- Rollback could overwrite an intact graph with a partial copy. It now uses only a recorded, verified
  copy, and checks it before the first rename.
- A rollback that failed part way could not be re-run, and `finish` could then delete the last good
  graph. It now runs again, and `finish` refuses until it completes.
- A neighbour's normal change or a host reboot could wedge rollback and `finish`. Rollback now only
  reports it, and forward stages have `--accept-shared-change`.
- `reset-neo4j` could run before `reset-postgres`, and rollback could refuse with the graph already
  gone. Stages now enforce their order, and rollback works from any point after `capture`.
- Re-running `reset-neo4j`, `capture`, `bootstrap` or `start`, or reusing `$W`, could damage state or
  run `schema-init` during the load. Each is now safe or refused.
- `restart: always` brings stopped containers back on a daemon restart. Writers are now removed, with
  their logs and OOM state saved first.
- The rollback steps removed the tool they called, and could leave new images on the old schema. Only
  the pins move back, and the tool checks them.
- `preflight` was optional and did not check the release set or the cap, the user-table list was
  hand-kept, `trim` was a weaker rule than `strip_person_name`, and the identity proof could pass on
  a partial projection.

112 mutations of the guards, including every kind the reviews named, now fail
`tests/deploy/test_catalog_reset.py`.

The bead's own reviewer, on `d6afe8d`, found the design sound and bounced it on
smaller points, now fixed: a rollback after a dead `finish` is refused and says to run `finish` again,
every forward stage refuses once a rollback has started, `capture` re-checks the user tables after
the stop, `preflight` needs a matching rehearsal report, the ledger poll is off for the load, and
`$W`'s records are fsynced and replaced atomically. Its limits are the list above.
