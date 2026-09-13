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

Override non-secret settings in `docker-compose.yml` or `docker-compose.override.yml`:

```yaml
services:
  dashboard:
    environment:
      - LOG_LEVEL=DEBUG
      - REDIS_HOST=redis
```

## Core Settings

### Effective Compose inputs

The following groups are interpolated by the executable stack:

| Inputs | Purpose |
| --- | --- |
| `DATABASE_SCHEMA_IMAGE`, `CATALOG_API_IMAGE`, `DISCOGS_INGESTION_IMAGE`, `MUSICBRAINZ_INGESTION_IMAGE`, `DISCOGS_GRAPH_ENRICHER_IMAGE`, `MUSICBRAINZ_GRAPH_ENRICHER_IMAGE`, `DISCOGS_SQL_LOADER_IMAGE`, `MUSICBRAINZ_SQL_LOADER_IMAGE`, `OPERATIONS_CONSOLE_IMAGE`, `GRAPH_EXPLORER_IMAGE`, `ANALYTICS_ENGINE_IMAGE` | Required immutable source-owned images |
| `UID`, `GID` | Host identity stamped on internal-image containers |
| `APP_BASE_URL`, `DB_PROFILING`, `ENCRYPTION_MASTER_KEY`, `INSIGHTS_INTERNAL_SECRET`, `NLQ_ENABLED`, `NLQ_API_KEY`, `NLQ_MODEL`, `RESEND_API_KEY`, `RESEND_SENDER_EMAIL`, `RESEND_SENDER_NAME` | Optional base-stack application settings |
| `NEO4J_HEAP_SIZE`, `NEO4J_PAGECACHE_SIZE`, `NEO4J_MEMORY_LIMIT` | Production overlay sizing |
| `GM_EXTRACTION_RULES_FILE` | Optional host path for the promoted Discogs extraction rules |
| `SMOKE_MEDIA_RABBITMQ_PORT`, `SMOKE_MEDIA_SERVICE_PLATFORM`, `SMOKE_MEDIA_SUBNET` | Isolated media-smoke settings only |

`docker compose config --environment` shows interpolation inputs, while
`just config` and `just config-prod` show the complete effective container
configuration. The sections below describe service runtime contracts; change a
literal Compose value with a reviewed override file, not by assuming every
source-level variable is an `.env` interpolation.

### RabbitMQ Configuration

RabbitMQ connections are configured using individual component variables.

| Variable            | Description        | Default          | Required |
| ------------------- | ------------------ | ---------------- | -------- |
| `RABBITMQ_HOST`     | RabbitMQ hostname  | `rabbitmq`       | No       |
| `RABBITMQ_PORT`     | RabbitMQ AMQP port | `5672`           | No       |
| `RABBITMQ_USERNAME` | RabbitMQ username  | `groovemap` | No       |
| `RABBITMQ_PASSWORD` | RabbitMQ password  | `groovemap` | No       |

**Used By**: Extractor, Graphinator, Tableinator, Brainzgraphinator, Brainztableinator, Dashboard

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

- Automatic reconnection on failure
- Heartbeat: 60 seconds
- Connection timeout: 30 seconds
- Prefetch count: 100 (configurable per service)

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

**Directory layout**:

| Directory | Dataset example | Metadata file |
| --- | --- | --- |
| `/discogs-data/artists/` | `discogs_20250115_artists.xml.gz` | `.metadata_artists.json` |
| `/discogs-data/labels/` | `discogs_20250115_labels.xml.gz` | `.metadata_labels.json` |
| `/discogs-data/releases/` | `discogs_20250115_releases.xml.gz` | `.metadata_releases.json` |
| `/discogs-data/masters/` | `discogs_20250115_masters.xml.gz` | `.metadata_masters.json` |

**PERIODIC_CHECK_DAYS**:

- How often to check for new data dumps
- Set to `0` to disable automatic checks
- The source-level default is 15 days; Compose deliberately overrides it per
  producer as shown above

## Database Connections

### Neo4j Configuration

