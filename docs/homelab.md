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
  the distro's `python3` is enough. mise, `just` and `uv` are optional maintainer tooling; with
  `uv` installed, the script uses it instead of `python3`. Set `DEPLOY_PYTHON` to pick another
  interpreter.
- A host that meets [the sizing minimums](#hardware-and-sizing).
- Free host ports for the services you publish (see [Ports and bind addresses](#ports-and-bind-addresses)).
  Deploy refuses to start if a configured port is taken by something other than this stack.
- Network access to GitHub Container Registry (several GB of images) and, for the first load,
  to the Discogs and MusicBrainz dump hosts.

## Hardware and sizing

`just deploy` enforces these minimums and warns below the recommended sizes. `--force` goes
ahead anyway. The values come from `scripts/deploy_helpers.py`.

| Check | Minimum | Recommended |
| --- | --- | --- |
| Memory for Docker, production | 16 GB | 32 GB |
| Memory for Docker, `--dev` | 8 GB | 16 GB |
| Free disk on Docker's data root, new install | 100 GB | 200 GB |

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

The disk preflight measures only Docker's data root. If you move volumes elsewhere with the
override (see [Data on a large disk](#data-on-a-large-disk)), check the target disk yourself.

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

### Later cycles

After the first load the order cannot be strictly enforced: MusicBrainz publishes twice weekly
and Discogs monthly, so new MusicBrainz links keep arriving ahead of their Discogs targets.
Today you run the manual re-attach and projection after each Discogs import
([maintenance](maintenance.md#post-import-identity-maintenance)). Automatic re-attachment
ships in catalog-api v0.5.0. That version is published but not yet promoted in this repository;
the promotion is tracked as gm-deployment-ew9k.

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

Production is always `-f docker-compose.yml -f docker-compose.prod.yml`, and
`-f docker-compose.override.yml` last when you have one. Compose reads the override by itself
only when no `-f` is given, so name it explicitly.

### Profiles

| Profile | Services | When |
| --- | --- | --- |
| `musicbrainz` | `extractor-musicbrainz`, `brainzgraphinator`, `brainztableinator` | After the Discogs import; `just deploy` sets `COMPOSE_PROFILES=musicbrainz` in `.env` |
| `jobs` | `embeddings` | Run on demand ([monthly embedding refresh](maintenance.md#monthly-embedding-refresh)) |

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
> run `just deploy`, which detects existing MusicBrainz containers or volumes and sets it
> for you. Otherwise a plain `docker compose up -d` no longer manages the MusicBrainz services.

To move to newly reviewed image digests, pull this repository and refresh `.env`. Be aware that
`just env-init --force` regenerates `.env` from the template, keeping the old file as a
timestamped backup (`.env.<timestamp>.bak`), and so drops host-specific values: `APP_BASE_URL`,
bind addresses, port remaps, Neo4j sizing. The groovemap-homelab skill carries them across with
`skills/groovemap-homelab/scripts/carry_env.py`: run from the checkout right after the pull,

```bash
git rev-parse HEAD                         # note this before pulling
git pull
just env-init --force
python3 skills/groovemap-homelab/scripts/carry_env.py --since <commit before the pull> .env.<timestamp>.bak
```

It copies values that differ from the old template and never the `*_IMAGE` digests. A built-in
merge mode for `just env-init` is coming; until then use this flow or re-apply the values by hand.
Then run `just deploy` to bring the stack up to date.

## Known limitations

These describe current behavior. A follow-up is planned; no dates are promised.

- **Console sign-in needs HTTPS.** The operations console's session cookie is Secure in the
  production overlay (`SESSION_COOKIE_SECURE` is reset there). Over plain http on a LAN IP,
  sign-in silently returns to the login form. Put a TLS reverse proxy in front of the console
  ([details](configuration.md#console-session-cookie-and-https)). Do not work around it by
  turning the opt-out on.
- **`just env-init --force` drops host-specific values.** See
  [Upgrading](#upgrading-an-existing-deployment).
- **The disk preflight measures only Docker's data root.** See [Hardware and sizing](#hardware-and-sizing).
- **Later cycles re-attach by hand** until catalog-api v0.5.0 is promoted. See
  [Later cycles](#later-cycles).
- **MusicBrainz disk use is an estimate.**

## Backups and operations

Backups are environment-specific and must be tested: see
[Backups and restore drills](maintenance.md#backups-and-restore-drills) and
[Database resilience](database-resilience.md). Day-to-day operation is covered by
[Administration](admin-guide.md), [Monitoring](monitoring.md) and
[Troubleshooting](troubleshooting.md); routine updates by [Maintenance](maintenance.md).
