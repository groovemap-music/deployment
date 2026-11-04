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
only `musicbrainz`-profile services mount (computed from the rendered Compose config; none
exists today), the three legacy `*_logs` volumes of the old layout, or a `loader_extraction_latch` row with
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
| Schema | `schema-init`, `rabbitmq-dlq-policy-init`, `data-volume-init` | One-shot initialisers for the schemas, broker policy and extractor data-volume ownership |
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
| `docker-compose.shared-monitoring.yml` | Added by `just deploy` and the gate when `.env` sets `GROOVEMAP_MONITORING=shared` ([Shared monitoring](#shared-monitoring)); never apply it by hand otherwise |

Production is always `-f docker-compose.yml -f docker-compose.prod.yml`, then any
external-store overlay `.env` selects, then the shared-monitoring overlay, and
`-f docker-compose.override.yml` last when you have one. Compose reads the override by itself only when no `-f` is given, so name it explicitly.

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

### Grafana now runs as your user

The production overlay runs Grafana as `${UID:-1000}:${GID:-1000}`, the owner of the 0600 files
in `secrets/`. The image's own user is UID 472, which cannot read them. A `grafana_data` volume
created before this change holds files owned by 472, and Grafana as your user cannot write
them: it exits with `permission denied` on `/var/lib/grafana/grafana.db`. A fresh volume needs
nothing.

To tell, with the stack's project name (the `.env` `COMPOSE_PROJECT_NAME`, else the checkout
directory name) in `<project>`:

```bash
docker run --rm --entrypoint ls -v <project>_grafana_data:/v grafana/grafana:13.2.4@sha256:dbbc38ea21103cefb50396340cd88afec779c7e070b0a7bf5a75b4ce46651d3f -ln /v
```

Owner `472` on `grafana.db` means the volume predates this change. Re-own it in place; this
changes ownership only and keeps every file:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop grafana
docker run --rm --user root --entrypoint chown -v <project>_grafana_data:/v \
  grafana/grafana:13.2.4@sha256:dbbc38ea21103cefb50396340cd88afec779c7e070b0a7bf5a75b4ce46651d3f \
  -R "$(id -u):$(id -g)" /v
just deploy
```

Run it against the `grafana_data` volume only. If the listing shows the volume is empty, it
holds nothing and `docker volume rm` of that one volume is also safe; never remove a volume
that has data. Dashboards are provisioned from `config/grafana/`, but users, preferences and
API tokens live in `grafana.db`.
### `/logs` is now a tmpfs (any host UID works)

The services run as `${UID}:${GID}`, but the named `*_logs` volumes that used to back `/logs`
initialised with the image user's ownership (UID 1000). Under any other host UID the consumers
crash-looped on `Permission denied: /logs/<service>.log`. Each service that writes `/logs` now
gets a size-capped tmpfs there, owned by `${UID}:${GID}` (Neo4j: its own `7474`), so no
`chown` of a volume is needed.

The extractor data volumes had the same defect: the extractor images create `/discogs-data` and
`/musicbrainz-data` owned by 1000:1000, which a fresh named volume inherits, so a non-1000 UID could
not write them. The one-shot `data-volume-init` now re-owns both volumes to `${UID}:${GID}` before
the extractors start, and the extractors mount them `nocopy` so Docker cannot re-apply the image's
ownership. It only changes the owner of mismatched entries (never deleting or `chmod`ing anything).

- On an existing UID 1000 install it is a no-op, except possibly re-owning a root-owned, empty
  `musicbrainz_data` root that phase 1 created before MusicBrainz ever ran.
- Recreating the extractors (they pick up the `nocopy` mount and the new dependency) belongs in the
  maintenance window; nothing else restarts.
- No `chown` of the data volumes (or of bind-backed directories for them) is needed.

- Logs are ephemeral inside containers. Read them with `docker compose logs <service>`
  (json-file driver, 10 MB x 3 per service) or through the telemetry stack.
- The old volumes (`neo4j_logs`, `api_logs`, `graphinator_logs`, ... twelve in all, prefixed
  with the Compose project name) are no longer mounted. `just deploy` never deletes volumes, so
  they stay on disk. Once you no longer need their old log files you may remove them yourself
  (`docker volume ls --filter name=_logs`, then `docker volume rm`), only after reviewing the list.
- `just deploy` still counts the three old MusicBrainz log volumes as evidence that MusicBrainz
  ran here. If you remove them, the MusicBrainz containers or the `loader_extraction_latch` row
  remain as evidence.
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
store are its operator's responsibility. The disk check skips it, and `postgres-exporter` and
`valkey-exporter` do not run: their scrape jobs get no target and the console's rows read disabled,
so nothing reports them down. To keep watching an external PostgreSQL through an exporter that
already watches it, set `EXTERNAL_POSTGRES_EXPORTER_ADDRESS=host:port`: the collector scrapes it
for the infrastructure dashboard and the console probes it as shared. The maintenance scripts that exec into the local `postgres` (`reset-password.sh`,
`provision-embedding-pipeline-login.sh`, `cleanup-implausible-years.sh`) do not apply; run
their SQL against your server instead. An override may still never rewire `POSTGRES_*`,
`VALKEY_*` or the store secrets: the supported mode is the only route
([the override check](#the-override-and-its-safety-check)). Secrets stay in encrypted storage,
and nothing is applied on a shared server without your explicit approval. Details:
[shared-services.md](../skills/groovemap-homelab/references/shared-services.md).

## Shared monitoring

A homelab that already runs Grafana, VictoriaMetrics, VictoriaTraces, node-exporter and cAdvisor
for other stacks can have GrooveMap report to them instead of running its own copies. Set
`GROOVEMAP_MONITORING=shared` in `.env`; `just deploy` adds `docker-compose.shared-monitoring.yml`
(after any external-store overlay) and records it in `COMPOSE_FILE`, exactly as for
[existing stores](#existing-postgresql-and-valkey). Valkey, Neo4j and RabbitMQ are unaffected,
and so is the collector: it stays the one place every GrooveMap service sends telemetry to.

What changes in shared mode:

- `grafana`, `victoria-metrics`, `victoria-traces`, `node-exporter` and `cadvisor` move into the
  never-enabled `local-monitoring` profile, so they do not start; nothing waits for them. Their
  volumes are left alone, and their host ports are neither published nor checked.
- The collector remote-writes metrics to the shared VictoriaMetrics, pushes spans to the shared
  VictoriaTraces and scrapes the shared node-exporter and cAdvisor, from the same
  `config/otel-collector.yaml` (it reads the endpoints from its environment). Its healthcheck
  validates that configuration in the same environment, so an endpoint it cannot use turns it
  unhealthy instead of silently dropping telemetry.
- The operations console probes the shared services and shows them as Shared with their
  endpoints, instead of UNREACHABLE for the services this stack no longer runs.
- Every GrooveMap series carries `service_namespace="groovemap"`, and every dashboard and alert
  query selects it, so GrooveMap's panels on the shared Grafana never mix in another stack's series.
- `just secrets-bootstrap` writes no Grafana admin password: this stack has no Grafana to sign in to.
- `just status` lists `Monitoring shared` under the recorded decisions and shows the five omitted
  services as `shared`, not `not installed`.

### Procedure

1. Find the shared services' addresses as seen from a container: names on a shared Docker network
   (such as `http://victoria-metrics:8428`), LAN addresses, or `host.docker.internal` for one
   published on this Docker host. The agent skill can discover them read-only.
2. Set them in `.env` (all variables in [configuration.md](configuration.md#shared-monitoring)):

   ```bash
   GROOVEMAP_MONITORING=shared
   SHARED_VICTORIA_METRICS_URL=http://victoria-metrics:8428   # required
   SHARED_VICTORIA_TRACES_URL=http://victoria-traces:10428    # required
   SHARED_GRAFANA_URL=http://grafana:3000                     # the console server's address for it
   SHARED_NODE_EXPORTER_ADDRESS=node-exporter:9100            # optional host:port
   SHARED_CADVISOR_ADDRESS=cadvisor:8080                      # optional host:port
   GRAFANA_URL=https://grafana.example.net                    # the shared Grafana, as browsers reach it
   ```

   With an existing PostgreSQL watched by a shared exporter, add
   `EXTERNAL_POSTGRES_EXPORTER_ADDRESS=pg-exporter:9187`; without it the console shows the
   PostgreSQL exporter as disabled. An unset node-exporter or cAdvisor address leaves that
   exporter unscraped (GrooveMap's Containers & host dashboard then has no host data) and the
   console lists it as shared but not configured.
3. If the shared services are containers on another Docker network, attach exactly `dashboard`
   and `otel-collector` to it in `docker-compose.override.yml`
   ([example](../skills/groovemap-homelab/assets/override-shared-monitoring-network.yml)). No other
   GrooveMap service talks to them. The override check accepts this.
4. Import GrooveMap's dashboards and alert rules into the shared Grafana:
   [Shared Grafana](observability.md#shared-grafana). Optionally create a Viewer token there for
   the console's alerts: [Grafana Viewer token](maintenance.md#on-a-shared-grafana).
5. Run `just deploy`. Before anything starts it checks every `SHARED_*` value's shape and stops
   on a missing or malformed one; it warns about what it cannot monitor and prints the shared
   Grafana instead of a local login.

**The console needs its configurable-targets release.** The reviewed operations-console is v0.3.0,
the first release that reads the `<NAME>_MODE` targets the overlay sets, so the omitted services show
as shared. An older console ignores them, which is harmless, but keeps probing the omitted services by
their compose names and shows them unreachable; `just deploy` warns when `.env` runs one
(`CONSOLE_TARGETS_FLOOR` in `scripts/check-images.py`).

**History.** Series written before this change lack the stack label on the exporter and collector
series, so the scoped infrastructure panels start at the upgrade (application series always had it).
Switching an existing deployment to shared monitoring does not move its local VictoriaMetrics or
Grafana data; the volumes stay, and switching back to `local` brings them back.

**Not supported yet:** shared endpoints that need authentication, and `https://` endpoints signed
by a private CA. Use an internal network both stacks share.

**Switching back.** Remove `GROOVEMAP_MONITORING` (or set it to `local`) and run `just deploy`:
the local services start again, deploy removes its `COMPOSE_FILE`, and
`just secrets-bootstrap` creates the local Grafana admin password if it is missing.

## Lux upgrade from 6af13ec/160fe7d

A host that installed GrooveMap at deployment `6af13ec` (or `160fe7d`) with mixed stores (an
external PostgreSQL with TLS, a dedicated Valkey) and shared monitoring carried local
workarounds for eight failures. Every fix has regression coverage, with the mapping and Linux
smoke status tracked in the [failure-to-test table](testing-guide.md#lux-regressions-failure-to-regression-test),
including the SQL loader latch and graph completion-marker races found in CI. This procedure never
removes volumes or regenerates secrets. The optional late-import recovery below triggers a full
Discogs reprocess against the existing stores and requires explicit operator approval.

**Ansible owns the deployment checkout and its `.env` and override.** Make the release and
configuration changes in the Ansible-managed deployment; do not edit those managed files by hand
or run `env-init` in a way that overwrites Ansible-owned values. Keep the existing secrets and
host settings; `EXTERNAL_POSTGRES_SSLMODE` remains `verify-full`.

**Snapshot first; do not interrupt an active import.** Before changing the checkout or running an
update, have Ansible capture a read-only baseline: deployed commit, effective configuration and
image pins, current service/container identities, `just status`, the Discogs gate and queue state,
and `just recovery-manifest`. Keep the existing extractor and consumer image IDs until an operator
chooses a bounded rollout. Do not run a broad Ansible deployment or Compose update while the
import is active. In a bounded, operator-approved window, promote both Discogs consumers together:
`tableinator` (SQL loader) and `graphinator` (graph enricher) to their reviewed v0.4.0 pins. Keep
the extractor and all MusicBrainz image pins unchanged. Keep the old `*_logs` volumes in place; the
updated Compose files mount a per-service tmpfs at `/logs` and never delete those volumes. Apply the
mount change in the same bounded maintenance window after the import has completed, because Compose
recreates services when it applies the new mounts. Do not interrupt the active import to roll out
this change.

Remove the local mitigations from the Ansible role only after the updated checkout passes the
verification below:

- **The store-check network attach.** The example override
  (`skills/groovemap-homelab/assets/override-external-store-network.yml`) now attaches
  `store-check-postgres` and `store-check-valkey` along with the app clients. Take the example's
  service list; a check that cannot resolve the store now names the store, host, service and
  networks.
- **The collector config workaround.** The collector loads the read-only config file mounted at
  `/etc/otelcol-contrib/config.yaml`; its command and healthcheck both validate that same file, and
  endpoint values are substituted from the container environment. Drop any `configs.content` or
  healthcheck override.
- **A `uv` install.** Operator recipes (`deploy`, `env-init`, `discogs-import-status`,
  `status`, `recovery-manifest`, the embeddings steps) run on `python3` 3.10 or newer when `uv` is
  absent; exit codes 0, 3 and 2 pass through unchanged. `uv` is for maintainers.
- **`SOURCE_DUMP_ID` and `SOURCE_DUMP_DATE` values.** Rendering and `up` no longer need them; only
  `just embeddings-run` does.
- **Exporter and secret ownership workarounds.** Every service that reads a 0600 secret runs as the
  owner of `secrets/`, so a `chown`/`chmod` of the derived Valkey exporter password or a `user:`
  override on an exporter is no longer needed.
- **A hand-written Discogs latch or a gate you stopped waiting on.** discogs-sql-loader v0.3.0
  did not write the completion latch. v0.4.0
  and the reviewed v0.4.1 (`sha256:9a1bc0108afadfc945606098bde4950bc42b53148bf3e9ca07c50f71feea669f`) write it when they
  receives the extraction-complete signals. It cannot recreate a missing latch if v0.3.0 already
  acknowledged those signals; an empty queue is not evidence of completion. Keep the gate
  fail-closed, do not restart the extractor or insert a latch row manually. The
  `discogs-graph-enricher` v0.3.0 image also has a CI-discovered concurrency race that can leave
  its durable completion marker at 2/4 after all queues drain. Promote the reviewed graph-enricher
  v0.5.0 pin (`sha256:0baf478b8203c19d095dda1ccf1cb7fbf7957ad763234da9c6534a660f18f79b`; v0.4.0 carried the fix) together
  with the SQL loader v0.4.1 pin; the combined Linux smoke passed the full gate on the v0.4.0
  pins (the run on v0.4.1 and v0.5.0 is recorded in the [promotion record](maintenance.md#promotion-of-database-schema-v061-discogs-sql-loader-v041-discogs-graph-enricher-v050-and-graph-explorer-v031-2026-11-04)). A pin-only upgrade
  still cannot recreate signals already acknowledged by v0.3.0. See
  [late Discogs recovery](#late-discogs-recovery).
- **A manual wait for the RabbitMQ DLQ initializer.** `up --wait` now succeeds: the queue services
  depend on `rabbitmq-dlq-policy-init` completing successfully.

**New `.env` keys** (all optional; see [Configuration](configuration.md)):

| Key | Purpose |
| --- | --- |
| `GROOVEMAP_MONITORING` | `local` (default) or `shared`; see [Shared monitoring](#shared-monitoring) |
| `SHARED_VICTORIA_METRICS_URL`, `SHARED_VICTORIA_TRACES_URL` | Required with `shared`: where the collector writes |
| `SHARED_GRAFANA_URL`, `SHARED_NODE_EXPORTER_ADDRESS`, `SHARED_CADVISOR_ADDRESS` | The console's and collector's view of the shared services |
| `EXTERNAL_POSTGRES_EXPORTER_ADDRESS` | `host:port` of the exporter that watches an external PostgreSQL |

For an unmanaged deployment, `just env-init` appends any missing keys under a comment and keeps
every other line. On lux, add needed values through Ansible's managed configuration; do not run
`env-init` against the Ansible-owned `.env`.

**Verify.**

1. Have Ansible capture the read-only config and identity baseline above, then update the
   deployment checkout. Preserve the existing `.env`, override and secrets; do not regenerate
   secrets or replace host-specific values.
2. Choose and approve a bounded rollout of both Discogs consumers: `tableinator` and `graphinator`
   at the reviewed v0.4.0 pins. Keep the extractor and MusicBrainz pins unchanged, and do not run
   the full deployment while Discogs is active.
3. After the import is complete and the operator chooses the maintenance window, apply the
   remaining deployment changes through Ansible. Preflight reports each effective store and
   monitoring decision and stops before changing anything on a setting it cannot honour.
4. `just status` (read-only; exit 0, or 2 when Docker or a setting cannot be read) lists the
   effective store and monitoring decisions with their sources, the services that are shared or
   external rather than missing, the reviewed image digests and the Discogs gate. Exit 3 from
   `just discogs-import-status` means the import is still in progress, not an error.
5. Check the Lux gate with `just discogs-import-status --prod -f docker-compose.override.yml`.
   Exit 0 means complete, exit 3 means importing, and exit 2 means the result cannot be
   determined. Exit 3 is normal progress; exit 2 is not evidence of completion, so keep
   MusicBrainz gated and report the problem.
6. If v0.3.0 already acknowledged the extraction-complete signals, or the graph marker is partial
   despite drained queues, keep the gate closed. Do not restart the extractor or insert a latch or
   marker row. Follow the operator-controlled [late Discogs recovery](#late-discogs-recovery)
   procedure only after its preconditions are met.
7. `just recovery-manifest`, then keep the listed files in an encrypted bundle, and check the
   [shared PostgreSQL checklist](maintenance.md#shared-postgresql-checklist).

### Late Discogs recovery

This is an operator-controlled **full new Discogs pass**, not a latch repair or a replay of only
the missing terminal messages. Use it only after a read-only baseline is captured, the recovery
bundle is encrypted, storage and database headroom are confirmed, the extractor is idle, the new
loader and graph-enricher consumers are healthy, and the operator explicitly approves the write.
Ansible remains the source of truth for the checkout, `.env`, and override. Keep MusicBrainz
disabled throughout.

Use the authenticated Catalog API admin endpoint `POST /api/admin/extractions/trigger` with the
JSON body `{"source":"discogs"}`. Catalog API v0.6.0 routes this through the supported
`discogs-ingestion` trigger with `force_reprocess=true`, records the extraction and audit entry,
and returns `202` when accepted. The producer returns `409` if an extraction is already running;
stop and re-check state instead of retrying concurrently. Do not call container restart commands,
write latch or marker rows, replay broker messages manually, or delete volumes to manufacture a
fresh run.

A forced reprocess creates new producer state and may download and process the selected dump
again. It is not a queue-only replay and its duration is not bounded by the missing-latch failure.
After it finishes, verify the single SQL latch row for the selected version contains all four
signals (`artists`, `labels`, `masters`, `releases`) and a non-null `refreshed_at`, all four durable
graph completion markers are present, and queues are drained. Then require
`just discogs-import-status --prod -f docker-compose.override.yml` to exit 0 before enabling
MusicBrainz. Exit 2 or 3 keeps MusicBrainz off and the gate authoritative.

A `grafana_data` volume created earlier needs the one-time re-own in
[Grafana now runs as your user](#grafana-now-runs-as-your-user). Existing `*_logs` volumes are
left untouched; the updated Compose files use UID-owned `/logs` tmpfs after the bounded deploy.

## Lux upgrade: schema-init v0.6.1, SQL loader v0.4.1, graph enricher v0.5.0

Applies to the stalled first Discogs load. It extends the bounded `tableinator` plus `graphinator`
window described in [Lux upgrade from 6af13ec/160fe7d](#lux-upgrade-from-6af13ec160fe7d). It needs
explicit operator approval for the window and, separately, for the homelab re-pin. Nothing here
has been run on lux by this repository.

**Read first.**

- **Do not run the homelab GrooveMap playbook for this window.** After it moves the checkout it
  runs `scripts/env-init.py`, which rewrites *every* `*_IMAGE` line to this repository's reviewed
  digests (there is no per-image setting), and then `bash scripts/deploy.sh --no-wait --no-admin`,
  a full production `up`. That would re-run `schema-init` with `tableinator` still writing and
  recreate every service whose rendered configuration differs. The steps below move the checkout
  and `.env` by hand and start nothing until the stopped-writer steps. The playbook's
  `groovemap_ref` (in `playbooks/deploy-groovemap.yml`) must be advanced to the merged commit
  before the playbook runs again; otherwise it checks the old commit out again and `env-init`
  moves the pins back. That re-pin is a separate homelab change and approval.
- `schema-init` `v0.6.1` must run with the writers stopped, and **no older `schema-init` may run
  afterwards**. There is no schema rollback: back out a consumer by its pin, never by running
  `schema-init` `v0.5.0` or `v0.6.0` again. Take the recovery bundle (`just recovery-manifest`)
  before the window.
- **`tableinator` is the only writer to stop** on lux (`musicbrainz-sql-loader` is not running
  there). `api`, `insights` and `dashboard` only read these tables; their `depends_on: schema-init`
  matters only when they start, so leave them running. Readers wait briefly during each
  `ACCESS EXCLUSIVE` key swap. database-schema's
  [Upgrading an existing database](https://github.com/groovemap-music/database-schema/blob/main/docs/architecture.md#upgrading-an-existing-database)
  puts the swap at minutes on a partly loaded database and tens of minutes on the full 147M-row
  tables; each swap is one guarded transaction per relation that commits or leaves the old key
  whole, and a batch the old key refused is still on the queue.
- **Do not run the window between 08:00 and 10:00 UTC.** The nightly `pg-backup` and
  `groovemap-ops` dumps hold locks that would block the swap and queue readers behind it. Before
  step 3, confirm no dump is running (see step 2).
- **Do not run `just deploy`, a plain `docker compose up`, or the playbook during the window.**
  Compose recreates every service whose rendered configuration differs and starts an exited
  one-shot such as `schema-init` again on every `up`. `up` with a named service and `--no-deps`
  touches only what you name. Recreating a service does not recreate its dependents (checked with
  Compose 5.6.1), so a plain `up` would not cascade from `schema-init`, but it would run it while
  `tableinator` is up.
- **Do not restart the extractor and do not recreate unrelated services.** Record the extractor's
  container ID and start time before, and compare after.
- `explore` (graph-explorer `v0.3.1`) is recreated in the window too. It only reads, and it does not
  touch the load.

**Commands.** Run them as `datum` in `/opt/groovemap`. Lux sets `COMPOSE_FILE` in `.env` to
`docker-compose.yml:docker-compose.prod.yml:docker-compose.external-postgres.yml:docker-compose.override.yml`
(no shared-monitoring overlay), so use plain `docker compose` and write no `-f` list.
`just discogs-import-status --prod -f docker-compose.override.yml` resolves to the same four files.

1. First save the current render, `docker compose config > before.yml`. Then move the checkout and
   `.env` without starting anything: `git fetch origin && git checkout --detach <merge commit>`, `python3 scripts/env-init.py`
   (rewrites only the `*_IMAGE` lines, keeps the previous file as `.env.<timestamp>.bak`, starts
   nothing), then `docker compose config > after.yml` and `diff before.yml after.yml`. Lux's checkout is at `568dc3a`. Between
   `568dc3a` and this change no Compose file, `.env.example` or `scripts/env-init.py` changed:
   the differences are the three image pins plus the publication-hygiene scan, `pyproject.toml`
   and docs. So the rendered diff must show `image:` changes on exactly `schema-init`,
   `tableinator`, `graphinator` and `explore`. If any other service differs (for example because lux's
   `.env` was not at `568dc3a`'s digests), stop and ask: the next steps would not touch it, but a
   later `up` would. Also record `docker ps --format '{{.Names}} {{.ID}} {{.Image}}' | sort > before.txt`,
   the extractor's `StartedAt`, and `just discogs-import-status --prod -f docker-compose.override.yml --json`.
2. Pick a time outside 08:00-10:00 UTC and check no dump holds a lock:
   `SELECT pid, usename, application_name, state, query_start, left(query, 80) FROM pg_stat_activity WHERE datname = 'groovemap' AND (query ILIKE '%pg_dump%' OR application_name ILIKE '%dump%' OR backend_type = 'client backend' AND state <> 'idle');`
   against the shared PostgreSQL, with the operator's read-only access. Only `groovemap-*` readers
   should appear. A running `pg_dump` (the `pg-backup` or `groovemap-ops` job) means wait.
3. Stop the writer: `docker compose stop tableinator`.
4. Run `schema-init` once and require exit 0:
   `docker compose up --no-deps --exit-code-from schema-init schema-init; echo "schema-init exit=$?"`.
   Do not continue on any other value, and never run an older image to retry.
   **If it blocks** (no output for several minutes, and the database-schema estimate above does
   not explain it): look at `pg_stat_activity` and `pg_locks` for the blocker. Do not terminate a
   dump or its backend. Run `docker stop groovemap-schema-init`, leave `tableinator` stopped (its queues hold the
   messages), and retry step 4 after the dump ends; only then continue to step 5. Each relation's swap commits or leaves the
   old key whole, and `schema-init` is idempotent (a converged database skips the swap), so a
   retry resumes safely.
5. Start the loader on the new pin: `docker compose up -d --no-deps tableinator`.
6. Recreate the enricher and Explore: `docker compose up -d --no-deps graphinator explore`.
7. Verify the recreation was limited: `docker ps --format '{{.Names}} {{.ID}} {{.Image}}' | sort | diff before.txt -`
   shows changed IDs for `groovemap-schema-init`, `groovemap-tableinator`, `groovemap-graphinator` and `groovemap-explore`
   only. The extractor's ID and `StartedAt` are unchanged, and `api`, `insights` and `dashboard`
   were not recreated.

**Checks that the load has resumed.**

- Queue depths fall: `just discogs-import-status --prod -f docker-compose.override.yml` (exit 3
  while importing) lists the eight `groovemap-discogs-{graphinator,tableinator}-*` queues; ready
  and unacknowledged counts should fall between runs, with both consumers attached.
- Dead-letter queues: the same report prints messages in any `.dlq` queue as warnings. They must
  not grow after the window. A growing DLQ is a failure: stop and report it.
- Both new images are running: `docker inspect -f '{{.Config.Image}}' groovemap-tableinator groovemap-graphinator groovemap-explore`
  show the new digests; `just status` lists the reviewed pins.
- When the import finishes, `loader_extraction_latch` (`loader = 'discogs'`) has all four
  `signals` and `refreshed_at` set, all four Neo4j markers exist, queues are empty, and
  `just discogs-import-status --prod -f docker-compose.override.yml` exits 0. Exit 2 is not
  completion. Until it exits 0, keep MusicBrainz off.

**Rollback of a consumer.** Set its pin back to the digest in
[the promotion record](maintenance.md#promotion-of-database-schema-v061-discogs-sql-loader-v041-discogs-graph-enricher-v050-and-graph-explorer-v031-2026-11-04) and recreate only that service with
`up -d --no-deps`. database-schema states that the new shape serves the old loaders unchanged, so
an older service image keeps working (same section as above, "Rolling back is a code rollback").
That is the schema repository's statement; loader `v0.4.0` and enricher `v0.4.0` on schema
`v0.6.1` have not been run end to end here. Never run an older `schema-init`.

**After the SQL load finishes**, run the stub-name backfill from discogs-graph-enricher's
`docs/stub-name-backfill.md`. It is a separate operator step and is not run in the window.

The first full deploy after the import (the homelab playbook once its `groovemap_ref` is advanced,
or `just deploy`) starts `schema-init` again (the same `v0.6.1`, idempotent) and recreates any
service whose configuration changed; schedule it as the full-stack window the earlier section
describes, not during the load.

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
- **Shared monitoring needs operations-console v0.3.0 or newer for truthful health rows.** The
  reviewed set pins v0.3.0; an older console ignores the configurable targets and shows shared
  services as unreachable. `just deploy` warns below the reviewed floor; see
  [Shared monitoring](#shared-monitoring).
- **A late Discogs-consumer upgrade cannot reconstruct acknowledged completion signals.** If
  v0.3.0 acknowledged the extraction-complete events before v0.4.0 was deployed, the latch stays
  absent and the gate must remain closed until an operator approves a full new Discogs pass. Never
  insert the latch manually or restart the extractor to try to recreate it.
- **The reviewed `discogs-graph-enricher` v0.3.0 can lose two completion markers in a concurrent
  cache version reset.** Empty queues do not prove all four durable markers were written. The
  source fix is released as v0.4.0, and v0.5.0 (the reviewed pin,
  `sha256:0baf478b8203c19d095dda1ccf1cb7fbf7957ad763234da9c6534a660f18f79b`) includes it; the combined Linux
  smoke proved all four markers and drained queues for a fresh fixture pass. This does not recover
  an earlier acknowledged terminal signal.
- **An active first import is not a Compose-wide rollout window.** Take the read-only config and
  identity snapshot first, preserve the current extractor and MusicBrainz IDs, and use a bounded,
  operator-approved `tableinator` plus `graphinator` promotion. Schedule full-stack recreation for
  after the import; do not destroy or replace its data volumes. The UID-owned `/logs` tmpfs change
  remains separate until its dedicated bead lands.
- **MusicBrainz disk use is an estimate.**

## Backups and operations

For the regressions found installing on a real host and the tests that now prevent each, see the
[failure-to-regression-test table](testing-guide.md#lux-regressions-failure-to-regression-test).

Backups are environment-specific and must be tested: see
[Backups and restore drills](maintenance.md#backups-and-restore-drills) and
[Database resilience](database-resilience.md). Day-to-day operation is covered by
[Administration](admin-guide.md), [Monitoring](monitoring.md) and
[Troubleshooting](troubleshooting.md); routine updates by [Maintenance](maintenance.md).
An external PostgreSQL or Valkey is backed up by its own operator, not by this stack
([what becomes yours](#existing-postgresql-and-valkey)).

**Reusing a shared PostgreSQL does not add GrooveMap's database to its backups.** Check that the
backup's protected-database list includes it, which schemas are excluded (an excluded
`musicbrainz` schema is fine to keep, but a restore needs a MusicBrainz re-import, a long job that
is unmeasured until `gm-deployment-vc5s`) and that restore verification covers it. The
[shared PostgreSQL checklist](maintenance.md#shared-postgresql-checklist) has the questions.
`just recovery-manifest` lists the files to keep in an encrypted bundle (SOPS/age) with the
generated secrets: see [Recovery bundle](maintenance.md#recovery-bundle). Never reset existing
secrets, and never purge keys or volumes after a failed staging.