| Variable            | Description                                             | Default     | Required |
| ------------------- | ------------------------------------------------------- | ----------- | -------- |
| `NEO4J_HOST`        | Neo4j hostname                                          | `localhost` | Yes      |
| `NEO4J_USERNAME`    | Neo4j username                                          | `neo4j`     | Yes      |
| `NEO4J_PASSWORD`    | Neo4j password                                          | (none)      | Yes      |
| `NEO4J_TLS_ENABLED` | Encrypt the Bolt connection to Neo4j (TLS)             | `false`     | No       |
| `NEO4J_TLS_VERIFY`  | Verify the Neo4j server certificate when TLS is enabled | `true`      | No       |

**Used By**: Graphinator, API, Schema-Init, Dashboard, Brainzgraphinator

**Connection Details**:

- Protocol: Bolt (binary protocol)
- Default port: 7687
- Connection pool: 50 connections (max)
- Retry logic: Exponential backoff (max 5 attempts)
- Transaction timeout: 60 seconds

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

| Variable            | Description                                  | Default          | Required |
| ------------------- | -------------------------------------------- | ---------------- | -------- |
| `POSTGRES_HOST`     | PostgreSQL hostname, optionally `host:port`  | `localhost`      | Yes      |
| `POSTGRES_PORT`     | PostgreSQL port (used when not in host)       | `5432`           | No       |
| `POSTGRES_USERNAME` | PostgreSQL username                          | (none)           | Yes      |
| `POSTGRES_PASSWORD` | PostgreSQL password                          | (none)           | Yes      |
| `POSTGRES_DATABASE` | Database name                                | `groovemap` | Yes      |
| `POSTGRES_POOL_MIN_SIZE` | Override the connection-pool minimum for **all** pooled services | per-service default | No |
| `POSTGRES_POOL_MAX_SIZE` | Override the connection-pool maximum for **all** pooled services | per-service default | No |

**Used By**: Tableinator, Dashboard, API, Insights, Brainztableinator, Schema-Init

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
- Retry logic: Exponential backoff (max 5 attempts)
- Query timeout: 30 seconds

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

> Query timeout (30 seconds, see **Connection Details** above) is a fixed value in
> code, not a configurable env var.

### Redis Configuration

| Variable         | Description                            | Default     | Required                               |
| ---------------- | -------------------------------------- | ----------- | -------------------------------------- |
| `REDIS_HOST`     | Redis hostname                         | `localhost` | Yes (Dashboard, API, Insights) |
| `REDIS_PORT`     | Redis port                             | `6379`      | No                                     |
| `REDIS_PASSWORD` | Redis password (`requirepass`)         | _(none)_    | No (required if Redis enforces auth)   |

**Used By**: Dashboard, API, Insights

**Connection Details**:

- Protocol: Redis protocol
- Default port: 6379 (override with `REDIS_PORT`)
- Database: 0 (default)
- Authentication: when `REDIS_PASSWORD` is set it is embedded in the connection URL (`redis://:<password>@host:port/0`); omitted entirely when unset. Supports the `_FILE` secret convention (`REDIS_PASSWORD_FILE`).
- Connection pool: Automatic
- Retry logic: Exponential backoff

**Examples**:

```bash
# Local development (no auth)
REDIS_HOST="localhost"

# Docker Compose
REDIS_HOST="redis"

# Authenticated Redis (e.g. requirepass enabled)
REDIS_HOST="redis"
REDIS_PASSWORD="<password>"
# or, via Docker secret:
REDIS_PASSWORD_FILE="/run/secrets/redis_password"
```

**Cache Configuration**:

- Default TTL: 3600 seconds (1 hour)
- Max memory: 512MB in the base file (change through a reviewed Compose override)
- Eviction policy: allkeys-lru
- Persistence: append-only file enabled (`--appendonly yes`) on the
  `redis_data` volume; Redis is used as a cache, but container replacement does
  not discard the volume automatically

## JWT Configuration

| Variable                       | Description                                                       | Default | Required  |
| ------------------------------ | ----------------------------------------------------------------- | ------- | --------- |
| `JWT_SECRET_KEY`               | HMAC-SHA256 signing secret                                        | (none)  | Yes (API) |
| `JWT_EXPIRE_MINUTES`           | Token lifetime in minutes                                         | `30`    | No        |
| `DISCOGS_USER_AGENT`           | User-Agent for Discogs API requests                               | (none)  | Yes (API) |
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
- Payload claims: `sub` (user UUID), `email`, `iat`, `exp`

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

