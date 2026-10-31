# Run GrooveMap in your homelab

GrooveMap is a self-hosted music knowledge graph. It loads the public Discogs and
MusicBrainz datasets (about 34 million Discogs records alone) into PostgreSQL and Neo4j and
serves them through an explorer, an API and an operations console. This guide takes you from a
Docker host to a fully loaded instance. The quick path comes first, the technical detail after.

## Quick path

You need a Linux (or macOS) machine with Docker Engine and Docker Compose v2, git, python3 3.10
or newer (Debian 12 and Ubuntu 22.04 ship it), about 16 GB of memory for Docker and about 100 GB of free disk (200 GB recommended). See
[Hardware and sizing](#hardware-and-sizing) for the numbers.

```bash
git clone https://github.com/groovemap-music/deployment.git
cd deployment
bash scripts/deploy.sh
```

The script checks the host, writes `.env` and `secrets/`, sizes Neo4j for your memory,
starts GrooveMap, waits for the Discogs import, then starts MusicBrainz. It prints your URLs and
offers to create the first admin account. Expect the first load to take hours to about two
days: releases alone take about 40 hours on the reference hardware
([dataset scale table in the architecture guide](architecture.md)). Ctrl-C, a closed terminal or a reboot loses
nothing: the import keeps running in Docker, and running `bash scripts/deploy.sh` again resumes. Use
`tmux` or `screen` anyway so you can watch it. If you use [mise](https://mise.jdx.dev/) and
[just](https://just.systems/), both optional, `just deploy` runs the same script.

Before you start, read these three notes:

- GrooveMap's images are `linux/amd64` only. See [Architecture support](#architecture-support).
- Putting a reverse proxy with HTTPS in front is required to sign in to the operations console.
  See [Known limitations](#known-limitations).
- Already running GrooveMap? See [Upgrading an existing deployment](#upgrading-an-existing-deployment).

### Let an agent do it

If you use an AI coding agent on the Docker host, the `groovemap-homelab` skill reads your
host first (reverse proxy, storage, ports, stack manager), proposes a plan, writes only
`docker-compose.override.yml` and `.env` values, and leaves the digests, secrets and staged
first load to the repository's recipes. In Claude Code:

```text
/plugin marketplace add groovemap-music/deployment
/plugin install groovemap-homelab@groovemap
```

Then ask it to install GrooveMap on this host. Other agents that support the
[agentskills.io](https://agentskills.io) format can be pointed at `skills/groovemap-homelab` in
a checkout. The skill's [SKILL.md](../skills/groovemap-homelab/SKILL.md) states its rules and
workflow.

## Prerequisites

- Docker Engine with Docker Compose v2. Deploy stops if Compose is older than v2.
- git and python3 3.10 or newer. The deploy helpers use only the Python standard library, so
  the distro's `python3` is enough. `just` is an optional convenience: every operator recipe
  (`just deploy`, `env-init`, `discogs-import-status`, `embeddings-*`) also runs without `uv`, on
  `python3`. With `uv` installed they use it instead. Set `DEPLOY_PYTHON` to pick another
  interpreter. Only maintainer recipes (`check`, `test`, `typecheck`, `audit`, `smoke-*`) need
  `uv`, and they stop with a message saying so. `discogs-import-status` exits 0 when the import is
  complete, 3 while it is still running (normal progress, not a failure: use `--wait` or run it
  again later) and 2 when it cannot tell; `just` passes these codes through unchanged.
- A host that meets [the sizing minimums](#hardware-and-sizing).
- Free host ports for the services you publish (see [Ports and bind addresses](#ports-and-bind-addresses)).
  Deploy refuses to start if a configured port is taken by something other than this stack.
- Network access to GitHub Container Registry (several GB of images) and, for the first load,
  to the Discogs and MusicBrainz dump hosts.

## Hardware and sizing

`just deploy` enforces these minimums and warns below the recommended sizes. `--force` goes
ahead anyway; it also skips the memory minimum, so a relocated data volume does not need it. The values come from `scripts/deploy_helpers.py`.

| Check | Minimum | Recommended |
| --- | --- | --- |
| Memory for Docker, production | 16 GB | 32 GB |
| Memory for Docker, `--dev` | 8 GB | 16 GB |
| Free disk, new install, summed over the disks the data volumes live on | 100 GB | 200 GB |

On an existing deployment the disk check only warns below 20 GB free.

| Data | Size on disk |
| --- | --- |
| Discogs dump (`discogs_data`) | about 11.3 GB |
| PostgreSQL with Discogs loaded | about 48 GB |
| Neo4j with Discogs loaded | about 28 GB |
| MusicBrainz | **an estimate, not measured yet** (tracked as gm-deployment-q4en); the 200 GB recommendation assumes it is as large as Discogs |

Discogs is about 76 GB on disk in total ([dataset scale table in the architecture guide](architecture.md)).
Images and container layers add several GB under Docker's data root.

Neo4j is sized from the memory Docker reports: half of it goes to heap plus page cache, up to
24 GB. The table and rules are in
[Neo4j sizing](configuration.md#neo4j-sizing-written-by-just-deploy). PostgreSQL's 2 GB shared
buffers and 6 GB cache in `docker-compose.prod.yml` come on top. `just deploy` writes the Neo4j
values to `.env` only where you have not set them, so you can edit them afterwards.

The disk check runs after `.env` and the override are in place and follows each data volume
to the disk it lives on, including bind mounts and `driver_opts: {o: bind, device: ...}`
volumes from `docker-compose.override.yml` (see [Data on a large disk](#data-on-a-large-disk)).
Volumes that share a filesystem share its free space, so each disk needs the sum of its
volumes' shares of the sizes above. The per-volume shares are in
[configuration](configuration.md#disk-check-per-volume-shares). Docker Desktop's VM disk, a
remote `DOCKER_HOST` and network filesystems cannot be measured from the host; deploy warns
with the sizes instead, and you check those yourself.

### Architecture support

GrooveMap's own images are built for `linux/amd64` only. On an arm64 host they run under
emulation, much more slowly, and the first load takes far longer. Deploy asks before going
ahead (`-y`/`--yes` to skip the question); on Linux, amd64 binfmt emulation has to be
registered first. Multi-arch images are tracked as gm-automation-2ik.

## What `just deploy` does

The step-by-step flags and exit codes are in the [quick start](quick-start.md#the-short-way-bash-scriptsdeploysh).
In order, it:

1. Runs the host checks above, stopping with exit code 3 before changing anything if a
   minimum is not met.
2. Creates `.env` from `.env.example` with the reviewed image digests (`just env-init`) unless
   one exists, and creates any missing files under `secrets/` (`just secrets-bootstrap`),
   never replacing existing ones.
3. Sizes Neo4j for the machine and fills `APP_BASE_URL` with a LAN default if it is unset.
4. If `docker-compose.override.yml` exists, runs the [override check](#the-override-and-its-safety-check).
5. Starts the production stack (`-f docker-compose.yml -f docker-compose.prod.yml`, plus the
   override last) without the `musicbrainz` profile, and waits for the Discogs import with
   `just discogs-import-status`.
6. Sets `COMPOSE_PROFILES=musicbrainz` in `.env`, starts MusicBrainz, prints the endpoints and
   offers to create the first admin account.

`just deploy --no-wait` stops after step 5 starts, and `just deploy --dev` uses the base
development stack instead (fixed public credentials; not for a host others can reach). To
watch the import on its own: `just discogs-import-status --prod` (exit 0 complete, 3 in
progress, 2 cannot determine), or add `--wait` to poll.

## The first load: Discogs before MusicBrainz

The Discogs import must finish before MusicBrainz starts. MusicBrainz entities attach to the
Discogs id that already exists for the same thing; if MusicBrainz loads first or at the same
time, they mint separate ids instead, and no later reload heals the split. A measurement on the
predecessor system found about 3.66 million entities that would be affected
([maintenance](maintenance.md#post-import-identity-maintenance)).

The order is enforced in two places. The three MusicBrainz services (`extractor-musicbrainz`,
`brainzgraphinator`, `brainztableinator`) sit behind the `musicbrainz` Compose profile, so a
plain `up` leaves them out. The gate, `just discogs-import-status`, reports the import complete
only when the loader's latch, Neo4j's completion marker and the eight Discogs queues all agree
for the newest dump version. `just deploy` runs both for you. Done by hand, run the gate
until it exits 0 and only then enable the profile.

`just deploy` treats a host as already running MusicBrainz only on evidence that MusicBrainz
itself produced: one of the three MusicBrainz containers (any state), a Compose volume that
only `musicbrainz`-profile services mount (such as `extractor_musicbrainz_logs`; computed from
the rendered Compose config, not a fixed list), or a `loader_extraction_latch` row with
`loader = 'musicbrainz'` while PostgreSQL runs. `musicbrainz_data` is not evidence: the API
mounts it read-only, so phase 1 creates it before MusicBrainz ever runs. If the render or the
query fails, `just deploy` does not guess; it goes through the Discogs gate.

### Later cycles

After the first load the order cannot be strictly enforced: MusicBrainz publishes twice weekly
and Discogs monthly, so new MusicBrainz links keep arriving ahead of their Discogs targets.
catalog-api re-attaches and projects by itself after each completed Discogs extraction
(`IDENTITY_AUTO_REATTACH_ENABLED` is `true` on the `api` service), so later cycles heal without
an operator. The manual `catalog-identity-reattach` and `catalog-identity-projection` commands
remain for a dry run, a forced re-run, or a stack that sets the switch to `false`
([maintenance](maintenance.md#post-import-identity-maintenance)).

## Service map

About two dozen containers, all on one `groovemap` bridge network (`172.20.0.0/16`).

| Group | Services | Role |
| --- | --- | --- |
| Data stores | `postgres`, `neo4j`, `rabbitmq`, `valkey` | Relational store, graph, message broker, cache |
| Schema | `schema-init`, `rabbitmq-dlq-policy-init` | One-shot initialisers for the schemas and broker policy |
| Discogs path | `extractor-discogs`, `graphinator`, `tableinator` | Download the dump, load the graph and PostgreSQL |
| MusicBrainz path | `extractor-musicbrainz`, `brainzgraphinator`, `brainztableinator` | Same for MusicBrainz; profile `musicbrainz` |
| Applications | `api`, `explore`, `dashboard`, `insights` | Catalog API, graph explorer (the app), operations console, insights |
| Observability | `grafana`, `victoria-metrics`, `victoria-traces`, `otel-collector`, `postgres-exporter`, `valkey-exporter`, `cadvisor`, `node-exporter` | Metrics, traces, dashboards |
| Jobs | `embeddings` | Monthly embedding refresh; profile `jobs`, run on demand |

The [architecture guide](architecture.md) has the data flow.

## Compose files

| File | Purpose |
| --- | --- |
| `docker-compose.yml` | The base stack with local development credentials. Never deploy it alone as production |
| `docker-compose.prod.yml` | Production overlay: Docker file secrets, hardening, Neo4j and PostgreSQL sizing, loopback binding for unauthenticated backends, Grafana without anonymous access |
| `docker-compose.override.yml` | Yours, untracked, optional: host-specific settings. `.gitignore` already lists it |
| `docker-compose.external-postgres.yml`, `docker-compose.external-valkey.yml` | Added by `just deploy` and the gate when `.env` selects an existing PostgreSQL or Valkey ([Existing PostgreSQL and Valkey](#existing-postgresql-and-valkey)); never apply them by hand otherwise |

Production is always `-f docker-compose.yml -f docker-compose.prod.yml`, then any
external-store overlay `.env` selects, and `-f docker-compose.override.yml` last when you have
one. Compose reads the override by itself only when no `-f` is given, so name it explicitly.

### Profiles

| Profile | Services | When |
| --- | --- | --- |
| `musicbrainz` | `extractor-musicbrainz`, `brainzgraphinator`, `brainztableinator` | After the Discogs import; `just deploy` sets `COMPOSE_PROFILES=musicbrainz` in `.env` |
| `jobs` | `embeddings` | Run on demand ([monthly embedding refresh](maintenance.md#monthly-embedding-refresh)) |
| `local-stores` | `postgres`, `postgres-exporter`, `valkey`, `valkey-exporter` | Never enabled. Only an external-store overlay puts a store and its exporter here, so that store is left out without removing the service; the base files never use it |

### The override and its safety check

Put proxy labels and networks, volume locations, restart policies, the `groovemap` network's
subnet and services of your own in `docker-compose.override.yml`; see
[Docker Compose Override](configuration.md#4-docker-compose-override). Before starting
anything, `just deploy` renders the stack with and without the override and stops with exit
code 3 if the override changes what only `.env` or deploy may change: `image`, `build`,
`platform`, `container_name` or `profiles` on a stack service, or `ports` or `network_mode` of
PostgreSQL, Neo4j, RabbitMQ, Valkey, VictoriaMetrics or VictoriaTraces.

## Ports and bind addresses

Every published host port is an `.env` variable, and so are the bind addresses. The full tables
are in [Host ports and bind addresses](configuration.md#host-ports-and-bind-addresses). The
ones you are likely to touch:

| Service | Default host port | Variable |
| --- | --- | --- |
| Operations console | 8003 | `DASHBOARD_HOST_PORT` |
| Catalog API (health) | 8004 (8005) | `API_HOST_PORT` (`API_HEALTH_HOST_PORT`) |
| Graph explorer (health) | 8006 (8007) | `EXPLORE_HOST_PORT` (`EXPLORE_HEALTH_HOST_PORT`) |
| Grafana | 3000 | `GRAFANA_HOST_PORT` |
| PostgreSQL | 5433 | `POSTGRES_HOST_PORT` |
| Neo4j HTTP, Bolt | 7474, 7687 | `NEO4J_HTTP_HOST_PORT`, `NEO4J_BOLT_HOST_PORT` |
| RabbitMQ AMQP, management | 5672, 15672 | `RABBITMQ_AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT` |

`WEB_BIND_ADDRESS` covers Grafana, the API, the console and the explorer. `DATA_BIND_ADDRESS`
covers PostgreSQL, Neo4j and RabbitMQ (and Valkey and the Victoria backends in the base file).
Both default to all interfaces. Docker-published ports bypass host firewalls such as ufw, so
set `DATA_BIND_ADDRESS=127.0.0.1` on any host others can reach, and `WEB_BIND_ADDRESS=127.0.0.1`
when a reverse proxy on the same host fronts the web services. If you move a port that a
browser or link uses, update `APP_BASE_URL`, `GRAFANA_URL` and `TRACES_URL`.

## Reverse proxy and `APP_BASE_URL`

`APP_BASE_URL` is the public origin of the explorer as users reach it, for example
`https://groovemap.example.com`. Password-reset emails link to it, and the production overlay
requires it. Behind a proxy it is the proxied `https://` URL. The explorer is the user-facing
app; the operations console is for admins (prefer LAN or VPN access); Grafana is optional.

The skill's
[reverse-proxy recipes](../skills/groovemap-homelab/references/reverse-proxy.md) cover
Traefik, Caddy, nginx, Nginx Proxy Manager, Cloudflare Tunnel and Tailscale, with override
examples in `skills/groovemap-homelab/assets/`. The proxy in front of the console has to meet
the requirements in
[Console session cookie and HTTPS](configuration.md#console-session-cookie-and-https).

## Data on a large disk

Volumes live under Docker's data root by default. To put the large ones elsewhere, redefine
them in `docker-compose.override.yml`; the skill's
[storage notes](../skills/groovemap-homelab/references/storage.md) and
`assets/override-storage.yml` show how. The big three are `postgres_data` (about 48 GB with
Discogs), `neo4j_data` (about 28 GB) and `discogs_data` (about 11.3 GB). The other volumes
are logs and the observability stores. Set `COMPOSE_PROJECT_NAME` in `.env` on a fresh
install so volume names do not depend on the checkout directory, and never change it or rename
the directory on an existing deployment: the stack would come up on new, empty volumes.

## Upgrading an existing deployment

> **If you already run GrooveMap, read this after pulling this change.** MusicBrainz now sits
> behind the `musicbrainz` Compose profile. Add `COMPOSE_PROFILES=musicbrainz` to `.env`, or
> run `just deploy`, which detects an existing MusicBrainz deployment (see
> [how it decides](#the-first-load-discogs-before-musicbrainz)) and sets it for you. Otherwise a plain `docker compose up -d` no longer manages the MusicBrainz services.

To move to newly reviewed image digests, pull this repository, refresh `.env` and redeploy:

```bash
git pull
just env-init      # or: python3 scripts/env-init.py
just deploy        # or: bash scripts/deploy.sh
```

With an existing `.env`, `just env-init` rewrites only the `*_IMAGE` lines to the reviewed
digests and appends any key the new `.env.example` has and your file lacks, under a comment.
Every other line stays byte for byte: `APP_BASE_URL`, the bind addresses, `*_HOST_PORT` remaps,
`COMPOSE_PROFILES` and the Neo4j sizing that `just deploy` wrote. It keeps the previous file as
`.env.<timestamp>.bak`, lists what it changed, and does nothing when `.env` is already current.
`just env-init --force` instead regenerates `.env` from the template; it warns which of your
values it drops, and the backup still has them.
`.env` is created private (mode 0600); a rewrite and its backup keep the mode the file already has.

## Existing PostgreSQL and Valkey

The agent skill discovers existing PostgreSQL, PgBouncer, Valkey and Redis instances read-only
(versions, networks, connections, memory, eviction policy, persistence, PgBouncer pool mode),
never displays credentials and never modifies them. When PostgreSQL and Valkey both exist, it
asks: "This homelab already has PostgreSQL and Valkey. Would you prefer GrooveMap to reuse
compatible existing services, or run dedicated instances?" It recommends dedicated instances;
discovery alone never authorizes reusing anything.

GrooveMap supports an existing PostgreSQL, an existing Valkey, or both, through an
**external-stores mode** (`gm-deployment-e1uh`). The Discogs-before-MusicBrainz gate stays
authoritative in it: the gate and deploy's probes read the external database the loaders write
to, an external store that cannot be reached or is wrong gives "cannot determine" or a refusal,
and neither ever enables MusicBrainz.

**Status: supported.** The reviewed release set meets the capability floors. `just deploy`
refuses each external store (exit 3, nothing changed) unless `.env` pins images that support it,
recorded as `EXTERNAL_STORE_FLOORS` beside `RELEASED_IMAGE_DIGESTS` in `scripts/check-images.py`:

| Store | Needs | Upstream work | Floor |
| --- | --- | --- | --- |
| PostgreSQL | database-schema with `POSTGRES_PREPROVISIONED` (verify the database, role and `pg_trgm` instead of creating them) | `gm-database-schema-2aty` | database-schema 0.6.0 |
| Valkey | catalog-api and analytics-engine that apply `VALKEY_KEY_PREFIX` to every key and speak TLS (`VALKEY_TLS`) | `gm-python-libraries-3sm` and its TLS settings, `gm-catalog-api-rchs`, `gm-analytics-engine-bah5`, `gm-catalog-api-vd7n`, `gm-analytics-engine-m0sz` | catalog-api 0.6.0 and analytics-engine 0.5.0 |

Your `.env` must run exactly the reviewed digests (`just env-init`), or `just deploy` still
refuses. A later release that raises a floor is recorded in `EXTERNAL_STORE_FLOORS` the same way.

### What GrooveMap needs from each server

PostgreSQL:

- **PostgreSQL 18 or newer** (the schema uses `uuidv7()`).
- **A database and a role of GrooveMap's own**, created by an administrator: the role owns the
  database (or holds `CREATE` on it and on its `public` schema) and needs no `SUPERUSER`,
  `CREATEDB` or `CREATEROLE`. Never another application's database, `postgres` or a template.
- **`pg_trgm` installed in that database.** `vector` is optional (artist embeddings).
- **At least 60 usable connections** for GrooveMap, on top of other applications, and no role or
  database connection limit below that.
- **No transaction pooling.** A PgBouncer must pool by session: catalog-api holds a
  session-level advisory lock that transaction and statement pooling break silently.
- **TLS with a certificate GrooveMap can verify.** Every client connects with
  `EXTERNAL_POSTGRES_SSLMODE` (default `verify-full`: the certificate and the host name are
  checked against the CA in `secrets/postgres_ca.crt`).

The provisioning SQL, run once as an administrator (replace the names and password):

```sql
CREATE ROLE groovemap LOGIN PASSWORD '<password>' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE groovemap OWNER groovemap;
\c groovemap
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;   -- optional, superuser only
```

Valkey:

- **Valkey 8.0 or newer** (or Redis 7.2 or newer); GrooveMap is tested with Valkey 9.1.
- **Authentication on.** GrooveMap authenticates with a password only (the `default` user).
- **`maxmemory-policy noeviction` and AOF persistence.** GrooveMap keeps security state that must
  never be evicted. Never change a shared instance for GrooveMap; if it does not match, run a
  dedicated one.
- **TLS with a certificate GrooveMap can verify** (`EXTERNAL_VALKEY_TLS`, default on), signed by
  the CA in `secrets/valkey_ca.crt`; the host name is checked too unless
  `EXTERNAL_VALKEY_TLS_VERIFY_HOSTNAME=false`. Client certificates are not wired yet.
- **A key prefix no other application uses** (`EXTERNAL_VALKEY_KEY_PREFIX`, such as
  `groovemap:`). Every GrooveMap key lives under it. A prefix, like a separate logical database,
  keeps cooperating applications apart; neither is a security boundary, since any client with
  the password can read any key and `FLUSHALL` is instance-wide.

### Procedure

1. Provision the database, role and extensions above, and check the Valkey settings. These are
   operator-run changes on services GrooveMap does not own.
2. Write the credentials yourself, before deploying: the role's name in
   `secrets/postgres_username.txt`, its password in `secrets/postgres_password.txt`, and the
   Valkey password in `secrets/valkey_password.txt` (mode 0600; seed them from SOPS or your
   secret store), plus the CA certificates in `secrets/postgres_ca.crt` and
   `secrets/valkey_ca.crt`. `just deploy` stops if one is missing, because `create-secrets.sh`
   would otherwise invent a value the server does not know.
3. Set the mode and the servers in `.env` (all variables in
   [configuration.md](configuration.md#external-postgresql-and-valkey)):

   ```bash
   GROOVEMAP_POSTGRES=external
   EXTERNAL_POSTGRES_HOST=db.lan          # host.docker.internal for a server on this Docker host
   EXTERNAL_POSTGRES_DATABASE=groovemap
   GROOVEMAP_VALKEY=external
   EXTERNAL_VALKEY_HOST=cache.lan
   EXTERNAL_VALKEY_KEY_PREFIX=groovemap:
   ```

   Each store is independent: either one can stay local.
4. Run `just deploy`. Before anything starts it checks the settings (TLS included), the
   capability floors, the secrets, and that this project has no `postgres_data` volume; then it
   writes `COMPOSE_FILE` to `.env` (below), adds the overlays and
   runs each store's read-only check, `store-check-postgres` and `store-check-valkey`, which
   verify the list above and say exactly what fails. Every later `docker compose up` runs the
   checks again first, and the services wait for them instead of the local stores' health.
   The staged first load then runs as usual, the gate reading the external database.

The servers must be reachable from containers on the `groovemap` network: a LAN address, or
`host.docker.internal` for a server on the Docker host itself (the overlays map it to the host
gateway). A server bound only to the host's `127.0.0.1` is not. For a server in another Compose
project, attach its network to the GrooveMap services and store checks in your override.

**Cleartext is refused off this machine.** A server that is not loopback or
`host.docker.internal` (this Docker host, which the overlays map to the host gateway) must be
reached over TLS: `EXTERNAL_POSTGRES_SSLMODE=disable`, `allow` or `prefer`, or
`EXTERNAL_VALKEY_TLS=false`, stops `just deploy`, and the store checks refuse it again on every
`docker compose up`. Anyone on the network path could read the password and every query, key and
value, and impersonate the server. To accept that on a network you trust, set
`EXTERNAL_POSTGRES_ALLOW_PLAINTEXT=true` or `EXTERNAL_VALKEY_ALLOW_PLAINTEXT=true`; every run then
says so with a `[warn]` line. A container name on a shared Docker network is not recognized as
this machine, so it needs TLS or the opt-out too. `EXTERNAL_POSTGRES_SSLMODE=require` encrypts
without checking who answers and is accepted with a warning.

**Bare Compose uses the same files.** While a store is external, `just deploy` writes
`COMPOSE_FILE` to `.env` (with the platform's path separator) listing exactly the files it uses:
`docker-compose.yml`, `docker-compose.prod.yml`, the external overlays and
`docker-compose.override.yml` when you have one. It rewrites it on every run, so a plain
`docker compose up -d`, `ps`, `logs` or `exec` in the checkout addresses the real stack and can
never start the local stores and point the services at them. Leave that line alone. If you set a
`COMPOSE_FILE` yourself (in `.env` or the shell), `just deploy` refuses to continue in external
mode instead of overwriting it; with both stores local it never writes one, and it removes its
own when you switch back. The gate (`just discogs-import-status --prod`) and deploy's probes use
the same files. Run Compose from the checkout, and with the same `-p` if you deploy with one.

**Switching an existing deployment is a data migration**, not a setting. `just deploy` refuses
an external PostgreSQL in a project that already has a `postgres_data` volume, because its data
is there and the gate would read an empty server. Move the data first, or stay local.

**What becomes yours.** Backups, upgrades, restarts, disk space and monitoring of an external
store are its operator's responsibility. The disk check skips it, `postgres-exporter` and
`valkey-exporter` do not run (their scrape jobs read as down on the infrastructure dashboard),
and the maintenance scripts that exec into the local `postgres` (`reset-password.sh`,
`provision-embedding-pipeline-login.sh`, `cleanup-implausible-years.sh`) do not apply; run
their SQL against your server instead. An override may still never rewire `POSTGRES_*`,
`VALKEY_*` or the store secrets: the supported mode is the only route
([the override check](#the-override-and-its-safety-check)). Secrets stay in encrypted storage,
and nothing is applied on a shared server without your explicit approval. Details:
[shared-services.md](../skills/groovemap-homelab/references/shared-services.md).

## Known limitations

These describe current behavior. A follow-up is planned; no dates are promised.

- **Console sign-in needs HTTPS, or an explicit opt-in.** The operations console's session
  cookie is Secure in the production overlay, so over plain http on a LAN IP sign-in would
  silently return to the login form. `just deploy` now says so before it prints the console
  URL and offers two ways out: a TLS reverse proxy (recommended; see
  [the reverse-proxy guide](../skills/groovemap-homelab/references/reverse-proxy.md)), or, on
  a trusted LAN only, `GROOVEMAP_ALLOW_INSECURE_CONSOLE=true` in `.env`, which `just deploy`
  offers to set (default no) and applies by recreating only `dashboard`
  ([details](configuration.md#console-session-cookie-and-https)).
- **`just env-init --force` drops host-specific values.** Plain `just env-init` keeps them; see
  [Upgrading](#upgrading-an-existing-deployment).
- **The disk check cannot measure everything.** Docker Desktop's VM disk, a remote `DOCKER_HOST`,
  network filesystems and non-local volume drivers get a warning with the sizes, not a
  measurement. See [Hardware and sizing](#hardware-and-sizing).
- **Later cycles re-attach automatically.** The manual commands are for dry runs and forced
  re-runs. See [Later cycles](#later-cycles).
- **Existing PostgreSQL and Valkey need the reviewed images and server prerequisites.** The
  external-stores mode is supported: the capability floors are met by the reviewed release set,
  and `just deploy` still refuses a store whose settings, secrets, TLS or server checks fail.
  See [Existing PostgreSQL and Valkey](#existing-postgresql-and-valkey).
- **MusicBrainz disk use is an estimate.**

## Backups and operations

Backups are environment-specific and must be tested: see
[Backups and restore drills](maintenance.md#backups-and-restore-drills) and
[Database resilience](database-resilience.md). Day-to-day operation is covered by
[Administration](admin-guide.md), [Monitoring](monitoring.md) and
[Troubleshooting](troubleshooting.md); routine updates by [Maintenance](maintenance.md).
An external PostgreSQL or Valkey is backed up by its own operator, not by this stack
([what becomes yours](#existing-postgresql-and-valkey)).
