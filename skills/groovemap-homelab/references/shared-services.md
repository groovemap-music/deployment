# Reusing an existing PostgreSQL or Valkey

The user may prefer to reuse a PostgreSQL or Valkey the homelab already runs. This page says
what reuse requires, how the repository's **external-stores mode** does it, and when `just deploy`
**refuses it**. SKILL.md step 2a asks the question; this page backs the answer.

Discovery is never authorization. Finding a compatible instance does not permit connecting to
it, creating anything on it or changing it. Never display its credentials, and never run
`FLUSH*`, `CONFIG SET` or any other write against an instance GrooveMap does not own.

## Current status: supported

Tracking: epic `gm-deployment-e1uh`. The repository has the mode: `GROOVEMAP_POSTGRES=external`
and `GROOVEMAP_VALKEY=external` in `.env` select an existing server for each store, independently.
The Discogs-before-MusicBrainz gate stays authoritative in it.

`just deploy` refuses each external store, with exit 3 before anything changes, unless the
reviewed release set supports it. The floors sit beside `RELEASED_IMAGE_DIGESTS` in
`scripts/check-images.py` as `EXTERNAL_STORE_FLOORS`, and the reviewed set meets both:

| Store | Needs | Upstream work | Status |
| --- | --- | --- | --- |
| PostgreSQL | database-schema with `POSTGRES_PREPROVISIONED` (verify an operator-provisioned database, role and `pg_trgm` instead of creating them) | `gm-database-schema-2aty` | released in database-schema 0.6.0, promoted (floor met) |
| Valkey | catalog-api and analytics-engine that apply `VALKEY_KEY_PREFIX` to every key (python-libraries `valkey_key`) and connect over TLS (`VALKEY_TLS`) | `gm-python-libraries-3sm` and its TLS settings (merged), `gm-catalog-api-rchs`, `gm-analytics-engine-bah5`, `gm-catalog-api-vd7n`, `gm-analytics-engine-m0sz` | released in catalog-api 0.6.0 and analytics-engine 0.5.0, promoted (floor met) |

A floor is the first release with the capability. `.env` must run exactly the reviewed digests
(`just env-init`), or `just deploy` still refuses. A store whose floor reads `unreleased`, or
sits above the pinned version, cannot be external: **the skill keeps the deployment stopped**
when the user chooses reuse of it, until they pick dedicated instances or the floor is met.

## How the mode keeps the gate authoritative

These were the blockers to reuse, and the risks reuse adds; each now has a supported answer, verified in the code:

1. **The gate and its probes read the database the loaders write.** One store-access layer,
   `scripts/stores.py`, routes every PostgreSQL query of `scripts/discogs_import_status.py` and
   of deploy's MusicBrainz latch probe: `docker compose exec postgres` locally, or a throwaway
   `store-check-postgres` container (the pinned postgres image, on the stack network, with the
   credentials from the secret files) for an external server. An unreachable server is
   "cannot determine" (exit 2), never complete.
2. **The local stores are omitted, not removed.** `docker-compose.external-postgres.yml` and
   `docker-compose.external-valkey.yml` move the store and its exporter into the `local-stores`
   profile, which nothing enables, and the services wait for a store-check one-shot instead of
   the local store's health. The base files are untouched, so a dedicated deployment renders
   byte for byte as before, and a bare `docker compose up` in an existing checkout keeps its
   database containers.
3. **`schema-init` needs no privileges a shared server should not grant.** In external mode it
   runs with `POSTGRES_PREPROVISIONED=true`: no `CREATE DATABASE`, `CREATE ROLE` or
   `CREATE EXTENSION`, only the schema DDL inside GrooveMap's own database.
4. **Valkey keys are namespaced.** api and insights get `VALKEY_KEY_PREFIX` and `VALKEY_DB`,
   and an empty prefix is refused before anything starts.
