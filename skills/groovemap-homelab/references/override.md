# docker-compose.override.yml

Every host-specific choice that `.env` can't express goes in one untracked file,
`docker-compose.override.yml`, at the checkout's root. `.gitignore` already lists it. The
examples in `../assets/` are rendered on top of the production stack by `just check`, so they
are known to merge and to pass deploy's override check.

## How it is applied

Compose reads `docker-compose.override.yml` by itself only when no `-f` is given. Production
always names its files, so the override must come last:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml <command>
```

`just deploy` does this for you. When the file exists, deploy adds it to every Compose call and
to the import gate, and prints a status command that includes it. Anything you run by hand
(`ps`, `logs`, `exec`, `config`, `just discogs-import-status --prod -f docker-compose.override.yml`)
must name it too.

## What deploy rejects

Before anything starts, `just deploy` renders the stack with and without the override
(`config --format json`, every profile included) and stops with exit code 3 if the override
changes any of these on a GrooveMap service:

| Key | Why |
| --- | --- |
| `image`, `build` | Images come only from the reviewed digests in `.env`, never a tag or `latest` |
| `platform` | The images are linux/amd64 only |
| `container_name` | deploy finds the stack by its `groovemap-*` container names |
| `profiles` | Only deploy turns on `musicbrainz`, after the import gate reports complete |
| `ports`, `network_mode` on postgres, neo4j, rabbitmq, valkey, victoria-metrics, victoria-traces | Data-tier ports move only through `.env` bind addresses and `*_HOST_PORT` |
| `networks`, `labels`, `extra_hosts` on the same six data-tier services | Proxy networks, Traefik TCP routers and hosts entries would expose or redirect the data tier. Attach only `explore`, `dashboard` and `grafana` (or `api`) to a proxy network |

It also stops, with no opt-in, if the override points a service at an external PostgreSQL or
Valkey: a change on any service to `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`,
`POSTGRES_USERNAME`, `VALKEY_HOST`, `VALKEY_PORT`, `VALKEY_HEALTH_ADDRESS`, libpq's `PGHOST`, `PGHOSTADDR`, `PGPORT` or `PGSERVICE` (the store check and the gate use them), or their `_FILE` variants (set directly or through
`env_file`, which Compose inlines into the render), or to the source of a `postgres_*` or
`valkey_*` secret. The hint says an existing store is reached "only through the supported
external-stores mode" (`GROOVEMAP_POSTGRES` / `GROOVEMAP_VALKEY=external` in `.env`; see
[shared-services.md](shared-services.md)) and cites `gm-deployment-e1uh` and `docs/homelab.md`:
the deploy gate and the MusicBrainz latch probe follow that mode, so a service rewired in the
override would leave the gate watching a database nothing writes to. The mode's overlays sit in
the render on both sides of the comparison, so they never trip this rule.
`POSTGRES_HOST_PORT` and `VALKEY_HOST_PORT` (published host ports) are unaffected.

It also stops if the override removes a service, or redefines a top-level network that a
data-tier service uses (`name`, `external`, `driver`, `attachable`, `internal`, `driver_opts`):
that would move the data tier onto another network without touching any service. Changing the
`groovemap` network's `ipam` subnet stays allowed (`assets/override-network-restart.yml`).

The check denies what it does not understand. On a service that already exists it also
stops (no opt-in) when the override adds or changes any of these. Existing services are
checked against this denylist rather than a key allowlist, because overrides legitimately
change labels, networks, environment, restart and volumes on web-tier services:

| Key or mount | Why |
| --- | --- |
| `privileged`, `cap_add`, `devices`, `security_opt`, `device_cgroup_rules`, `sysctls`, `runtime`, `isolation`, `group_add` | Wider access to the host than the base file grants |
| `network_mode` (any service) | `host`, `service:` and `container:` share another network namespace |
| `pid`, `ipc`, `uts`, `cgroup`, `cgroup_parent`, `userns_mode`, `volumes_from` | Shares another container's or the host's namespaces or data |
| a bind mount of a sensitive host path | The Docker socket or the host's own files |
| a mount of a named volume that another service uses (or that no service uses) | Another service's data, such as `postgres_data` |

Paths are compared after leading slashes are collapsed and `.`/`..` resolved
(`//var/run/docker.sock` is the Docker socket). On a local engine symlinks are followed too, so
a link to a sensitive or relocated path is judged by where it leads; with a remote
`DOCKER_HOST` the paths are on another machine and symlinks are not resolved.

