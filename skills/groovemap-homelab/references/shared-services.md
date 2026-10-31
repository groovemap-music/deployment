# Reusing an existing PostgreSQL or Valkey

The user may prefer to reuse a PostgreSQL or Valkey the homelab already runs. This page says
what reuse would require, and why it is **not deployable today**. SKILL.md step 2a asks the
question; this page backs the answer.

Discovery is never authorization. Finding a compatible instance does not permit connecting to
it, creating anything on it or changing it. Never display its credentials, and never run
`FLUSH*`, `CONFIG SET` or any other write against an instance GrooveMap does not own.

## Current status: not deployable

Reuse is **not deployable** with this repository. Tracking: epic `gm-deployment-e1uh`. The
blockers, each verified in the code:

1. **The deploy gate and its probes read the stack's own postgres.** `deploy.sh`
   (`stack_running`, the MusicBrainz latch probe) and `scripts/discogs_import_status.py` use
   `docker compose exec postgres`. Pointing services elsewhere would leave the authoritative
   Discogs-before-MusicBrainz gate reading a database nothing writes to.
2. **The local stores cannot be removed.** The override check forbids removing a service, and
   the app services `depends_on` the local postgres and valkey health.
3. **`schema-init` needs privileges a shared instance should not grant:** `CREATE DATABASE`,
   `CREATE ROLE` and `CREATE EXTENSION` (`pg_trgm`, `vector`) on PostgreSQL 18.
4. **Valkey has no key prefix and no DB-index setting yet**, and GrooveMap's key names share
   Discogsography's lineage, so two such applications on one Valkey could read each other's keys.
5. **The override safety check refuses half-wired reuse.** An override that changes
   `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USERNAME`, `VALKEY_HOST` or
   `VALKEY_PORT` (or their `_FILE` variants) on any service, or a `postgres_*` or `valkey_*`
   secret's source, stops `just deploy` with exit 3 before anything starts. The message says
   "External PostgreSQL and Valkey are not supported yet" and cites `gm-deployment-e1uh` and
   `docs/homelab.md`. There is no opt-in. `POSTGRES_HOST_PORT` and `VALKEY_HOST_PORT` only
   publish a port and stay allowed.

**Required upstream work**, tracked under `gm-deployment-e1uh`: make the gate and probes address
the configured store; make the local stores optional; split `schema-init` so the privileged
steps run once under operator control; add a Valkey key prefix and DB-index setting. The
library and service changes are `gm-python-libraries-3sm`, `gm-catalog-api-rchs`,
`gm-analytics-engine-bah5` and `gm-database-schema-2aty`.

**The skill keeps the deployment stopped** when the user chooses reuse, until they pick
dedicated instances or the upstream work lands and this page is updated. Do not write an
override that works around a blocker.

## What PostgreSQL reuse will require

- **PostgreSQL 18**, with `pg_trgm` and `vector` available (`pg_available_extensions`).
- **A separate GrooveMap database and a dedicated role**, never another application's database
  or role. Privileges: ownership of that database; `CREATE EXTENSION` for `pg_trgm` and
  `vector` (a superuser or trusted-extension arrangement run once by the operator); `CREATE ROLE`
  only if `schema-init` still creates service roles.
- **A connection budget.** Sum GrooveMap's pools (`POSTGRES_POOL_MAX_SIZE` across services) and
  compare with `max_connections` minus connections in use. Set a role connection limit.
- **PgBouncer: session pool mode only.** catalog-api holds a session-level
  `pg_try_advisory_lock`, which transaction and statement pooling break silently. If the
  existing PgBouncer is in `transaction` mode, reuse through it is out; a direct connection or
  a separate session-mode pool is the option.

## What Valkey reuse will require

- **A supported server version** (GrooveMap is tested with Valkey 9.1) and **authentication on**.
- **`maxmemory-policy noeviction` and AOF persistence.** GrooveMap stores state that must not
  be evicted. Do not change the setting on a shared instance: if it is wrong, reuse is out.
- **Key namespacing**: a GrooveMap-specific key prefix (blocker 4).
- **Application isolation.** A separate logical database (`SELECT n`) is **not a security
  boundary**: any client with the password can switch databases, and `FLUSHALL` and `CONFIG`
  are instance-wide. Isolation needs a dedicated ACL user limited to the key prefix, or a
  dedicated instance.
- Never `FLUSHDB`, `FLUSHALL` or `CONFIG SET` on another application's instance.

## Comparing the options

| | Dedicated (supported) | Reuse (not deployable) |
| --- | --- | --- |
| Compatibility | Pinned versions, tested | Must be PostgreSQL 18, supported Valkey, session-mode PgBouncer |
| Isolation | Full | Database and role, ACL and prefix; a logical DB alone is not enough |
| Capacity | Sized by the overlay | Shared connections and memory with the other application |
| Operational impact | One more PostgreSQL and Valkey to back up | GrooveMap's upgrades and load affect, and are affected by, the other application |

Recommend dedicated. After the upstream work lands, recommend reuse only for an instance that
passes every requirement above and is operated by the user who asked for it.