5. **Credentials never cross the network in the clear by default.** Every PostgreSQL client gets
   `PGSSLMODE` (`EXTERNAL_POSTGRES_SSLMODE`, default `verify-full`, checked against
   `secrets/postgres_ca.crt`), and api and insights get `VALKEY_TLS` with
   `secrets/valkey_ca.crt`. Cleartext to a server off this machine (anything but loopback and
   `host.docker.internal`) is refused unless `EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true` or
   `EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true`, which every run then warns about.
6. **A bare `docker compose` cannot diverge.** While a store is external, `just deploy` keeps
   `COMPOSE_FILE` in `.env` equal to its own files (production overlay, external overlays,
   override), so `docker compose up/ps/logs/exec` without `-f` never starts the local stores and
   points the services at them. It refuses a `COMPOSE_FILE` it did not write.
7. **The override safety check still refuses half-wired reuse.** An override that changes
   `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USERNAME`, `VALKEY_HOST` or
   `VALKEY_PORT` (or their `_FILE` variants) on any service, or a `postgres_*` or `valkey_*`
   secret's source, stops `just deploy` with exit 3. The hint says an existing store is reached
   "only through the supported external-stores mode" and cites `gm-deployment-e1uh` and
   `docs/homelab.md`. There is no opt-in. `POSTGRES_HOST_PORT` and `VALKEY_HOST_PORT` only
   publish a port and stay allowed.

Before anything starts, `just deploy` also refuses: external stores with `--dev`; unusable
settings (a missing host, `EXTERNAL_POSTGRES_DATABASE=postgres` or a template, an empty or
glob-like prefix); a missing operator-written secret; and an external PostgreSQL in a project
that already has a `postgres_data` volume (a dedicated deployment whose data has not moved).

## What PostgreSQL reuse requires

- **PostgreSQL 18 or newer with pgvector**: `ghcr.io/groovemap-music/postgres` (the image the
  dedicated stack runs: the official `postgres:18-alpine` plus pgvector) or an equivalent. Keep
  the libc the data directory was created with. A server on `postgres:18-alpine` switches by
  changing its image only, while a move between musl and glibc (Debian `pgvector/pgvector`)
  reorders text and needs a full `REINDEX`. Changing the server's image is the administrator's
  decision, never yours.
- **`shared_preload_libraries = 'pg_stat_statements'` with `pg_stat_statements.track_utility =
  off`.** With utility tracking on, `ALTER ROLE ... PASSWORD` is stored in plaintext. Turning
  it off doesn't purge entries already stored, so the administrator then checks for those role
  password statements and removes them (SQL in `docs/homelab.md`).
- **`pg_trgm`, `vector` and `pg_stat_statements` created by a superuser in GrooveMap's
  database.** Don't ask for `pg_read_all_stats` for GrooveMap's role: every service connects as
  that one login, which already sees its own statements, and the grant would expose other
  tenants' query text on a shared server. Without `vector` there are no artist embeddings,
  and without `pg_stat_statements` there is no query audit. Schema init skips each one with a
  notice; `store-check-postgres` only notes a missing `vector` and does not check
  `pg_stat_statements`, so ask the administrator about both.
- **A separate GrooveMap database and a dedicated role**, never another application's database
  or role. The role owns the database (or holds `CREATE` on it and on `public`) and needs no
  `SUPERUSER`, `CREATEDB` or `CREATEROLE`. The administrator creates the database, role and
  extensions once (SQL in `docs/homelab.md`, Existing PostgreSQL and Valkey).
- **A connection budget** of at least 60 usable connections for GrooveMap, on top of the other
  applications, and no role or database connection limit below that.
- **PgBouncer: session pool mode only.** catalog-api holds a session-level
  `pg_try_advisory_lock`, which transaction and statement pooling break silently. If the
  existing PgBouncer is in `transaction` mode, reuse through it is out; a direct connection or
  a separate session-mode pool is the option. `store-check-postgres` refuses it: two concurrent
  sessions that share one server backend mean transaction or statement pooling.

## What Valkey reuse requires