Volumes are judged by engine identity, not Compose key. A top-level volume with `name:` or
`external: true` that resolves to a stack volume (`<project>_postgres_data`) is that stack
volume, so mounting the alias into another service stops. A bind mount, or a volume
`driver_opts.device`, that is, contains or lies inside the host directory another service's
data was relocated to stops for non-owners. The override may not change `name`, `external` or
`driver` on a stack volume, and two services may not relocate data into overlapping
directories. `OVERRIDE_ALLOW_SERVICES` lifts the mount rules for an added service only.

A service may still move **its own** data volume: a bind mount at its data path, or `driver_opts`
on its top-level volume ([storage.md](storage.md)), as in `assets/override-storage.yml`. That
stays allowed unless the target is a sensitive host path.

Sensitive host paths: `/`; the Docker, containerd and podman sockets (`/var/run/docker.sock`,
`/run/docker.sock`, `/run/podman`, `/run/containerd`, `/var/run/...` equivalents and
`/run/user/<uid>/docker|podman|containerd`); `/var/lib/docker`, `/var/lib/containerd`,
`/var/lib/podman`, `/var/lib/containers`; `/proc`, `/sys`, `/dev`, `/etc`, `/root`, `/boot`;
anything under them; and any parent that contains one (`/var/lib`, `/run`). Paths are
compared in canonical form, and symlinks are followed on a local engine (see below).

### Services you add

A service that exists only in the override (a backup job, a tunnel sidecar) is allowed if:

- its `image` is pinned by digest (`repo@sha256:<64 hex digits>`), with no `build`. There is no opt-in for this.
- it sets only these keys: `image`, `command`, `entrypoint`, `environment`, `env_file`,
  `labels`, `networks`, `restart`, `healthcheck`, `logging`, `user`, `working_dir`,
  `read_only`, `tmpfs`, `init`, `stop_grace_period`, `deploy.resources`, `container_name`,
  `depends_on` and `volumes`. Anything else (`privileged`, `cap_add`, `devices`,
  `security_opt`, `volumes_from`, `pid`/`ipc`/`uts`/`cgroup`, ...) stops.
- its `volumes` are new named volumes, `tmpfs`, or host paths outside the sensitive list. It
  does not mount a stack volume (`postgres_data`, `neo4j_data`, ...) or the Docker socket.
- it publishes no `ports` and does not join the `groovemap` network (by network, by a renamed
  network, or through `network_mode: service:...`).

Name the service in `OVERRIDE_ALLOW_SERVICES` in `.env` (or the shell), comma-separated, to
lift every rule above except the digest pin and no `build`:
`OVERRIDE_ALLOW_SERVICES=pg-forward,backup`.

**A backup sidecar needs `OVERRIDE_ALLOW_SERVICES`.** Reading `postgres_data` or `neo4j_data`
(or a socket) is exactly what the check stops, so add the sidecar's name to the variable, keep
it digest-pinned, and mount the volumes read-only.

A new service that omits `networks` lands on the project's default network, a separate network
from `groovemap`, so it needs no opt-in. That only means no route over `groovemap`; it says
nothing about the host or LAN, which the key and mount rules cover. Listing a service is a decision to let it reach (or expose)
the stack, so a socat forwarder into postgres needs it. Each message names the service, the key or mount and the opt-in, for example `docker-compose.override.yml adds the pg-forward service and publishes ports`.

## Attaching to an existing store's network

With an external store (`GROOVEMAP_POSTGRES=external` or `GROOVEMAP_VALKEY=external`) that runs
as a container on its own Docker network, `EXTERNAL_POSTGRES_HOST` / `EXTERNAL_VALKEY_HOST` is a
name only that network resolves. Attach **every** service that connects to the store, not just
the application, as `../assets/override-external-store-network.yml` does:

