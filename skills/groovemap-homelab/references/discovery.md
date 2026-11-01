# Discovering the host (read-only)

Every command here only reads. Run what applies to the host's OS, and skip anything that needs
`sudo` unless the user agrees. If `DOCKER_HOST` or a non-default `docker context` points at a
remote engine, the disk, port and process checks below describe the wrong machine: say so and
run them on the Docker host instead.

## Machine and Docker

```bash
uname -m; hostname
cat /etc/os-release 2>/dev/null | head -3        # Linux distribution (Unraid, TrueNAS, Synology DSM show here too)
docker version --format '{{.Server.Version}}'
docker compose version --short                   # must be v2; the production overlay uses !override and !reset
docker context show; echo "${DOCKER_HOST:-local}"
docker info --format 'arch={{.Architecture}} mem={{.MemTotal}} cpus={{.NCPU}} root={{.DockerRootDir}} os={{.OperatingSystem}}'
```

- `arch` other than `x86_64` means GrooveMap runs under amd64 emulation (hard rule 4).
  `just deploy` checks for binfmt emulation on Linux and explains how to register it.
- `mem` is what Docker can use (on Docker Desktop, the VM's share). Production needs 16 GB and
  recommends 32 GB.
- `os` containing `Docker Desktop` means the data lives in the VM's disk image, so free space on
  the host disk is not what counts.

## Disks

```bash
df -h "$(docker info --format '{{.DockerRootDir}}')"
df -h -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null || df -h   # candidate big disks
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null
command -v zpool >/dev/null && zpool list && zfs list -o name,used,avail,mountpoint
findmnt -t nfs,nfs4,cifs,fuse.mergerfs,fuse.shfs 2>/dev/null           # network or FUSE mounts
```

A new install needs about 100 GB for Discogs and about 200 GB with MusicBrainz (an estimate;
MusicBrainz is not measured yet). If the Docker data root is small and another local disk has
room, plan to move the large volumes ([storage.md](storage.md)). Avoid network and FUSE
filesystems for the databases.

## Containers, Compose projects and stack managers

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Label "com.docker.compose.project"}}'
docker compose ls --all                    # project names and the compose files they came from
```

The `CONFIG FILES` column of `docker compose ls` shows where stacks live and who manages them.
These locations are typical defaults, so confirm with the user:

| Path pattern | Usually managed by |
| --- | --- |
| `/opt/stacks/<name>/compose.yaml` | Dockge |
| `/data/compose/<id>/docker-compose.yml` (inside the Portainer container) | Portainer stacks |
| A path under Komodo's stack directory, or a Komodo `periphery` container running | Komodo |
| `~/…/<repo>/docker-compose.yml` next to other repos | plain Compose, possibly Ansible-templated |

Also look for:

```bash
docker ps --format '{{.Image}}' | grep -Ei 'portainer|dockge|komodo|watchtower|diun|ouroboros'
systemctl list-units --type=service --all 2>/dev/null | grep -Ei 'compose|docker-'
ls /etc/systemd/system 2>/dev/null | grep -Ei 'compose|stack'
```

Ask whether the user keeps homelab configuration in Ansible, Nix or a git repository. Auto
updaters such as Watchtower must not touch GrooveMap's containers: images are pinned by digest
and change only through `just env-init` ([stack-managers.md](stack-managers.md)).

## Existing PostgreSQL, PgBouncer, Valkey and Redis

From the `docker ps` output, note containers running `postgres`, `pgbouncer`, `redis`, `valkey`,
`keydb` or `dragonfly` images (Neo4j and RabbitMQ are always dedicated) and the host ports they
publish. These checks are read-only and never need a secret. Never print `docker inspect`
output that includes `.Config.Env`, never read a `secrets/` or `.env` file of another stack, and
never run anything that writes. Discovery is not authorization to reuse any of it
([shared-services.md](shared-services.md)).

Without credentials (the agent may run these):

```bash
# image, version tag/digest, status, networks and compose project, with no environment
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}\t{{.Networks}}' \
  | grep -Ei 'postgres|pgbouncer|redis|valkey|keydb|dragonfly'