- **Valkey 8.0 or newer** (or Redis 7.2 or newer; tested with Valkey 9.1) and
  **authentication on**. GrooveMap authenticates with a password only.
- **`maxmemory-policy noeviction` and AOF persistence.** GrooveMap stores state that must not
  be evicted. Do not change the setting on a shared instance: if it is wrong, reuse is out.
- **Key namespacing**: `EXTERNAL_VALKEY_KEY_PREFIX`, a GrooveMap-specific prefix no other
  application uses.
- **Application isolation.** A separate logical database (`SELECT n`) is **not a security
  boundary**: any client with the password can switch databases, and `FLUSHALL` and `CONFIG`
  are instance-wide. A prefix only keeps cooperating applications apart. Real isolation needs
  a dedicated instance.
- Never `FLUSHDB`, `FLUSHALL` or `CONFIG SET` on another application's instance.
  `store-check-valkey` sends only `AUTH`, `SELECT`, `PING`, `INFO` and `QUIT`, and over TLS sends
  the password only after the certificate has verified for the host name.

## Procedure

Only for a store whose floor is met, and only with the user's approval at each step.

1. **The operator provisions the server** (you may give them the SQL; never run a write on a
   shared server yourself): the database, role and extensions for PostgreSQL; for Valkey, the
   settings above, confirmed read-only during discovery.
2. **The operator writes the credentials** into `secrets/postgres_username.txt`,
   `secrets/postgres_password.txt` and `secrets/valkey_password.txt` (mode 0600), for example
   from SOPS. Never read, print or paste them.