- PostgreSQL clients: `api`, `brainztableinator`, `dashboard`, `embeddings`, `insights`, `schema-init`, `store-check-postgres` and `tableinator`. `store-check-postgres` is also where the Discogs gate and the
  MusicBrainz latch probe run `psql`, so leaving it out breaks the deploy check, the gate and the
  latch, even though the application connects.
- Valkey clients: `api`, `dashboard`, `insights` and `store-check-valkey`.

Adding `networks` to these services is allowed. The data-tier services (postgres, neo4j,
rabbitmq, valkey, victoria-metrics, victoria-traces) stay pinned, and changing a host, port or
secret still stops. If a check cannot resolve the host, `just deploy` stops with exit 3 and names
the store, host, service and the networks that service is on.

## Merge behavior

Checked with Docker Compose 5.6.1 by rendering the production stack with each example:

- **Service `networks`** merge by network name. Adding `proxy: {}` keeps the `groovemap`
  network and its aliases. Use the mapping form shown in the examples.
- **Service `labels`** merge by key. The base files set no labels on any service.
- **Service `volumes`** merge by container path. A bind mount to `/var/lib/postgresql/data`
  replaces the named volume at that path.
- **Top-level `volumes`** merge by key. Adding `driver_opts` to `postgres_data` keeps the
  volume's name and moves its location ([storage.md](storage.md)).
- **`restart`** and other scalars are replaced.
- **`!override`** replaces a whole node instead of merging it. **`!reset`** removes it.

Never edit `docker-compose.yml` or `docker-compose.prod.yml`.

## Use `.env` instead

These settings are interpolation inputs. Set them in `.env`, not the override:

- host ports: every `*_HOST_PORT`
- bind addresses: `WEB_BIND_ADDRESS`, `DATA_BIND_ADDRESS`, `INTERNAL_BIND_ADDRESS`
- URLs: `APP_BASE_URL`, `GRAFANA_URL`, `TRACES_URL`
- Neo4j sizing: `NEO4J_HEAP_SIZE`, `NEO4J_PAGECACHE_SIZE`, `NEO4J_MEMORY_LIMIT`,
  `NEO4J_TRANSACTION_MEMORY_MAX`. `just deploy` writes these from the machine's memory when
  they are unset.
- email sender: `RESEND_SENDER_EMAIL`, `RESEND_SENDER_NAME` (the API key is a secret file)
- `COMPOSE_PROJECT_NAME`, and `COMPOSE_PROFILES` (written only by deploy)

Hosts, data paths, log levels and consumer timings are literal Compose values. Change one only
when the user has a reason, with an `environment:` entry in the override.

## Restart policies

The production overlay sets `restart: always` on every long-running service. Docker then starts
the stack again after a reboot, so no systemd unit is needed. If the user prefers that services
they stopped by hand stay stopped, set `unless-stopped` per service. Never set
`deploy.restart_policy`: the production overlay forbids it, because Compose lets it override
`restart:`.

```yaml
services:
  grafana:
    restart: unless-stopped
```

## Subnet

The base file pins the `groovemap` network to `172.20.0.0/16`. If that overlaps another
network or a route, replace it as `../assets/override-network-restart.yml` does:

```yaml
networks:
  groovemap:
    ipam: !override
      config:
        - subnet: 172.28.0.0/16
```

A subnet takes effect when the network is created. On a running stack, stop it with `down`
(never `down -v`, which deletes the volumes) and run `just deploy` again.

## Only one GrooveMap per Docker host

Container names are fixed (`groovemap-postgres`, `groovemap-explore`, and so on). A second
copy of the stack on the same engine would collide, whatever its project name.

## Validate

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml config --format json \
  | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]; [print(n, sorted(s[n].get("networks", {})), s[n].get("labels", {})) for n in ("explore", "dashboard", "grafana")]'
```

An external network that doesn't exist passes `config` but fails at `up`. Check
`docker network ls` for the exact name.