# Discogs User-Agent (required for Discogs API)
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
| `RABBITMQ_USERNAME`     | RabbitMQ management API username (shared with the core RabbitMQ credentials) | `groovemap`  | No       |
| `RABBITMQ_PASSWORD`     | RabbitMQ management API password (shared with the core RabbitMQ credentials) | `groovemap`  | No       |
| `CORS_ORIGINS`                 | Comma-separated list of allowed CORS origins | (none — disabled) | No       |
| `CACHE_WARMING_ENABLED`        | Pre-warm cache on startup                    | `true`            | No       |
| `CACHE_WEBHOOK_SECRET`         | Secret for cache invalidation webhooks       | (none — disabled) | No       |
| `API_HOST`                     | API service hostname for admin proxy         | `api`             | No       |
| `API_PORT`                     | API service port for admin proxy             | `8004`            | No       |

**Used By**: Dashboard only (for `CACHE_WARMING_ENABLED`, `CACHE_WEBHOOK_SECRET`); `RABBITMQ_USERNAME` / `RABBITMQ_PASSWORD` and `CORS_ORIGINS` are also supported by other services — see the [RabbitMQ Configuration](#rabbitmq-configuration) and [API](#api) sections above. `API_HOST` / `API_PORT` are used by the admin proxy router to forward authenticated admin requests to the API service.

**Notes**:

- The dashboard authenticates against the RabbitMQ management API using the same `RABBITMQ_USERNAME` / `RABBITMQ_PASSWORD` credentials as the core RabbitMQ connection (there is no separate management-only credential pair). In production these are supplied via `RABBITMQ_USERNAME_FILE` / `RABBITMQ_PASSWORD_FILE` (Docker secrets)
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
REDIS_HOST="localhost"
JWT_SECRET_KEY="your-secret-key-here"
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

**Notes**: After startup, set Discogs app credentials using the `discogs-setup` CLI bundled in the API container:

```bash
docker compose exec api discogs-setup \
  --consumer-key YOUR_CONSUMER_KEY \
  --consumer-secret YOUR_CONSUMER_SECRET

# Verify (values are masked)
docker compose exec api discogs-setup --show
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
LOG_LEVEL=INFO
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
REDIS_HOST="localhost"

# RabbitMQ (also used to authenticate against the RabbitMQ management API)
RABBITMQ_HOST=rabbitmq           # default: rabbitmq
RABBITMQ_USERNAME=groovemap # default: groovemap
RABBITMQ_PASSWORD=groovemap # default: groovemap

# Optional - CORS
CORS_ORIGINS="http://localhost:8003,http://localhost:8006"  # comma-separated origins

# Optional - Cache
CACHE_WARMING_ENABLED=true         # Pre-warm cache on startup
CACHE_WEBHOOK_SECRET=              # Secret for cache invalidation webhooks

# Optional - Logging
LOG_LEVEL=INFO
```

Published health endpoint: <http://localhost:8003/health>.

### Insights

```bash
# Required
API_BASE_URL="http://api:8004"       # URL of the API service (fetches raw query data over HTTP)
POSTGRES_HOST="localhost"
POSTGRES_USERNAME="groovemap"
POSTGRES_PASSWORD="groovemap"
POSTGRES_DATABASE="groovemap"

# Optional - Redis Caching
REDIS_HOST="localhost"                        # Redis hostname for result caching (default: localhost)

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

# Optional - Startup
STARTUP_DELAY=10                              # Seconds to wait before starting (code default: 0, docker-compose: 10)

# Optional - Logging
LOG_LEVEL=INFO
```

Internal health port: 8009. It is not published to the host; use
`docker compose ps` for status.

**Notes**: The Insights service uses Redis for caching computed results (cache-aside pattern). The cache TTL matches the `INSIGHTS_SCHEDULE_HOURS` interval and is invalidated after each computation run. If Redis is unavailable, the service operates without caching.

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

Internal health port: 8011. It is not published to the host; use
`docker compose ps` for status.

**Notes**: Brainzgraphinator enriches existing Neo4j nodes with MusicBrainz metadata (properties, relationships, cross-references). It skips entities without Discogs matches. Consumes from the `musicbrainz-{artists,labels,release-groups,releases}` exchanges.

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

Internal health port: 8010. It is not published to the host; use
`docker compose ps` for status.

**Notes**: Brainztableinator stores all MusicBrainz data in the `musicbrainz` PostgreSQL schema — including entities without Discogs matches — with relationships and external links. Consumes from the `musicbrainz-{artists,labels,release-groups,releases}` exchanges. Unlike Graphinator/Tableinator/Brainzgraphinator, it does **not** batch writes — it commits one PostgreSQL transaction per message and instead sets RabbitMQ prefetch (channel-global QoS) equal to its connection-pool maximum, so in-flight message handlers never exceed pool capacity.

### MCP Server

```bash
# Required
API_BASE_URL="http://api:8004"   # Base URL for the GrooveMap API
```

**Notes**: The MCP server has no direct database dependencies — all data is fetched via the API service over HTTP. It supports `stdio` (default, for local use with Claude Desktop/Cursor/Zed) and `streamable-http` (for hosted deployments) transports.

## Environment Templates

### Development example

The repository ships `.env.example`; it does not ship `.env.development`.
Start from the former so all eleven required image variables remain present.
The fragment below illustrates optional runtime overrides only and is not a
complete Compose input.

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

# Redis
REDIS_HOST=localhost

# JWT (API)
JWT_SECRET_KEY=dev-secret-key-not-for-production
JWT_EXPIRE_MINUTES=1440
DISCOGS_USER_AGENT="GrooveMap/1.0-dev +https://groovemap.music"

# Data
DISCOGS_ROOT=/tmp/discogs-data-dev
PERIODIC_CHECK_DAYS=0

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
| `neo4j_password.txt` | `openssl rand -base64 24` |
| `postgres_password.txt` | `openssl rand -base64 24` |
| `postgres_username.txt` | `groovemap` |
| `rabbitmq_password.txt` | `openssl rand -base64 24` |
| `rabbitmq_username.txt` | `groovemap` |
| `redis_password.txt` | `openssl rand -base64 24` |
| `insights_internal_secret.txt` | `openssl rand -hex 32`; shared by API and insights |
| `grafana_admin_password.txt` | `openssl rand -base64 24`; Grafana admin login |

See `secrets.example/` for reference placeholders and generation commands.

**Step 2 — Set non-secret production environment** in the target environment.
The values are non-secret, but this repository still forbids committing `.env`
or rendered environment configuration:

```bash
# RabbitMQ (hostname only — credentials come from Docker secrets)
RABBITMQ_HOST=rabbitmq.prod.internal

# Neo4j
NEO4J_HOST=neo4j.prod.internal

# PostgreSQL
POSTGRES_HOST=postgres.prod.internal
POSTGRES_DATABASE=groovemap

# Redis
REDIS_HOST=redis

# JWT (optional non-secret settings)
JWT_EXPIRE_MINUTES=1440
DISCOGS_USER_AGENT="GrooveMap/1.0 +https://groovemap.music"

# Data
DISCOGS_ROOT=/mnt/data/discogs
PERIODIC_CHECK_DAYS=15

# Logging
LOG_LEVEL=INFO

# Consumer Management
CONSUMER_CANCEL_DELAY=300
QUEUE_CHECK_INTERVAL=3600
```

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

# Redis with TLS
REDIS_HOST=rediss://:password@redis.example.com:6380/0
```

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

Only those three HTTP probes are published. Compose runs the remaining health
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
- Restart services after changes
- For Docker: rebuild images if needed

See [Troubleshooting Guide](troubleshooting.md) for more solutions.

## Related Documentation

- [Quick Start Guide](quick-start.md) - Get started with default configuration
- [Docker Security](docker-security.md) - Runtime secrets, container hardening, and production setup
- [Architecture Overview](architecture.md) - Understand service dependencies
- [Database Resilience](database-resilience.md) - Connection patterns
- [Logging Guide](https://github.com/groovemap-music/catalog-api/blob/main/docs/logging-guide.md) - Logging configuration details
- [Performance Guide](performance-guide.md) - Performance tuning settings

______________________________________________________________________

**Last Updated**: 2026-09-12