3. **You set the `.env` values**: `GROOVEMAP_POSTGRES=external` with `EXTERNAL_POSTGRES_HOST`,
   `EXTERNAL_POSTGRES_PORT`, `EXTERNAL_POSTGRES_DATABASE` and, if not `verify-full`,
   `EXTERNAL_POSTGRES_SSLMODE`; `GROOVEMAP_VALKEY=external` with `EXTERNAL_VALKEY_HOST`,
   `EXTERNAL_VALKEY_PORT`, `EXTERNAL_VALKEY_DB` and `EXTERNAL_VALKEY_KEY_PREFIX`. TLS is on for
   both; the operator also writes `secrets/postgres_ca.crt` and `secrets/valkey_ca.crt`. Set a
   cleartext opt-out only when the user asks for it after hearing the risk. The server must be reachable from containers: a LAN address,
   or `host.docker.internal` for one on the Docker host.
   If the server is a container on its own Docker network (`EXTERNAL_*_HOST` is a container name),
   the override must attach **every** client of that store to the network. PostgreSQL:
   `api`, `brainztableinator`, `dashboard`, `embeddings`, `insights`, `schema-init`, `similar-artists`, `store-check-postgres` and `tableinator`. Valkey: `api`, `dashboard`, `insights` and `store-check-valkey`. Start from
   [`assets/override-external-store-network.yml`](../assets/override-external-store-network.yml)
   ([override.md](override.md#attaching-to-an-existing-stores-network)). A host that is a LAN
   address or `host.docker.internal` needs none.
4. **`just deploy --no-wait`.** It checks the settings, the floors and the secrets, adds the
   overlays, and runs `store-check-postgres` and `store-check-valkey` before anything starts.
   A `[stop]` line or a `FAILED:` line names the fix; read it to the user.
5. Commands you run by hand need no extra `-f`: `COMPOSE_FILE` in `.env` already names the
   overlays, and `just discogs-import-status --prod` adds them itself. Never edit that line.

Backups, upgrades, restarts, disk space and monitoring of the external server stay with its
operator. `postgres-exporter` and `valkey-exporter` do not run in this mode: their scrape jobs get
no target and the console shows them disabled. If an exporter already watches the shared
PostgreSQL (typically beside it in the same stack), set `EXTERNAL_POSTGRES_EXPORTER_ADDRESS` to
its `host:port`: the collector scrapes it and the console probes it as shared. `otel-collector`
and `dashboard` must then reach that address too (see the monitoring network below).

## Comparing the options

| | Dedicated (default) | Reuse (external-stores mode) |
| --- | --- | --- |
| Deployable | Yes | Yes, when the capability floors are met (they are) and the store checks pass |
| Compatibility | Pinned versions, tested | PostgreSQL 18, Valkey 8.0+, session-mode PgBouncer; verified by the store checks |
| Isolation | Full | Database and role, and a key prefix; a logical DB or prefix alone is not a security boundary |
| Capacity | Sized by the overlay | Shared connections and memory with the other application |
| Operational impact | One more PostgreSQL and Valkey to back up | GrooveMap's upgrades and load affect, and are affected by, the other application |

Recommend dedicated. Once a floor is met, recommend reuse of that store only for an instance
that passes every requirement above and is operated by the user who asked for it.

## Shared monitoring

A separate choice from the stores: `GROOVEMAP_MONITORING=shared` in `.env` makes GrooveMap report to
an existing Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor instead of running
its own. Read the current value from `.env` first; it is the deployment's decision, not yours.
Full procedure: [docs/homelab.md](../../../docs/homelab.md#shared-monitoring).

What you set, with approval, and nothing else:

| `.env` value | What to put there |
| --- | --- |
| `GROOVEMAP_MONITORING` | `shared` |
| `SHARED_VICTORIA_METRICS_URL` | Base URL of the shared VictoriaMetrics as a container reaches it, such as `http://victoria-metrics:8428` (required) |
| `SHARED_VICTORIA_TRACES_URL` | The same for VictoriaTraces, such as `http://victoria-traces:10428` (required) |
| `SHARED_GRAFANA_URL` | The shared Grafana as the console's container reaches it, such as `http://grafana:3000` |
| `GRAFANA_URL` | The shared Grafana as the operator's browser reaches it |
| `SHARED_NODE_EXPORTER_ADDRESS`, `SHARED_CADVISOR_ADDRESS` | `host:port` of each, when GrooveMap's host dashboards should include them |
| `EXTERNAL_POSTGRES_EXPORTER_ADDRESS` | Only with `GROOVEMAP_POSTGRES=external`: `host:port` of the exporter that watches that server |

**Network.** Exactly two GrooveMap services talk to shared monitoring services: `otel-collector`
(it writes metrics and spans and scrapes the shared exporters) and `dashboard` (the console probes
them and reads history and alerts). No application talks to them; they send telemetry only to
`otel-collector`. When the shared services are containers on another Docker network, attach those
two services to it, and only those: start from
[`assets/override-shared-monitoring-network.yml`](../assets/override-shared-monitoring-network.yml).
The list is `MONITORING_CLIENTS` in `scripts/stores.py`, and the tests keep the example equal to it.
A LAN address or `host.docker.internal` needs no attachment.

**The shared Grafana.** Someone with admin rights there imports GrooveMap's dashboards and alert
rules ([docs/observability.md](../../../docs/observability.md#shared-grafana)), ideally into an
organization of their own; every query is scoped to `service_namespace="groovemap"`, so they show
only GrooveMap's series. You never sign in to it and never handle its credentials.

**Viewer token (optional).** It lets the console's attention bar list firing Grafana alerts; the
console's deep links and its Grafana health row work without it. It is created on the shared
Grafana, by its administrator: a service account with the **Viewer** role in GrooveMap's
organization, then a token, which the operator writes to `secrets/grafana_viewer_token.txt` from
their secret store ([maintenance](../../../docs/maintenance.md#on-a-shared-grafana)). This stack
runs no Grafana in shared mode, so never suggest creating or using a local Grafana login for it.

**Tell the user** that the console needs operations-console v0.3.0 or newer (the reviewed set
pins it) to show the shared services truthfully; an older one shows them as unreachable
(`just deploy` warns), and that
shared endpoints needing authentication are not supported yet.