docker inspect <name> --format '{{.Config.Image}} {{json .Config.Labels}} {{range $n, $_ := .NetworkSettings.Networks}}{{$n}} {{end}}'
docker inspect <name> --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{println}}{{end}}'   # volumes: persistence on disk?
docker inspect <name> --format '{{.HostConfig.Memory}} {{.HostConfig.RestartPolicy.Name}}'      # container memory limit (0 = none)
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.MemPerc}}' <name>                 # memory headroom
```

`docker inspect` without `--format` prints `Config.Env`, which usually holds passwords: always
pass `--format`, and leave `.Config.Env` out. To classify an instance, count what uses it:
the containers on its networks (`docker network inspect <net> --format '{{range .Containers}}{{.Name}} {{end}}'`)
and the compose projects of those containers. One application's project name or a single
consumer means **dedicated to another application**. Several unrelated projects, or a name or
label such as `shared`, means **shared infrastructure**. Report both as a judgment and let the
user correct it.

Needs the operator (ask them to run it in their own session, or to authorize read access
first; the agent never supplies or reads the password):

| What | Command (run by the operator) | Look for |
| --- | --- | --- |
| PostgreSQL version | `SELECT version();` | 18 required |
| Available extensions | `SELECT name FROM pg_available_extensions WHERE name IN ('pg_trgm','vector');` | both present |
| Connections | `SHOW max_connections;` and `SELECT count(*) FROM pg_stat_activity;` | headroom for GrooveMap's connection budget |
| Role privileges | `SELECT rolcreatedb, rolcreaterole, rolsuper FROM pg_roles WHERE rolname = current_user;` | what `schema-init` could use |
| PgBouncer pool mode | `SHOW CONFIG;` on the PgBouncer admin console (`pool_mode`) | `session` only |
| PgBouncer limits | `SHOW POOLS;` and `SHOW CONFIG;` (`default_pool_size`, `max_client_conn`) | headroom |
| Valkey version, persistence | `INFO server`, `INFO persistence` (`aof_enabled`) | supported version, AOF on |
| Valkey eviction, memory | `CONFIG GET maxmemory-policy`, `CONFIG GET maxmemory`, `INFO memory` | `noeviction`, headroom |
| Valkey auth | whether an unauthenticated `PING` is refused (the operator runs it; report only yes or no) | authentication on |
| Valkey other users | `INFO keyspace` | which databases hold keys |

Report auth as on or off and never its value. Connection counts and memory are a snapshot, not
a forecast: say so.

## Networks

```bash
docker network ls
docker network inspect $(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
ip route 2>/dev/null || netstat -rn
```

GrooveMap creates a bridge network `<project>_groovemap` on `172.20.0.0/16`. If an existing
network, a VPN route or the LAN overlaps it, plan a different subnet
([override.md](override.md#subnet)). Note the reverse proxy's network name for the override.

For an existing PostgreSQL or Valkey that runs as a container, note which network it is on
(`docker inspect <name> --format '{{range $n, $_ := .NetworkSettings.Networks}}{{$n}} {{end}}'`).
Every service that connects to it must join that network, including the one-shot store checks:
PostgreSQL `api`, `brainztableinator`, `dashboard`, `embeddings`, `insights`, `schema-init`, `store-check-postgres` and `tableinator`; Valkey `api`, `dashboard`, `insights` and `store-check-valkey`
([override.md](override.md#attaching-to-an-existing-stores-network)).

## Occupied ports

```bash
ss -ltn 2>/dev/null || netstat -an | grep LISTEN       # Linux / others
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null                # macOS
```

GrooveMap's defaults, and the `.env` variable that moves each one:

| Port | Variable | Bound to (production) |
| --- | --- | --- |
| 3000 | `GRAFANA_HOST_PORT` | `WEB_BIND_ADDRESS` |
| 8003 | `DASHBOARD_HOST_PORT` | `WEB_BIND_ADDRESS` |
| 8004, 8005 | `API_HOST_PORT`, `API_HEALTH_HOST_PORT` | `WEB_BIND_ADDRESS` |
| 8006, 8007 | `EXPLORE_HOST_PORT`, `EXPLORE_HEALTH_HOST_PORT` | `WEB_BIND_ADDRESS` |
| 5433 | `POSTGRES_HOST_PORT` | `DATA_BIND_ADDRESS` |
| 7474, 7687 | `NEO4J_HTTP_HOST_PORT`, `NEO4J_BOLT_HOST_PORT` | `DATA_BIND_ADDRESS` |
| 5672, 15672 | `RABBITMQ_AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT` | `DATA_BIND_ADDRESS` |
| 15692, 6379, 8428, 10428 | `RABBITMQ_METRICS_HOST_PORT`, `VALKEY_HOST_PORT`, `VICTORIA_METRICS_HOST_PORT`, `VICTORIA_TRACES_HOST_PORT` | `INTERNAL_BIND_ADDRESS` (`127.0.0.1`) |

A port is taken on loopback too if another service listens on `0.0.0.0`, so binding GrooveMap
to `127.0.0.1` does not avoid the collision; remap it. Port 3000 (Grafana, Gitea, AdGuard Home
and others) and 6379 (an existing Redis) collide most often. `just deploy`'s preflight checks
every configured port and names the variable to change.

## The reverse proxy

Find it among the containers and host services:

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -Ei 'traefik|caddy|nginx|proxy-manager|swag|haproxy|cloudflared|tailscale'
for s in caddy nginx traefik haproxy cloudflared tailscaled; do systemctl is-active --quiet "$s" 2>/dev/null && echo "host service: $s"; done
command -v caddy nginx tailscale cloudflared 2>/dev/null
```

Then learn how it finds backends. Copying the conventions of a service the user already proxies
is the most reliable approach:

- **Traefik:** `docker inspect <traefik> --format '{{json .Config.Cmd}} {{json .Args}}'` and its
  mounted static configuration (`docker inspect <traefik> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'`)
  name the entrypoints, the certificate resolver, `providers.docker.network` and
  `exposedByDefault`. `docker inspect <some proxied container> --format '{{json .Config.Labels}}'`
  shows the label pattern in use.
- **Caddy, nginx, SWAG, Nginx Proxy Manager:** find the configuration (Caddyfile,
  `/etc/nginx/conf.d`, SWAG's `proxy-confs`, or NPM's web UI). Note whether upstreams are
  container names on a shared network (`docker inspect <proxy> --format '{{json .NetworkSettings.Networks}}'`)
  or host addresses.
- **cloudflared:** a token-run tunnel (`tunnel run --token`) is configured in the Cloudflare
  dashboard. A `config.yml` with `ingress:` rules is configured locally.
- **Tailscale:** `tailscale status` and `tailscale serve status` show the machine's tailnet
  name and what is already served.

Record the DNS names the user owns and how certificates are issued (ACME through the proxy, a
wildcard certificate, Cloudflare, Tailscale HTTPS).
