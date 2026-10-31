# ⚙️ Configuration Guide

<div align="center">

**Complete configuration reference for all GrooveMap services**

[🏠 Back to Main](../README.md) | [📚 Documentation Index](README.md) | [🚀 Quick Start](quick-start.md)

</div>

## Overview

GrooveMap's deployment inputs are the digest-pinned image variables and local
development defaults in `.env.example`, plus the file-backed secrets declared
by `docker-compose.prod.yml`. Compose also sets source-owned runtime variables
on each service. Treat the rendered Compose model, rather than this prose or a
source repository's example environment, as the final value presented to a
container.

## Configuration Methods

### 1. Environment File (.env) — Development

The required approach for image promotion and host-side Compose interpolation
is an untracked `.env` file:

```bash
# Copy the example file
cp .env.example .env

# Edit with your settings
nano .env
```

Only values referenced as `${NAME}` in the Compose files are interpolated from
this file. Entries such as `RABBITMQ_HOST` and `POSTGRES_HOST` are useful when
running a source-owned process directly, but they do not override the literal
container environment in `docker-compose.yml`. Use a Compose override for that.

> **Production**: Do not put real credentials in `.env`. The file still carries
> required image references and non-secret interpolation inputs; use Docker
> Compose runtime secrets for credentials — see [Production Secrets](#production-secrets).

### 2. Direct Compose interpolation — development

Shell variables override matching `.env` interpolation inputs, such as an image
reference or host UID:

```bash
export CATALOG_API_IMAGE="ghcr.io/groovemap-music/catalog-api@sha256:<digest>"
env UID="$(id -u)" GID="$(id -g)" docker compose config
```

This does not replace `services.api.environment.POSTGRES_HOST`, for example,
because that value is declared directly in Compose rather than interpolated.

### 3. Docker Compose Runtime Secrets — Production

In production, credentials are mounted at `/run/secrets/*` via
`docker-compose.prod.yml`, so their values are not embedded in container
environment variables or normal `docker inspect` output. The source files
under untracked `secrets/` still exist on the Docker host: protect and remove
them according to the environment's secret-management policy.

```bash
# Generate secrets once (idempotent)
just secrets-bootstrap

# Render the production overlay without starting it
just config-prod
```

See [Production Secrets](#production-secrets) below and [Docker Security](docker-security.md) for full details.

### 4. Docker Compose Override

Put host-specific, non-secret settings in an untracked
`docker-compose.override.yml` (`.gitignore` already lists it) rather than
editing the tracked Compose files:

```yaml
services:
  dashboard:
    environment:
      - LOG_LEVEL=DEBUG
      - SESSION_CACHE_SECONDS=10
```

Compose reads `docker-compose.override.yml` by itself only when no `-f` is
given, so a plain `docker compose up` merges it into the base stack, but the
production stack has to name it last:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml config
```

`just deploy` does this for you: when the file exists, it adds it to every
Compose call and to the import gate. First it renders the stack with and
without the override (`config --format json`, every profile included) and stops
with exit code 3 if the override changes what only `.env` or deploy may
change on a stack service: `image`, `build`, `platform`, `container_name` or
`profiles`, or the `ports` or `network_mode` of PostgreSQL, Neo4j, RabbitMQ,
Valkey, VictoriaMetrics or VictoriaTraces, or their `networks`, `labels` or
`extra_hosts` (a proxy network, Traefik TCP router labels or a hosts entry would
expose or redirect the data tier). Reverse-proxy labels and networks on
other services such as `explore`, `dashboard` and `grafana`, volume locations,
restart policies and the `groovemap` network's subnet are fine. The check denies
what it does not understand. An existing service may not gain `privileged`, `cap_add`,
`devices`, `security_opt`, `device_cgroup_rules`, `sysctls`, `runtime`, `isolation`, `group_add`, `pid`, `ipc`, `uts`, `cgroup` or `volumes_from`, or change `network_mode` on any service, or a mount of
the Docker socket, another sensitive host path or another service's data volume; it may
still move its own data volume to a host path. An extra service of your own may set
only `image`, `command`, `entrypoint`, `environment`, `env_file`, `labels`, `networks`,
`restart`, `healthcheck`, `logging`, `user`, `working_dir`, `read_only`, `tmpfs`, `init`,
`stop_grace_period`, `deploy.resources`, `container_name`, `depends_on` and `volumes`
(new named volumes, tmpfs or non-sensitive host paths), its image must be pinned by
digest (`repo@sha256:...`) and it may not publish ports or join the `groovemap` network.
List a service that must break any of these (a backup sidecar that reads `postgres_data`,
a forwarder into PostgreSQL) in `OVERRIDE_ALLOW_SERVICES` in `.env`
(comma-separated names, for example `OVERRIDE_ALLOW_SERVICES=pg-forward,backup`).
Volumes are compared by what the engine sees, not by Compose key: a top-level volume
declared with `name:` or `external: true` that resolves to a stack volume
(`<project>_postgres_data`) counts as that stack volume, and a bind mount or a
`driver_opts.device` that is, contains or lies inside the host directory another service's
data was relocated to is stopped for every other service. The override may not change `name`,
`external` or `driver` of the stack's own top-level volumes (`driver_opts`, the data's
location, may change), and two services may not relocate their data into overlapping
directories. Host paths are compared after leading slashes are collapsed and `.`/`..` resolved
(`//var/run/docker.sock` is the Docker socket). On a local engine the check also follows
symlinks, so a link to a sensitive or relocated path is judged by where it leads; with a
remote `DOCKER_HOST` the paths are on another machine, so symlinks are not resolved.
Existing services are checked against this denylist, not a key allowlist, because overrides
legitimately change labels, networks, environment, restart and volumes on web-tier services.
The digest and no-`build` rules have no opt-in, and neither do the rules for existing
services. Sensitive host paths are `/`, the Docker, containerd and podman sockets
(`/var/run/docker.sock`, `/run/docker.sock`, `/run/podman`, `/run/containerd`,
`/run/user/<uid>/...`), `/var/lib/docker`, `/var/lib/containerd`, `/var/lib/podman`,
`/var/lib/containers`, `/proc`, `/sys`, `/dev`, `/etc`, `/root` and `/boot`, anything
under them, and any parent that contains one (`/var/lib`, `/run`). The override may
not redefine a network that a data-tier service uses (`name`, `external`, `driver`,
`attachable`, `internal`, `driver_opts`), which would move the data tier without touching any
service's `networks`; changing its `ipam` subnet is fine. A new service that omits `networks`
joins the project's default network, a separate network from `groovemap`, so it needs no
network opt-in; that says nothing about what the service reaches on the host or LAN, which
the key and mount rules cover. The override may not point GrooveMap at an external PostgreSQL or Valkey, with no opt-in:
the check stops (exit 3, before any `compose up`) when it changes `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USERNAME`, `VALKEY_HOST` or `VALKEY_PORT`, or
their `_FILE` variants, on any stack service (an `env_file` counts, because Compose inlines it
into the rendered environment), or changes a `postgres_*` or `valkey_*` secret's source. External
stores are not supported yet because the deploy gate and the MusicBrainz latch probe read this
stack's own `postgres` container; see `gm-deployment-e1uh` and [homelab.md](homelab.md#existing-postgresql-and-valkey).
`POSTGRES_HOST_PORT` and `VALKEY_HOST_PORT` only publish a port and stay allowed. Move published ports with the
[host port and bind address variables](#host-ports-and-bind-addresses) in
`.env` instead. Run `just discogs-import-status --prod -f docker-compose.override.yml`
to address the same stack by hand.

## Core Settings

### Effective Compose inputs

The following groups are interpolated by the executable stack:

| Inputs | Purpose |
| --- | --- |
| `DATABASE_SCHEMA_IMAGE`, `CATALOG_API_IMAGE`, `DISCOGS_INGESTION_IMAGE`, `MUSICBRAINZ_INGESTION_IMAGE`, `DISCOGS_GRAPH_ENRICHER_IMAGE`, `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE`, `DISCOGS_SQL_LOADER_IMAGE`, `MUSICBRAINZ_SQL_LOADER_IMAGE`, `OPERATIONS_CONSOLE_IMAGE`, `GRAPH_EXPLORER_IMAGE`, `ANALYTICS_ENGINE_IMAGE` | Required immutable source-owned images |
| `UID`, `GID` | Host identity stamped on internal-image containers |
| `APP_BASE_URL`, `DB_PROFILING`, `INSIGHTS_INTERNAL_SECRET`, `NLQ_ENABLED`, `NLQ_API_KEY`, `NLQ_MODEL` | Optional base-stack application settings (`APP_BASE_URL` is required by the production overlay) |
| `GRAFANA_URL`, `TRACES_URL` | Browser-facing Grafana and traces base URLs for the operations console's deep links (default `http://localhost:3000`) |
| `RABBITMQ_DLQ_MAX_LENGTH`, `RABBITMQ_DLQ_MAX_LENGTH_BYTES`, `RABBITMQ_DLQ_OVERFLOW` | Catalog dead-letter queue size-cap policy (defaults `50000`, `536870912`, `drop-head`); see [the admin guide](admin-guide.md#dlq-size-cap-policy) |
| `SOURCE_DUMP_ID`, `SOURCE_DUMP_DATE`, `EMBEDDING_PIPELINE_POSTGRES_USERNAME`, `EMBEDDING_PIPELINE_POSTGRES_PASSWORD` | One-shot `embeddings` job only; see [Embeddings job](#embeddings-job) |
| `RESEND_SENDER_EMAIL`, `RESEND_SENDER_NAME` | Production overlay email sender (defaults `noreply@groovemap.music`, `GrooveMap`) |
| `NEO4J_HEAP_SIZE`, `NEO4J_PAGECACHE_SIZE`, `NEO4J_MEMORY_LIMIT`, `NEO4J_TRANSACTION_MEMORY_MAX` | Production overlay sizing (defaults `8g`, `16g`, `48G`, `8G`); see [Neo4j sizing written by `just deploy`](#neo4j-sizing-written-by-just-deploy) |
| `GM_EXTRACTION_RULES_FILE` | Optional host path for the Discogs extraction rules mounted by the infrastructure-smoke overlay (`docker-compose.smoke.yml`) |
| `SMOKE_MEDIA_API_PORT`, `SMOKE_MEDIA_RABBITMQ_PORT`, `SMOKE_MEDIA_SERVICE_PLATFORM`, `SMOKE_MEDIA_SUBNET` | Isolated media-smoke settings only |
| `SMOKE_ERASURE_API_PORT`, `SMOKE_ERASURE_SERVICE_PLATFORM`, `SMOKE_ERASURE_SUBNET` | Isolated erasure-smoke settings only |
| `SMOKE_RELEASED_FIXTURE_RABBITMQ_PORT`, `SMOKE_RELEASED_FIXTURE_SERVICE_PLATFORM`, `SMOKE_RELEASED_FIXTURE_SUBNET` | Isolated released-fixture-smoke settings only |

### Host ports and bind addresses

Every published host port is an interpolation input whose default is the stock
mapping, so an unset variable renders the same Compose configuration as before.
Only the host side moves; container ports and in-network addresses are fixed.
If you move a port that a browser or link uses, update `APP_BASE_URL`,
`GRAFANA_URL` and `TRACES_URL` to match.

| Variable | Default | Publishes |
| --- | --- | --- |
| `GRAFANA_HOST_PORT` | `3000` | Grafana |
| `API_HOST_PORT`, `API_HEALTH_HOST_PORT` | `8004`, `8005` | Catalog API, API health |
| `DASHBOARD_HOST_PORT` | `8003` | Operations console |
| `EXPLORE_HOST_PORT`, `EXPLORE_HEALTH_HOST_PORT` | `8006`, `8007` | Graph Explorer, Explorer health |
| `POSTGRES_HOST_PORT` | `5433` | PostgreSQL (container port 5432) |
| `NEO4J_HTTP_HOST_PORT`, `NEO4J_BOLT_HOST_PORT` | `7474`, `7687` | Neo4j browser, Bolt |
| `RABBITMQ_AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT` | `5672`, `15672` | RabbitMQ AMQP, management UI |
| `RABBITMQ_METRICS_HOST_PORT` | `15692` | RabbitMQ Prometheus metrics (loopback) |
| `VALKEY_HOST_PORT` | `6379` | Valkey |
| `VICTORIA_METRICS_HOST_PORT` | `8428` | VictoriaMetrics |
| `VICTORIA_TRACES_HOST_PORT` | `10428` | VictoriaTraces |

| Bind address | Default | Applies to |
| --- | --- | --- |
| `WEB_BIND_ADDRESS` | unset (all interfaces) | Grafana, API, dashboard, Explore |
| `DATA_BIND_ADDRESS` | unset (all interfaces) | PostgreSQL, Neo4j, RabbitMQ AMQP and management; Valkey, VictoriaMetrics and VictoriaTraces in the base file |
| `INTERNAL_BIND_ADDRESS` | `127.0.0.1` | RabbitMQ metrics in both files; Valkey, VictoriaMetrics and VictoriaTraces in the production overlay |

Set `WEB_BIND_ADDRESS` or `DATA_BIND_ADDRESS` to an IP (for example
`127.0.0.1`) to stop publishing on every interface. The production overlay
still publishes PostgreSQL, Neo4j and RabbitMQ on all interfaces by default;
set `DATA_BIND_ADDRESS` to narrow that.

`ENCRYPTION_MASTER_KEY` and `RESEND_API_KEY` are not interpolated: the base file
carries them only as commented-out entries, and the production overlay delivers
them as the `encryption_master_key` and `resend_api_key` file secrets.

`docker compose config --environment` shows interpolation inputs, while
`just config` and `just config-prod` show the complete effective container
configuration. The sections below describe service runtime contracts; change a
literal Compose value with a reviewed override file, not by assuming every
source-level variable is an `.env` interpolation.

### Neo4j sizing written by `just deploy`

`just deploy` writes `NEO4J_HEAP_SIZE`, `NEO4J_PAGECACHE_SIZE`,
`NEO4J_MEMORY_LIMIT` and `NEO4J_TRANSACTION_MEMORY_MAX` into `.env` for the
production overlay, sized from the
memory Docker reports (`docker info` MemTotal, rounded up to whole GB, so a
16 GB machine counts as 16). For total memory T in GB:

- heap + page cache = half of T, at most 24 GB (the overlay's 8g + 16g)
- heap = a third of that, at least 1 GB and at most 8 GB; page cache = the rest
- limit = twice heap + page cache, but at most T − 4 GB, and always at least
  1 GB above heap + page cache
- transaction memory cap = 70% of the heap, rounded down to whole GB, at least
  1 GB

| Memory | Heap | Page cache | Limit | Transaction cap |
| --- | --- | --- | --- | --- |
| 16 GB | `2g` | `6g` | `12G` | `1g` |
| 32 GB | `5g` | `11g` | `28G` | `3g` |
| 64 GB or more | `8g` | `16g` | `48G` | `5g` |

From 64 GB the heap, page cache and limit are the overlay's own defaults. The
transaction cap is not: the overlay's `NEO4J_TRANSACTION_MEMORY_MAX` default
of 8G equals its 8g heap. Neo4j tracks transaction memory inside the heap, so a
cap that is not below the heap bounds nothing, and a large transaction can
exhaust the heap and stop the JVM (the overlay sets `ExitOnOutOfMemoryError`).
Keeping the cap at 70% leaves the rest of the heap for Neo4j itself, so such a
transaction fails on its own instead. Only a 1 GB heap, below the production
minimum and reached only with `--force`, gets a cap as large as the heap.

It writes a value only when nobody has chosen one: the variable is absent or
empty, or it is still exactly what `.env.example` ships. The template's
`NEO4J_HEAP_SIZE=2G` counts as unset, because left alone it would hold the
production heap at 2 GB beside a 16 GB page cache. Deploy marks what it wrote
with a comment and never rewrites any of it, so edit the values freely
afterwards. If you set the heap yourself and leave the transaction cap unset,
deploy derives the cap from your heap. The base stack (`just deploy --dev`)
fixes Neo4j's memory in `docker-compose.yml`, so nothing is written there.

### Identity ledger refresh

The Identity Ledger's works list and density read precomputed views that `catalog-api` rebuilds.
Re-attachment and backfill runs rebuild them inline whether or not anything else is set.
After loads, a background poll in `catalog-api` compares the sources' write counts, so
`docker-compose.yml` sets `IDENTITY_LEDGER_REFRESH_ENABLED=true` on the `api` service. The
production and smoke overlays inherit it. `catalog-api` defaults it to `false`, and with the poll
off, link changes from loads never reach the ledger until the next re-attachment run, and a new
deployment answers `503` on the list and density until that run.

| Variable | Default | Meaning |
| --- | --- | --- |
| `IDENTITY_LEDGER_REFRESH_ENABLED` | `false` in `catalog-api`, `true` in this stack | Run the change-detection poll |
| `IDENTITY_LEDGER_REFRESH_INTERVAL` | `300` | Seconds between polls (at least 1) |
| `IDENTITY_LEDGER_REFRESH_QUIET` | `600` | Seconds the sources must hold still after a change before the poll refreshes |
| `IDENTITY_LEDGER_MAX_STALENESS` | `21600` (6 h) | Age of the last refresh after which the poll refreshes a changed ledger even while writes continue |

Staleness bounds, with the stack's settings: a new deployment builds the unpopulated views at the
first poll, which starts within `IDENTITY_LEDGER_REFRESH_INTERVAL` (5 minutes) of `catalog-api`
starting. That first build takes about 2 to 3 minutes at 50% synthetic scale and longer at full
scale, and until it completes the works list and density answer `503` with `Retry-After`.
After a load, the poll refreshes once the sources have been quiet for 10 minutes and the next poll
runs, so the refresh starts within about 15 minutes of the load's last write. The refresh itself
then takes 195 seconds at 50% scale and several minutes at full catalog size (about 6 to 7
minutes extrapolated), so the ledger catches up about 22 minutes after the load's last write at
full size. During continuous writes it still refreshes once the last refresh is 6 hours old.
Every ledger payload carries `refreshed_at`, the
instant the last refresh started, so a console shows how old the data is. A failed refresh is
logged with `alert=identity_ledger_refresh_failed` and keeps the previous rows. See
[`catalog-api`'s README](https://github.com/groovemap-music/catalog-api/blob/main/api/README.md#keeping-the-identity-ledger-fresh).
The variables are `catalog-api` settings, not Compose interpolation inputs; change one with a
reviewed override file.

### Structured log environment

The production overlay explicitly sets `ENVIRONMENT=production` on every
GrooveMap application service, including the schema and embedding jobs. Apply
both `docker-compose.yml` and `docker-compose.prod.yml` to obtain that runtime
setting; the base development configuration leaves it unset.

The shared Python logger reads `ENVIRONMENT` when logging is configured and
falls back to `development` when the variable is absent. This adds the
`environment` field to structured logs; it does not set verbosity. `LOG_LEVEL`
controls verbosity independently. The telemetry resource attribute remains
`deployment.environment.name=prod`; its abbreviated value does not set the
logger's `ENVIRONMENT` variable.

`ENVIRONMENT` is a literal container value in the production overlay, rather
than an `.env` interpolation input. Changing a host shell variable or adding
`ENVIRONMENT` to `.env` does not override it. A reviewed Compose override may
change the literal value. Existing containers retain their old environment
until an operator applies the reviewed configuration and recreates them.

For post-deployment verification, inspect a newly emitted application JSON log
and confirm its `environment` field is `production`. A local Compose validation
proves the configuration supplied to containers; it does not prove that a live
host has applied it or that every third-party image emits the same log fields.

### RabbitMQ Configuration

RabbitMQ connections are configured using individual component variables.

| Variable            | Description        | Default          | Required |
| ------------------- | ------------------ | ---------------- | -------- |
| `RABBITMQ_HOST`     | RabbitMQ hostname  | `rabbitmq`       | No       |
| `RABBITMQ_PORT`     | RabbitMQ AMQP port | `5672`           | No       |
| `RABBITMQ_USERNAME` | RabbitMQ username  | `groovemap` | No       |
| `RABBITMQ_PASSWORD` | RabbitMQ password  | `groovemap` | No       |

**Used By**: both extractors, API, Graphinator, Tableinator, Brainzgraphinator, Brainztableinator. The Dashboard holds no RabbitMQ credential; it reads broker metrics from `RABBITMQ_PROMETHEUS_URL` (see [Dashboard Configuration](#dashboard-configuration)).

**Secret convention**: `RABBITMQ_USERNAME_FILE` / `RABBITMQ_PASSWORD_FILE` paths are supported for Docker Compose runtime secrets.

**Examples**:

```bash
# Local development
RABBITMQ_HOST=localhost
RABBITMQ_USERNAME=groovemap
RABBITMQ_PASSWORD=groovemap

# Docker Compose (internal network — these are the defaults)
RABBITMQ_HOST=rabbitmq

# Remote broker with custom credentials
RABBITMQ_HOST=rabbitmq.example.com
RABBITMQ_USERNAME=myuser
RABBITMQ_PASSWORD=mypassword
```

**Connection Properties**:

- Automatic reconnection on failure (up to 5 connection attempts with exponential backoff, behind a circuit breaker)
- Heartbeat: 600 seconds
- Prefetch count is set per consumer, not by a variable: 200 for Brainzgraphinator; in batch
  mode, the larger of 200 and twice the batch size for Graphinator and Tableinator (Tableinator
  without batch mode uses its connection-pool maximum); and the connection-pool maximum
  (channel-global) for Brainztableinator

### Data Storage

| Variable              | Description                      | Effective Compose value | Required |
| --------------------- | -------------------------------- | ----------------------- | -------- |
| `DISCOGS_ROOT`        | Discogs producer data path       | `/discogs-data`         | Yes      |
| `MUSICBRAINZ_ROOT`    | MusicBrainz producer data path   | `/musicbrainz-data`     | Yes      |
| `PERIODIC_CHECK_DAYS` | Per-source update check interval | `5` Discogs / `3` MusicBrainz | No |

**Used By**: `extractor-discogs` and `extractor-musicbrainz`, with separate
named volumes (`discogs_data` and `musicbrainz_data`).

**DISCOGS_ROOT Details**:

- Must be writable by service user (UID 1000 in Docker)
- Requires ~76GB free space for full dataset
- Contains downloaded XML files and metadata cache

**Directory layout**: the Discogs extractor keeps every dump file directly under
`DISCOGS_ROOT`, with no per-type subdirectories:

| Path | Purpose |
| --- | --- |
| `/discogs-data/discogs_20250115_artists.xml.gz` (and `_labels`, `_masters`, `_releases`) | Downloaded dump files |
| `/discogs-data/.discogs_metadata.json` | Checksums and sizes of the downloaded files |
| `/discogs-data/.extraction_status_<version>.json` | Per-dump processing state marker |
| `/discogs-data/flagged/<version>/<type>/` | Records flagged by the data-quality rules |

#### Disk check per-volume shares

`just deploy` resolves each data volume from the rendered Compose
config (override included), groups the volumes by filesystem, and checks every filesystem
against the sum of its volumes' shares (`DATA_VOLUMES` in `scripts/deploy_helpers.py`):

| Volume | Minimum | Recommended |
| --- | --- | --- |
| `postgres_data` | 48 GB | 48 GB |
| `neo4j_data` | 28 GB | 28 GB |
| `discogs_data` | 12 GB | 12 GB |
| `victoria_metrics_data` | 2 GB | 2 GB |
| `rabbitmq_data` | 2 GB | 2 GB |
| Docker's data root (images, layers, logs, small volumes) | 8 GB | 8 GB |
| `musicbrainz_data` | 0 | 100 GB (an estimate, not measured; gm-deployment-q4en) |

Named volumes on the default driver count against Docker's data root. Docker Desktop's VM
disk, a remote `DOCKER_HOST` and network filesystems cannot be measured, so they warn with
the sizes. The 76 GB Discogs figure is 28 GB Neo4j plus 48 GB PostgreSQL
([architecture](architecture.md)); the 2 GB, 2 GB and 8 GB allowances are rough.

**PERIODIC_CHECK_DAYS**:

- How often to check for new data dumps
- The minimum is `1`; a value of `0` is raised to one day, so periodic checks
  cannot be disabled
- The source-level default is 15 days; Compose deliberately overrides it per
  producer as shown above

## Database Connections

### Neo4j Configuration

| Variable            | Description                                             | Default     | Required |
| ------------------- | ------------------------------------------------------- | ----------- | -------- |
| `NEO4J_HOST`        | Neo4j hostname, or a full `scheme://host[:port]` URI    | `localhost` | Yes      |
| `NEO4J_PORT`        | Bolt port (used when `NEO4J_HOST` is a bare hostname)   | `7687`      | No       |
| `NEO4J_USERNAME`    | Neo4j username                                          | `neo4j`     | Yes      |
| `NEO4J_PASSWORD`    | Neo4j password                                          | (none)      | Yes      |
| `NEO4J_TLS_ENABLED` | Encrypt the Bolt connection to Neo4j (TLS)             | `false`     | No       |
| `NEO4J_TLS_VERIFY`  | Verify the Neo4j server certificate when TLS is enabled | `true`      | No       |

**Used By**: Graphinator, API, Schema-Init, Dashboard, Brainzgraphinator

**Connection Details**:

- Protocol: Bolt (binary protocol)
- Default port: 7687
- Connection pool: 50 connections (max), 60-second connection acquisition timeout
- Retry logic: Exponential backoff (max 5 attempts)
- Transaction timeout: set on the server, `120s` in the base file and `600s` in the
  production overlay (`NEO4J_db_transaction_timeout`)

**Examples**:

```bash
# Local development
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="password"

# Docker Compose
NEO4J_HOST="neo4j"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"

# Neo4j Aura (cloud) — NEO4J_HOST accepts a full "scheme://host" URI, passed
# through unchanged (Aura requires the neo4j+s:// routing+TLS scheme; do not
# set NEO4J_TLS_ENABLED here — the scheme already encrypts the connection)
NEO4J_HOST="neo4j+s://xxxxx.databases.neo4j.io"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="your-secure-password"

# TLS for a non-local deployment (verify a real/CA-issued certificate)
NEO4J_HOST="neo4j.example.com"
NEO4J_TLS_ENABLED="true"
NEO4J_TLS_VERIFY="true"
```

### Enabling TLS for Neo4j (production)

By default, services connect to Neo4j over **unencrypted Bolt** (`bolt://host:7687`), which is
acceptable only when the connection stays on a trusted host/network. For any deployment where
Bolt traffic crosses an untrusted network (separate host/VM, overlay network, cloud), enable TLS:

- `NEO4J_TLS_ENABLED=true` — encrypt the Bolt connection.
- `NEO4J_TLS_VERIFY=true` (default) — verify the server certificate against the system CA bundle.
  Set to `false` only for self-signed/internal certificates: traffic stays encrypted, but the
  server identity is not verified (no protection against an active man-in-the-middle).

> **Bolt is not HTTP.** A reverse proxy that TLS-terminates the Neo4j _Browser_ (HTTP, port 7474)
> does **not** secure the Bolt protocol (port 7687) that these services use. To actually encrypt
> Bolt you must terminate TLS at one of:
>
> 1. **Neo4j-native Bolt TLS** — configure an SSL policy + certificate on the Neo4j server
>    (`NEO4J_dbms_ssl_policy_bolt_*`, mounted cert); services keep `bolt://neo4j:7687` and set
>    `NEO4J_TLS_ENABLED=true`. Real/CA cert → `NEO4J_TLS_VERIFY=true`; self-signed → `false`.
> 2. **Reverse-proxy TCP router** (e.g. Traefik TCP router / nginx `stream`) — add a dedicated
>    **TCP** (not HTTP) router with TLS that forwards to `neo4j:7687`; point services at that
>    endpoint with `NEO4J_HOST=<bolt-fqdn>`, `NEO4J_TLS_ENABLED=true`, `NEO4J_TLS_VERIFY=true`
>    (the proxy's managed certificate validates via SNI). The proxy→Neo4j hop is then plaintext on
>    the trusted internal network.

**Security Notes**:

- Use strong passwords in production
- Enable encryption with `bolt+s://` or `bolt+ssc://`
- Consider certificate validation for production
- Rotate credentials regularly

### PostgreSQL Configuration

> These variables describe the stack's own `postgres`. Pointing them at an existing PostgreSQL is not supported yet and the override check refuses it (`gm-deployment-e1uh`; see [Existing PostgreSQL and Valkey](homelab.md#existing-postgresql-and-valkey)). Reuse would also need PgBouncer in session mode only.

| Variable            | Description                                  | Default          | Required |
| ------------------- | -------------------------------------------- | ---------------- | -------- |
| `POSTGRES_HOST`     | PostgreSQL hostname, optionally `host:port`  | `localhost`      | Yes      |
| `POSTGRES_PORT`     | PostgreSQL port (used when not in host)       | `5432`           | No       |
| `POSTGRES_USERNAME` | PostgreSQL username                          | (none)           | Yes      |
| `POSTGRES_PASSWORD` | PostgreSQL password                          | (none)           | Yes      |
| `POSTGRES_DATABASE` | Database name                                | `groovemap` | Yes      |
| `POSTGRES_POOL_MIN_SIZE` | Override the connection-pool minimum for **all** pooled services | per-service default | No |
| `POSTGRES_POOL_MAX_SIZE` | Override the connection-pool maximum for **all** pooled services | per-service default | No |

**Used By**: Tableinator, Dashboard, API, Insights, Brainztableinator, Schema-Init, and the
`embeddings` job (which logs in with its own `EMBEDDING_PIPELINE_POSTGRES_*` credentials)

> **Note**: `POSTGRES_HOST` may include a port, e.g. `pgbouncer:6432` (useful when
> connecting through a connection pooler). An embedded port always takes precedence
> over `POSTGRES_PORT`; if no port is present, `POSTGRES_PORT` (default `5432`) is used.
> IPv6 hosts may be bracketed, e.g. `[::1]:6432`.

> **Connection-pool sizing**: Under a shared pooler in *session* mode (e.g. PgBouncer),
> each client connection pins a dedicated Postgres backend for its lifetime, so the sum
> of every service's pool maximum is the deployment's real backend footprint and must
> stay under the pooler's per-database cap. Each service ships a conservative, workload-
> appropriate default (api 2/8, tableinator 2/12, brainztableinator 2/12, insights 1/4;
> dashboard uses a single connection). The two `POSTGRES_POOL_*` overrides clamp the whole
> fleet uniformly without a code change. See
> [PostgreSQL pool exhaustion analysis](https://github.com/groovemap-music/discogs-sql-loader/blob/main/docs/postgres-pool-exhaustion-analysis.md) for the
> rationale.

**Connection Details**:

- Protocol: PostgreSQL wire protocol
- Default port: 5432 (mapped to 5433 in Docker)
- Connection pool: budget-aware per-service sizing (see note above)
- Retry logic: Exponential backoff (max 5 attempts), behind a circuit breaker with a
  30-second recovery window
- Query timeout: none set globally; individual queries set their own `statement_timeout`
  where needed

**Examples**:

```bash
# Local development
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Docker Compose
POSTGRES_HOST="postgres"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Remote server
POSTGRES_HOST="db.example.com"
POSTGRES_USERNAME="app_user"
POSTGRES_PASSWORD="secure-password"
POSTGRES_DATABASE="groovemap_prod"

# Through a connection pooler (port embedded in host)
POSTGRES_HOST="pgbouncer:6432"
POSTGRES_USERNAME="app_user"
POSTGRES_PASSWORD="secure-password"
POSTGRES_DATABASE="groovemap_prod"
```

**Performance Tuning**:

```bash
# Clamp the connection-pool min/max across the whole fleet (see the
# "Connection-pool sizing" note above — per-service defaults otherwise apply)
POSTGRES_POOL_MIN_SIZE=10
POSTGRES_POOL_MAX_SIZE=20
```

### Valkey Configuration

> These variables describe the stack's own `valkey`. Pointing them at an existing Valkey is not supported yet and the override check refuses it (`gm-deployment-e1uh`; see [Existing PostgreSQL and Valkey](homelab.md#existing-postgresql-and-valkey)). A separate logical database is not a security boundary.

| Variable         | Description                            | Default     | Required                               |
| ---------------- | -------------------------------------- | ----------- | -------------------------------------- |
| `VALKEY_HOST`     | Valkey hostname                        | `localhost` | Yes (API, Insights)            |
| `VALKEY_PORT`     | Valkey port                            | `6379`      | No                                     |
| `VALKEY_PASSWORD` | Valkey password (`requirepass`)        | _(none)_    | No (required if Valkey enforces auth)  |

**Used By**: API, Insights. The Dashboard does not read `VALKEY_*`; its platform-health page
only pings `VALKEY_HEALTH_ADDRESS` (default `valkey:6379`).

**Connection Details**:

- Protocol: RESP, spoken by the `valkey-py` client
- TLS: not supported; the shared client always builds a plain `valkey://` URL, so keep Valkey
  on the internal network
- Default port: 6379 (override with `VALKEY_PORT`)
- Database: 0 (default)
- Authentication: when `VALKEY_PASSWORD` is set it is embedded in the connection URL (`valkey://:<password>@host:port/0`); omitted entirely when unset. Supports the `_FILE` secret convention (`VALKEY_PASSWORD_FILE`).
- Connection pool: Automatic
- Retry logic: Exponential backoff

**Examples**:

```bash
# Local development (no auth)
VALKEY_HOST="localhost"

# Docker Compose
VALKEY_HOST="valkey"

# Authenticated Valkey (e.g. requirepass enabled)
VALKEY_HOST="valkey"
VALKEY_PASSWORD="<password>"
# or, via Docker secret:
VALKEY_PASSWORD_FILE="/run/secrets/valkey_password"
```

**Cache Configuration**:

- Default TTL: 3600 seconds (1 hour)
- Max memory: 512MB in the base file (change through a reviewed Compose override)
- Eviction policy: `noeviction` (`--maxmemory-policy noeviction`). Valkey never
  drops a key to make room; once `maxmemory` is reached, commands that add
  memory (`SET`, `SETEX`, `INCR`) fail with `OOM command not allowed when used
  memory > 'maxmemory'`, while reads, `DEL`, `GETDEL` and `EXPIRE` keep working.
  Valkey also holds security state that exists nowhere else, namely
  `revoked:jti:*` (logout and admin-logout token revocation) and
  `password_changed:*` (session invalidation after a password reset or change).
  An evicting policy such as `allkeys-lru` could silently drop those keys and
  make a revoked token valid again, so the policy is pinned by
  `tests/deploy/test_docker_compose_prod.py`. The trade-off is that a full
  Valkey fails writes loudly instead of degrading quietly; watch
  `ValkeyMemoryHigh` (see [observability](observability.md)) and raise
  `maxmemory` through a reviewed Compose override before it fills.
- Persistence: append-only file enabled (`--appendonly yes`) on the
  `valkey_data` volume; Valkey is used as a cache plus security state, and
  container replacement does not discard the volume automatically.

## JWT Configuration

| Variable                       | Description                                                       | Default | Required  |
| ------------------------------ | ----------------------------------------------------------------- | ------- | --------- |
| `JWT_SECRET_KEY`               | HMAC-SHA256 signing secret                                        | (none)  | Yes (API) |
| `JWT_EXPIRE_MINUTES`           | Token lifetime in minutes (`docker-compose.yml` sets `60`)        | `30`    | No        |
| `JWT_ALGORITHM`                | Signing algorithm; any value other than `HS256` stops the API starting | `HS256` | No   |
| `DISCOGS_USER_AGENT`           | User-Agent for Discogs API requests                               | `GrooveMap-catalog-api/1.0 +https://github.com/groovemap-music/catalog-api` | No |
| `DISCOGS_OAUTH_CALLBACK_URL`   | Public callback URL Discogs redirects to after the user authorizes the app. When unset, the OAuth flow falls back to the out-of-band (OOB) mode where the user copy/pastes a verifier code into the app. When set, the value must exactly match the **Callback URL** field on your Discogs developer app settings page and point at `oauth-discogs-callback.html` on the Explore frontend (e.g. `https://your-host/oauth-discogs-callback.html`). | (none)  | No        |
| `APP_BASE_URL`                 | Public origin of the user-facing Explore frontend, used to build absolute links in outbound email (currently the password reset link). Must be reachable from a recipient's mail client — a relative or internal-only URL produces an unclickable link. Any trailing slash is stripped. | `http://localhost:8006` | Yes in production |

**Used By**: API

**`APP_BASE_URL` vs `API_BASE_URL`**: these are different addresses and are easy to confuse.
`API_BASE_URL` is the internal service-to-service address of the API (e.g. `http://api:8004`),
used by Explore, Insights, and the MCP server. `APP_BASE_URL` is the **public** origin a real
user's browser reaches — the one that must appear in email. In production, leaving
`APP_BASE_URL` at its localhost default means every password reset link mails out pointing at
the recipient's own machine.

**JWT Details**:

- Algorithm: HS256 (HMAC-SHA256)
- Token format: Standard JWT (`header.body.signature`, base64url-encoded)
- Payload claims: `sub` (user UUID), `email`, `iat`, `exp`, `jti` (the token ID that logout revokes)

**Security Notes**:

- Use a cryptographically random secret of at least 32 bytes in production
- Rotate the secret to invalidate all existing tokens
- Never log or expose `JWT_SECRET_KEY`
- In production, supply via `JWT_SECRET_KEY_FILE` pointing to a Docker secret file — see [Production Secrets](#production-secrets)

**Examples**:

```bash
# Generate a secure random secret (Linux/macOS)
JWT_SECRET_KEY=$(openssl rand -hex 32)

# Set token lifetime
JWT_EXPIRE_MINUTES=30     # 30 minutes (default)
JWT_EXPIRE_MINUTES=60     # 1 hour
JWT_EXPIRE_MINUTES=1440   # 24 hours

# Discogs User-Agent (optional; overrides the catalog-api default)
DISCOGS_USER_AGENT="GrooveMap/1.0 +https://groovemap.music"

# Optional — public Discogs OAuth callback URL. When set, end users no longer
# have to copy/paste a verifier code; Discogs redirects directly back to the
# app. The URL must also be registered as the "Callback URL" on the Discogs
# developer app settings page.
DISCOGS_OAUTH_CALLBACK_URL="https://your-host/oauth-discogs-callback.html"
```

## Logging Configuration

| Variable    | Description       | Default | Valid Values                                    |
| ----------- | ----------------- | ------- | ----------------------------------------------- |
| `LOG_LEVEL` | Logging verbosity | `INFO`  | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

**Used By**: All services

**Log Level Behavior**:

| Level      | Output                                    | Use Case               |
| ---------- | ----------------------------------------- | ---------------------- |
| `DEBUG`    | All logs + query details + internal state | Development, debugging |
| `INFO`     | Normal operation logs                     | Production (default)   |
| `WARNING`  | Warnings and errors                       | Production (minimal)   |
| `ERROR`    | Errors only                               | Production (alerts)    |
| `CRITICAL` | Critical errors only                      | Production (severe)    |

**Examples**:

```bash
# Development - see everything
LOG_LEVEL=DEBUG

# Production - normal operation
LOG_LEVEL=INFO

# Production - minimal logging
LOG_LEVEL=WARNING
```

**Debug Level Features**:

- Neo4j query logging with parameters
- PostgreSQL query logging
- RabbitMQ message details
- Cache hit/miss statistics
- Internal state transitions

See [Logging Guide](https://github.com/groovemap-music/catalog-api/blob/main/docs/logging-guide.md) for detailed logging information.

## Consumer Management

| Variable                | Description                             | Default       | Range     |
| ----------------------- | --------------------------------------- | ------------- | --------- |
| `CONSUMER_CANCEL_DELAY` | Seconds before canceling idle consumers | `300` (5 min) | 60-3600   |
| `QUEUE_CHECK_INTERVAL`  | Seconds between queue checks when idle  | `3600` (1 hr) | 300-86400 |
| `STUCK_CHECK_INTERVAL`  | Seconds between stuck-state checks      | `30`          | 5-300     |
| `STARTUP_IDLE_TIMEOUT`  | Seconds after startup with no messages before entering idle mode | `30` | — |
| `IDLE_LOG_INTERVAL`     | Seconds between idle status log lines   | `300` (5 min) | —         |
| `STARTUP_DELAY`         | Seconds to wait before starting (Compose: `15` Graphinator, `20` Tableinator and Brainzgraphinator, `25` Brainztableinator). Read only by these consumers; `api` and `insights` do not read it and are ordered by `depends_on` health conditions | `5` | — |
| `HEALTH_PORT`           | Internal health port                    | `8001` graph consumers, `8002` table consumers | — |

**Used By**: Graphinator, Tableinator, Brainzgraphinator, Brainztableinator

**Purpose**: Smart resource management for RabbitMQ connections

**How It Works**:

1. Service processes messages from all queues
1. When all queues are empty for `CONSUMER_CANCEL_DELAY` seconds:
   - Close RabbitMQ connections
   - Stop progress logging
   - Enter "waiting" mode
1. Every `QUEUE_CHECK_INTERVAL` seconds:
   - Briefly connect to RabbitMQ
   - Check all queues for new messages
   - If messages found, restart consumers
1. When new messages detected:
   - Reconnect to RabbitMQ
   - Resume all consumers
   - Resume progress logging

**Configuration Examples**:

```bash
# Aggressive resource saving (testing)
CONSUMER_CANCEL_DELAY=60     # 1 minute
QUEUE_CHECK_INTERVAL=300     # 5 minutes

# Balanced (default)
CONSUMER_CANCEL_DELAY=300    # 5 minutes
QUEUE_CHECK_INTERVAL=3600    # 1 hour

# Conservative (always connected)
CONSUMER_CANCEL_DELAY=3600   # 1 hour
QUEUE_CHECK_INTERVAL=300     # 5 minutes (doesn't matter if rarely triggered)
```

See [Consumer Cancellation](https://github.com/groovemap-music/discogs-graph-enricher/blob/main/docs/consumer-cancellation.md) for details.

## Batch Processing Configuration

| Variable                        | Description                              | Code Default | Docker Compose | Range      |
| ------------------------------- | ---------------------------------------- | ------------ | -------------- | ---------- |
| `NEO4J_BATCH_MODE`              | Enable batch processing for Neo4j writes | `true`       | `true`         | true/false |
| `NEO4J_BATCH_SIZE`              | Records per batch for Neo4j              | `100`        | `500`          | 10-1000    |
| `NEO4J_BATCH_FLUSH_INTERVAL`    | Seconds between automatic flushes        | `5.0`        | `2.0`          | 1.0-60.0   |
| `POSTGRES_BATCH_MODE`           | Enable batch processing for PostgreSQL   | `true`       | `true`         | true/false |
| `POSTGRES_BATCH_SIZE`           | Records per batch for PostgreSQL         | `100`        | `500`          | 10-1000    |
| `POSTGRES_BATCH_FLUSH_INTERVAL` | Seconds between automatic flushes        | `5.0`        | `2.0`          | 1.0-60.0   |

**Used By**: Graphinator (Neo4j), Tableinator (PostgreSQL), Brainzgraphinator (Neo4j). Brainztableinator does **not** use batch flushing — it commits one PostgreSQL transaction per message and instead couples RabbitMQ prefetch to its connection-pool size (channel-global QoS = pool max); the `POSTGRES_BATCH_*` variables have no effect on it.

**Purpose**: Improve write performance by batching multiple database operations

**How Batch Processing Works**:

1. Messages are collected into batches instead of being processed individually
1. When batch reaches `BATCH_SIZE` or `BATCH_FLUSH_INTERVAL` expires:
   - All records in batch are written in a single database operation
   - Message acknowledgments are sent after successful write
1. On service shutdown:
   - All pending batches are flushed automatically
   - No data loss occurs during graceful shutdown

**Performance Impact**:

- **Throughput**: 3-5x improvement in write operations per second
- **Database Load**: Fewer transactions, reduced connection overhead
- **Latency**: Slight increase (up to `BATCH_FLUSH_INTERVAL` seconds)
- **Memory**: Increased by approximately `BATCH_SIZE * record_size`

**Configuration Examples**:

```bash
# High throughput (recommended for initial data load)
NEO4J_BATCH_MODE=true
NEO4J_BATCH_SIZE=500
NEO4J_BATCH_FLUSH_INTERVAL=10.0

# Low latency (real-time updates)
NEO4J_BATCH_MODE=true
NEO4J_BATCH_SIZE=10
NEO4J_BATCH_FLUSH_INTERVAL=1.0

# Disabled (per-message processing)
NEO4J_BATCH_MODE=false
# BATCH_SIZE and FLUSH_INTERVAL ignored when disabled
```

**Best Practices**:

- **Initial Load**: Use larger batch sizes (500-1000) for faster initial data loading
- **Real-time Updates**: Use smaller batches (10-50) for lower latency
- **Memory Constrained**: Reduce batch size if memory usage is a concern
- **High Throughput**: Increase flush interval to accumulate more records
- **Testing**: Disable batch mode to debug individual record processing

**Monitoring**:

Batch processing logs provide visibility into performance:

```
🚀 Batch processing enabled (batch_size=100, flush_interval=5.0)
📦 Flushing batch for artists (size=100)
✅ Batch flushed successfully (artists: 100 records in 0.45s)
```

See [Performance Guide](performance-guide.md) for detailed optimization strategies.

## Dashboard Configuration

| Variable                | Description                                  | Default           | Required |
| ------------------------ | --------------------------------------------- | ----------------- | -------- |
| `RABBITMQ_PROMETHEUS_URL` | Base URL of the broker's `rabbitmq_prometheus` endpoint; queue stats are read from `/metrics/detailed` | `http://rabbitmq:15692` | No       |
| `CORS_ORIGINS`                 | Comma-separated list of allowed CORS origins | (none — disabled) | No       |
| `CACHE_WARMING_ENABLED`        | Pre-warm cache on startup                    | `true`            | No       |
| `CACHE_WEBHOOK_SECRET`         | Secret for cache invalidation webhooks       | (none — disabled) | No       |
| `API_HOST`                     | API service hostname for admin proxy         | `api`             | No       |
| `API_PORT`                     | API service port for admin proxy             | `8004`            | No       |
| `VICTORIA_METRICS_URL`         | VictoriaMetrics base URL for the history tabs and its health probe | `http://victoria-metrics:8428` | No |
| `GRAFANA_API_URL`              | In-network Grafana the server asks for firing alerts | falls back to `GRAFANA_URL` | No |
| `GRAFANA_URL`                  | Browser-facing Grafana base for deep links; Compose default `http://localhost:3000` | (none — no links) | No |
| `TRACES_URL`                   | Browser-facing base for trace links          | `GRAFANA_URL`     | No       |
| `GRAFANA_TOKEN_FILE`           | File holding a Grafana Viewer token for alerts; the production overlay mounts `grafana_viewer_token` | (none — alerts off) | No |
| `SESSION_COOKIE_SECURE`        | Secure attribute on the session cookie; see [below](#console-session-cookie-and-https) | `true` | No |
| `SESSION_CACHE_SECONDS`        | Seconds a validated session is cached (capped at 30) | `30`      | No       |
| `VICTORIA_TRACES_URL`, `OTEL_COLLECTOR_HEALTH_URL`, `VALKEY_HEALTH_ADDRESS` | Platform-health probe targets | `http://victoria-traces:10428`, `http://otel-collector:13133`, `valkey:6379` | No |

**Used By**: Dashboard only (for `CACHE_WARMING_ENABLED`, `CACHE_WEBHOOK_SECRET`); `CORS_ORIGINS` is also supported by the API and Explore — see the [API](#api) and [Explore](#explore) sections below. `API_HOST` / `API_PORT` are used by the admin proxy router to forward authenticated admin requests to the API service.

**Notes**:

- The dashboard reads queue depth, consumers, and publish/ack rates from the broker's unauthenticated `rabbitmq_prometheus` endpoint on the internal network. It holds no RabbitMQ credential and opens no AMQP or management-API connection, so the production overlay mounts no RabbitMQ secrets into it
- `CORS_ORIGINS` is optional; omit it to restrict cross-origin access
- `CACHE_WEBHOOK_SECRET` enables an authenticated endpoint to invalidate cached queries
- `API_HOST` / `API_PORT` configure the admin proxy — the dashboard forwards admin panel requests (login, extraction trigger, DLQ purge) to the API service at `http://{API_HOST}:{API_PORT}`

## Python Version

| Variable         | Description               | Default | Used By       |
| ---------------- | ------------------------- | ------- | ------------- |
| `PYTHON_VERSION` | Python version for builds | `3.14`  | Docker, CI/CD |

**Used By**: Source-repository build systems, not this repository's Compose
services. Deployment consumes already-built images.

**Purpose**: Compatibility metadata retained in `.env.example`; it does not
select the Python interpreter inside a promoted image.

**Notes**:

- Supported version line: Python 3.14
- Managed runtime pins use Python 3.14.7
- Older and newer minor versions are not supported

## Service-Specific Settings

### API

```bash
# Required
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"
JWT_SECRET_KEY="your-secret-key-here"

# Valkey (default: localhost)
VALKEY_HOST="localhost"

# Optional — overrides the catalog-api default User-Agent
DISCOGS_USER_AGENT="GrooveMap/1.0 +https://groovemap.music"

# Optional — HKDF master encryption key (derives OAuth + TOTP encryption keys)
# Required for TOTP 2FA. Without it, OAuth tokens are stored unencrypted and 2FA is disabled.
# Generate with: uv run python -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
# In production, supply via ENCRYPTION_MASTER_KEY_FILE (Docker secret)
ENCRYPTION_MASTER_KEY="your-master-key-here"

# Shared secret gating the internal /api/internal/insights/* router.
# These endpoints are mounted on the public app and reachable through the explore proxy,
# so the /api/internal prefix alone provides NO isolation. Callers must present this value
# as the X-Internal-Secret header; the insights service is configured with the same value.
# When unset, the router fails closed (rejects every caller). Supply via
# INSIGHTS_INTERNAL_SECRET_FILE (Docker secret) in production.
# Full trust-model rationale: architecture.md § "Why insights needs a shared secret".
INSIGHTS_INTERNAL_SECRET="change-me-in-production"

# Optional — Resend transactional email for password reset notifications
# When not set, password reset links are logged to stdout instead of emailed.
# Get your API key from https://resend.com/api-keys
# RESEND_API_KEY="your-resend-api-key"
# RESEND_SENDER_EMAIL="noreply@yourdomain.com"   # Must be verified in Resend
# RESEND_SENDER_NAME="GrooveMap"

# Optional — CORS origins (comma-separated; omit to disable CORS)
CORS_ORIGINS="http://localhost:8003,http://localhost:8006"

# Optional — snapshot settings
SNAPSHOT_TTL_DAYS=28     # Snapshot expiry in days (default: 28)
SNAPSHOT_MAX_NODES=100   # Max nodes per snapshot (default: 100)

# Optional
JWT_EXPIRE_MINUTES=1440
LOG_LEVEL=INFO
```

Published endpoints: <http://localhost:8004> (service) and
<http://localhost:8005/health> (health probe).

After configuring Resend, follow the
[transactional email live-verification runbook](transactional-email-verification.md).
The final send and password change require explicit operator approval; the
configuration examples above do not prove live delivery.

**Notes**: After startup, set Discogs app credentials using the `catalog-setup` CLI bundled in the API container:

```bash
docker compose exec api catalog-setup \
  --consumer-key YOUR_CONSUMER_KEY \
  --consumer-secret YOUR_CONSUMER_SECRET

# Verify (values are masked)
docker compose exec api catalog-setup --show
```

See the [API README](https://github.com/groovemap-music/catalog-api/blob/main/api/README.md) for full setup instructions.

### Schema-Init

```bash
# Required
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Optional
LOG_LEVEL=INFO
```

**Notes**: Schema-init is a one-shot initializer — it exits 0 on success and 1 on failure. It has no health check port. In Docker Compose, all dependent services use `condition: service_completed_successfully`. Re-running on an already-initialized database is a no-op (all DDL uses `IF NOT EXISTS`).

### Extractor

The extractor runs as two Docker services: `extractor-discogs` and `extractor-musicbrainz`.

```bash
# Required (Discogs mode)
DISCOGS_ROOT="/discogs-data"

# Required (MusicBrainz mode)
MUSICBRAINZ_ROOT="/musicbrainz-data"

# RabbitMQ
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional
PERIODIC_CHECK_DAYS=5            # Days between update checks (code default: 15, docker-compose: 5 Discogs / 3 MusicBrainz)
BATCH_SIZE=100                   # Records per AMQP publish batch (default: 100)
MAX_WORKERS=4                    # Concurrent processing workers (default: number of CPU cores)
FORCE_REPROCESS=false            # Force reprocess even if already extracted (default: false)
QUEUE_SIZE=5000                  # Internal pipeline channel capacity (default: 5000)
PROGRESS_LOG_INTERVAL=1000       # Records between progress log lines (default: 1000)
STATE_SAVE_INTERVAL=5000         # Records between state-marker saves (default: 5000)
FAILURE_COOLDOWN_SECS=600        # Pause after a failed run before exiting (default: 600)
HEALTH_PORT=8000                 # Internal health port (default: 8000)
LOG_LEVEL=INFO

# Discogs only — promoted data-quality rules mounted by Compose
DATA_QUALITY_RULES=/config/extraction-rules.yaml

# MusicBrainz only
MUSICBRAINZ_DUMP_URL=https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/  # default
```

Internal health port: 8000 on each extractor. It is not published to the host;
use `docker compose ps` for status.

### Graphinator

```bash
# Required
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"

# RabbitMQ
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional - Consumer Management
CONSUMER_CANCEL_DELAY=300
QUEUE_CHECK_INTERVAL=3600
STUCK_CHECK_INTERVAL=30    # Seconds between stuck-state checks

# Optional - Batch Processing (enabled by default)
NEO4J_BATCH_MODE=true
NEO4J_BATCH_SIZE=500
NEO4J_BATCH_FLUSH_INTERVAL=2.0

# Optional - Startup
STARTUP_DELAY=15                 # Seconds to wait before starting (code default: 5, docker-compose: 15)

# Optional - Logging
LOG_LEVEL=INFO
```

Internal health port: 8001. It is not published to the host; use
`docker compose ps` for status.

### Tableinator

```bash
# Required
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# RabbitMQ
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional - Consumer Management
CONSUMER_CANCEL_DELAY=300
QUEUE_CHECK_INTERVAL=3600
STUCK_CHECK_INTERVAL=30    # Seconds between stuck-state checks

# Optional - Batch Processing (enabled by default)
POSTGRES_BATCH_MODE=true
POSTGRES_BATCH_SIZE=500
POSTGRES_BATCH_FLUSH_INTERVAL=2.0

# Optional - Startup
STARTUP_DELAY=20                 # Seconds to wait before starting (code default: 5, docker-compose: 20)

# Optional - Logging
LOG_LEVEL=INFO
```

Internal health port: 8002. It is not published to the host; use
`docker compose ps` for status.

### Explore

```bash
# Required
API_BASE_URL="http://api:8004"   # URL of the API service to proxy requests to

# Optional
CORS_ORIGINS="http://localhost:3000,http://localhost:8003"  # comma-separated origins
LOG_LEVEL=INFO
```

Published health endpoint: <http://localhost:8007/health>.

### Dashboard

```bash
# Required
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Broker metrics (queue depth and publish/ack rates)
RABBITMQ_PROMETHEUS_URL=http://rabbitmq:15692  # default: http://rabbitmq:15692

# History, alerts and deep links (Compose values)
VICTORIA_METRICS_URL=http://victoria-metrics:8428
GRAFANA_API_URL=http://grafana:3000
GRAFANA_URL=http://localhost:3000   # set to the public Grafana URL outside local dev
TRACES_URL=http://localhost:3000    # defaults to GRAFANA_URL

# Optional - Session cookie
SESSION_COOKIE_SECURE=false        # true/1/yes or false/0/no; anything else stops the console starting

# Optional - CORS (off unless set; then GET-only and without credentials)
CORS_ORIGINS="http://localhost:8003,http://localhost:8006"  # comma-separated origins

# Optional - Cache
CACHE_WARMING_ENABLED=true         # Pre-warm cache on startup
CACHE_WEBHOOK_SECRET=              # Secret for cache invalidation webhooks

# Optional - Logging
LOG_LEVEL=INFO
```

Published health endpoint: <http://localhost:8003/health>.

#### Console session cookie and HTTPS

The console signs operators in with an HttpOnly session cookie
(`__Host-groovemap_console_session`) that is **Secure by default**, whatever scheme the console
itself sees. Only an explicit `SESSION_COOKIE_SECURE=false` turns Secure off.

- **Development:** `docker-compose.yml` sets `SESSION_COOKIE_SECURE=false` on `dashboard`, because
  the dev stack serves the console over plain http on port 8003. Without it, Safari (on any
  host) and every browser on `http://<host>:8003` drop the Secure cookie and sign-in silently
  returns to the login form. Chromium and Firefox accept a Secure cookie on `http://localhost`.
- **Production:** `docker-compose.prod.yml` resets the base file's value (`!reset null`; Compose
  merges `environment` key by key, so the opt-out would otherwise carry over), so the Secure default applies. The
  production console should be reached over HTTPS. Reached over plain http away from
  localhost, the browser drops the cookie and sign-in silently returns to the login form. The
  fix is a TLS endpoint in front (below).
- **Production on a trusted LAN without TLS:** set `GROOVEMAP_ALLOW_INSECURE_CONSOLE=true` in
  `.env`, then recreate the console
  (`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps dashboard`,
  with your project name and override file if you use them). The overlay then loads
  `config/console-insecure.true.env`, which sets `SESSION_COOKIE_SECURE=false` for `dashboard`
  only. Unset, or any value but exactly `true`, leaves today's Secure default and an identical
  Compose render. With the opt-in the session cookie crosses the network unencrypted, so anyone
  on the path can read it: use it only where you trust every device on the network.
- **What `just deploy` does:** after the stack is up and before it prints the endpoints, a
  production run explains this and the two options when `APP_BASE_URL` (the TLS heuristic) or
  the console's own published URL (the one in the endpoints block, `DASHBOARD_HOST_PORT`) is
  plain `http://` on a host other than localhost. It names the console URL, for example
  "GrooveMap is configured for plain http (APP_BASE_URL=http://nas:8006), so the console at
  http://nas:8003 is reached without TLS". With a TLS proxy, set `https://` in `APP_BASE_URL`
  and bind the raw ports to `127.0.0.1` (`WEB_BIND_ADDRESS`) and it stays quiet. On a terminal
  it offers to set the opt-in and recreate only `dashboard`, defaulting to no; otherwise it only
  prints the guidance. It is also silent with the opt-in set and for `--dev`.

The TLS endpoint in front of the production console must:

- pass `Cookie` and `Set-Cookie` unchanged (no `SESSION_COOKIE_SECURE` is needed behind it);
- pass `X-GrooveMap-Console`, `Sec-Fetch-Site`, `Origin` and `Authorization`;
- keep the browser's `Host` header;
- serve the console at the root of its own host name (no path prefix);
- forward `/ws` with `Upgrade` (WebSocket);
- not log `/ws` query strings or `Cookie`/`Set-Cookie` headers.

Credentialed CORS is gone: nothing in this repository sets `CORS_ORIGINS` for `dashboard`, and
Grafana only navigates to the console.

### Insights

```bash
# Required
API_BASE_URL="http://api:8004"       # URL of the API service (fetches raw query data over HTTP)
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Optional - Valkey Caching
VALKEY_HOST="localhost"                        # Valkey hostname for result caching (default: localhost)

# Internal API authentication — MUST match the API service's INSIGHTS_INTERNAL_SECRET.
# The API's /api/internal/insights/* router is mounted on the public app and reachable
# through the explore proxy, so it is gated by this shared secret (sent as the
# X-Internal-Secret header). When unset on the API, the router fails closed (rejects all
# callers); when unset here, the insights service cannot fetch its computation data.
# Full trust-model rationale: architecture.md § "Why insights needs a shared secret".
INSIGHTS_INTERNAL_SECRET="change-me-in-production"

# Optional - Scheduler
INSIGHTS_SCHEDULE_HOURS=24                    # Computation interval in hours (default: 24)

# Optional - Milestones
INSIGHTS_MILESTONE_YEARS="25,30,40,50,75,100" # Anniversary years to highlight (default: 25,30,40,50,75,100)

# Optional - Logging
LOG_LEVEL=INFO
```

`insights` has no startup delay. Compose orders it with `depends_on` health
conditions.

Internal health port: 8009. It is not published to the host; use
`docker compose ps` for status.

**Notes**: The Insights service uses Valkey for caching computed results (cache-aside pattern). The cache TTL matches the `INSIGHTS_SCHEDULE_HOURS` interval and is invalidated after each computation run. If Valkey is unavailable, the service operates without caching.

### Embeddings job

The one-shot `embeddings` service (Compose profile `jobs`) runs the analytics-engine image's
`analytics-engine-embeddings` entry point for the monthly FastRP recompute. Run it with
`just embeddings-run`; see [Maintenance](maintenance.md#monthly-embedding-refresh).

```bash
# Required — no default; they change every month
SOURCE_DUMP_ID=discogs-2026-09                 # the dump this run embeds
SOURCE_DUMP_DATE=2026-09-01                    # that dump's release date, YYYY-MM-DD

# The pipeline's own least-privilege login (a member of database-schema's
# NOLOGIN embedding_pipeline role), never POSTGRES_USERNAME
EMBEDDING_PIPELINE_POSTGRES_USERNAME=embedding_pipeline  # Compose default
EMBEDDING_PIPELINE_POSTGRES_PASSWORD=groovemap           # Compose default; a file secret in production

# Optional
ENABLE_SIMILAR_ARTISTS=false                   # exactly true or false; true also precomputes similar artists
```

Provision the login once per stack with `just embeddings-provision-login`.

### Brainzgraphinator

```bash
# Required
NEO4J_HOST="localhost"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="groovemap"

# RabbitMQ
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional - Consumer Management
CONSUMER_CANCEL_DELAY=300
QUEUE_CHECK_INTERVAL=3600

# Optional - Batch Processing (enabled by default)
NEO4J_BATCH_MODE=true
NEO4J_BATCH_SIZE=500
NEO4J_BATCH_FLUSH_INTERVAL=2.0

# Optional - Startup
STARTUP_DELAY=20                 # Seconds to wait before starting (code default: 5, docker-compose: 20)

# Optional - Logging
LOG_LEVEL=INFO
```

Internal health port: 8001 (`HEALTH_PORT`). It is not published to the host; use
`docker compose ps` for status.

**Notes**: Brainzgraphinator enriches existing Neo4j nodes with MusicBrainz metadata (properties, relationships, cross-references). It skips entities without Discogs matches. Consumes from the `groovemap-musicbrainz-{artists,labels,release-groups,releases}` exchanges (prefix set by `MUSICBRAINZ_EXCHANGE_PREFIX`).

### Brainztableinator

```bash
# Required
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# RabbitMQ
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional - Consumer Management
CONSUMER_CANCEL_DELAY=300
QUEUE_CHECK_INTERVAL=3600

# Optional - PostgreSQL pool sizing (also bounds RabbitMQ prefetch — see notes below)
POSTGRES_POOL_MIN_SIZE=2         # code default: 2
POSTGRES_POOL_MAX_SIZE=12        # code default: 12

# Optional - Startup
STARTUP_DELAY=25                 # Seconds to wait before starting (code default: 5, docker-compose: 25)

# Optional - Logging
LOG_LEVEL=INFO
```

Internal health port: 8002 (`HEALTH_PORT`). It is not published to the host; use
`docker compose ps` for status.

**Notes**: Brainztableinator stores all MusicBrainz data in the `musicbrainz` PostgreSQL schema — including entities without Discogs matches — with relationships and external links. Consumes from the `groovemap-musicbrainz-{artists,labels,release-groups,releases}` exchanges (prefix set by `MUSICBRAINZ_EXCHANGE_PREFIX`). Unlike Graphinator/Tableinator/Brainzgraphinator, it does **not** batch writes — it commits one PostgreSQL transaction per message and instead sets RabbitMQ prefetch (channel-global QoS) equal to its connection-pool maximum, so in-flight message handlers never exceed pool capacity.

### MCP Server

```bash
API_BASE_URL="http://api:8004"   # Base URL for the GrooveMap API (default: http://localhost:8004)

# Optional — delegated Catalog API app token; without it the delegated tools decline
GROOVEMAP_CATALOG_APP_TOKEN="<app-token>"
```

**Notes**: The MCP server is not part of this Compose stack. It has no direct database dependencies — all data is fetched via the API service over HTTP. It supports `stdio` (default when run directly, for local use with Claude Desktop/Cursor/Zed) and `streamable-http` (the image's default command, for hosted deployments) transports.

## Environment Templates

### Development example

The repository ships `.env.example`; it does not ship `.env.development`.
Start from the former so all eleven required image variables remain present.
The fragment below illustrates the variables a source-owned process reads when
it runs directly outside Compose. It is not a complete Compose input, and inside
the stack these values change only through a reviewed Compose override (see
[Configuration Methods](#configuration-methods)).

```bash
# RabbitMQ (built from components)
RABBITMQ_HOST=localhost
RABBITMQ_USERNAME=groovemap
RABBITMQ_PASSWORD=groovemap

# Neo4j
NEO4J_HOST=localhost
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=development

# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_USERNAME=postgres
POSTGRES_PASSWORD=development
POSTGRES_DATABASE=groovemap_dev

# Valkey
VALKEY_HOST=localhost

# JWT (API)
JWT_SECRET_KEY=dev-secret-key-not-for-production
JWT_EXPIRE_MINUTES=1440
DISCOGS_USER_AGENT="GrooveMap/1.0-dev +https://groovemap.music"

# Data
DISCOGS_ROOT=/tmp/discogs-data-dev
PERIODIC_CHECK_DAYS=1    # minimum; checks cannot be disabled

# Logging
LOG_LEVEL=DEBUG

# Consumer Management (aggressive for testing)
CONSUMER_CANCEL_DELAY=60
QUEUE_CHECK_INTERVAL=300
```

### Production Secrets

In production, all sensitive credentials are delivered as Docker Compose runtime secrets — never as plain environment variables. The `docker-compose.prod.yml` overlay wires everything up automatically.

**Step 1 — Bootstrap secrets** (run once; safe to re-run, skips existing files):

```bash
bash scripts/create-secrets.sh
```

This creates `secrets/` with these files (all `chmod 600`, directory `chmod 700`):

| File | Value or generation method |
| --- | --- |
| `resend_api_key.txt` | Optional Resend API key |
| `nlq_api_key.txt` | Optional NLQ provider API key; empty unless enabled |
| `encryption_master_key.txt` | URL-safe base64 of 32 random bytes |
| `jwt_secret_key.txt` | `openssl rand -hex 32` |
| `neo4j_password.txt` | 36 random `[A-Za-z0-9]` characters |
| `postgres_password.txt` | 36 random `[A-Za-z0-9]` characters |
| `postgres_username.txt` | `groovemap` |
| `rabbitmq_password.txt` | 36 random `[A-Za-z0-9]` characters |
| `rabbitmq_username.txt` | `groovemap` |
| `valkey_password.txt` | 36 random `[A-Za-z0-9]` characters |
| `valkey_exporter_passwords.json` | Derived from `valkey_password.txt` by `scripts/create-secrets.sh` on every run (JSON map of Redis URI to password, for `valkey-exporter`) |
| `insights_internal_secret.txt` | `openssl rand -hex 32`; shared by API and insights |
| `grafana_admin_password.txt` | 36 random `[A-Za-z0-9]` characters; Grafana admin login |
| `grafana_viewer_token.txt` | Optional Grafana Viewer token for the console's alerts (`GRAFANA_VIEWER_TOKEN`); empty unless supplied |
| `embedding_pipeline_username.txt` | `embedding_pipeline` |
| `embedding_pipeline_password.txt` | 36 random `[A-Za-z0-9]` characters; grant the role with `just embeddings-provision-login` |

Generated passwords avoid `/` and `+`, which Neo4j rejected and which would need
percent-encoding in connection URLs.

See `secrets.example/` for reference placeholders and generation commands.

**Step 2 — Set the non-secret production interpolation inputs** in the target
environment's untracked `.env`, next to the image digests. The values are
non-secret, but this repository still forbids committing `.env` or rendered
environment configuration:

```bash
# Required by the production overlay: the public Explore origin used in email links
APP_BASE_URL=https://groovemap.example.com

# Browser-facing Grafana for the console's deep links (TRACES_URL follows it)
GRAFANA_URL=https://grafana.example.com

# Optional email sender (defaults shown)
RESEND_SENDER_EMAIL=noreply@groovemap.music
RESEND_SENDER_NAME=GrooveMap

# Optional Neo4j sizing (defaults shown)
NEO4J_HEAP_SIZE=8g
NEO4J_PAGECACHE_SIZE=16g
NEO4J_MEMORY_LIMIT=48G
NEO4J_TRANSACTION_MEMORY_MAX=8G
```

Hosts, data paths, log levels, and consumer timings are literal Compose values,
not interpolation inputs; change them only with a reviewed Compose override.

**Step 3 — after reviewing the render and obtaining operator approval, start
with the production overlay**:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Credentials are mounted at `/run/secrets/<name>` inside each container and read automatically. See [Docker Security](docker-security.md) for the full secrets table and Neo4j entrypoint details.

## Security Best Practices

### Password Management

**Never commit credentials**:

```bash
# ❌ BAD — hardcoded password in any committed file
NEO4J_PASSWORD=supersecret

# ✅ GOOD — Docker Compose runtime secret (production)
# secrets/neo4j_password.txt contains the value, mounted at /run/secrets/neo4j_password
# docker-compose.prod.yml wires it up automatically via get_secret()
```

For production deployments, use `docker-compose.prod.yml` with `scripts/create-secrets.sh`. For other platforms:

- **Kubernetes**: Kubernetes Secrets or an external secrets operator
- **HashiCorp Vault**: Vault Agent Injector or the Vault CSI provider
- **AWS**: Secrets Manager with the AWS Secrets and Configuration Provider
- **Azure**: Azure Key Vault with the CSI Secrets Store driver

### Connection Encryption

Enable encryption for production:

```bash
# Neo4j with TLS (encrypted Bolt via NEO4J_TLS_ENABLED — see "Enabling TLS
# for Neo4j" above; for Aura, pass a full "neo4j+s://host" URI instead)
NEO4J_HOST=neo4j.example.com
NEO4J_TLS_ENABLED=true

# PostgreSQL hostname
POSTGRES_HOST=postgres.example.com
```

The shared Valkey client has no TLS option, so keep Valkey on the internal
network rather than across an untrusted one.

### Access Control

Use least-privilege principles:

```bash
# ❌ BAD - using admin credentials
NEO4J_USERNAME=admin
POSTGRES_USERNAME=postgres

# ✅ GOOD - dedicated service accounts
NEO4J_USERNAME=groovemap_app
POSTGRES_USERNAME=groovemap_app
```

## Validation and Testing

### Health Checks

Verify all services are configured correctly:

```bash
docker compose ps
curl --fail http://localhost:8003/health  # Dashboard
curl --fail http://localhost:8005/health  # API health port
curl --fail http://localhost:8007/health  # Explore health port
```

Only those three application health probes are published (Grafana's
`/api/health` on port 3000 and VictoriaMetrics' `/health` on port 8428 are also
reachable in the base stack). Compose runs the remaining health
checks inside containers; some are HTTP, while databases and other
infrastructure use their native commands. Inspect the exact commands with
`docker compose config` rather than assuming every check returns the same JSON.

## Troubleshooting

### Common Configuration Issues

**Connection Refused Errors**:

- Check host and port are correct
- Verify service is running
- Check firewall rules
- Wait for service startup (databases can take 30-60s)

**Authentication Failures**:

- Verify username and password
- Check password special characters are properly escaped
- Ensure credentials match database configuration

**Cache Directory Errors**:

- Verify directory exists
- Check write permissions (UID 1000 for Docker)
- Ensure sufficient disk space

**Environment Variables Not Loading**:

- Check `.env` file is in correct location
- Verify no syntax errors in `.env`
- Recreate the affected containers after an approved change (a restart keeps
  the old environment); this repository never builds images
- Remember that `.env` only feeds `${NAME}` interpolation, not literal Compose values

See [Troubleshooting Guide](troubleshooting.md) for more solutions.

## Related Documentation

- [Quick Start Guide](quick-start.md) - Get started with default configuration
- [Docker Security](docker-security.md) - Runtime secrets, container hardening, and production setup
- [Architecture Overview](architecture.md) - Understand service dependencies
- [Database Resilience](database-resilience.md) - Connection patterns
- [Logging Guide](https://github.com/groovemap-music/catalog-api/blob/main/docs/logging-guide.md) - Logging configuration details
- [Performance Guide](performance-guide.md) - Performance tuning settings

______________________________________________________________________

**Last Updated**: 2026-10-26
