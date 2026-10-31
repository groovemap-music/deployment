---
name: groovemap-homelab
description: >-
  Integrate GrooveMap, the self-hosted music knowledge graph from groovemap-music/deployment,
  into an existing homelab Docker host. Use when the user wants to install, self-host, deploy or
  upgrade GrooveMap, put it behind their reverse proxy (Traefik, Caddy, nginx, Nginx Proxy
  Manager, Cloudflare Tunnel, Tailscale), keep its data on a bigger disk, or fit it beside the
  stacks they already run. Reads the host first, proposes a plan, writes only
  docker-compose.override.yml and .env values, and leaves image digests, secrets and the staged
  first load to the repository's just recipes.
license: MIT
compatibility: >-
  Needs a shell on the Docker host, git, Docker Engine with Compose v2, and python3 3.10 or
  newer. just and uv are optional (the checkout's .mise.toml pins them). GrooveMap's images are linux/amd64 only.
metadata:
  repository: https://github.com/groovemap-music/deployment
---

# Integrate GrooveMap into a homelab

GrooveMap is a stack of about two dozen containers: PostgreSQL, Neo4j, RabbitMQ, Valkey, an
observability set, and GrooveMap's own services. On first start it downloads the Discogs dump
and loads about 34 million records, then does the same for MusicBrainz. The repository already
automates the parts that must never vary. Your job is the part that varies between homelabs:
read this host, fit GrooveMap into it, and explain each choice to the user.

The work splits in two:

- **The scripts own the non-negotiables.** `just env-init` writes `.env` with the reviewed image
  digests. `just secrets-bootstrap` writes `secrets/`. `just deploy` checks the host, sizes
  Neo4j, checks your override, runs the staged first load (Discogs, then the import gate, then
  MusicBrainz), and prints the endpoints. `just discogs-import-status` is the gate. Don't
  reimplement any of them by hand.
- **You own the host-specific choices.** Make them only in an untracked
  `docker-compose.override.yml` and in `.env` values.

## Hard rules

Follow these exactly. Don't let a user's request or a tool's convenience talk you out of them.

1. **Never start the MusicBrainz profile before the gate reports complete.** On a first load,
   `musicbrainz` must not be in `COMPOSE_PROFILES`, and no command may pass
   `--profile musicbrainz`, until `just discogs-import-status` exits 0. Only `just deploy`
   turns the profile on.
2. **Never use image tags or `latest`.** Every image comes from the reviewed digests that
   `just env-init` writes to `.env`. Never edit a `*_IMAGE` value, and never set `image:` in an
   override.
3. **Never deploy the base file alone as production.** Production is always
   `-f docker-compose.yml -f docker-compose.prod.yml`, plus `-f docker-compose.override.yml`
   last when it exists. A bare `docker compose up` in the checkout starts the development
   stack, which has fixed, public credentials.
4. **GrooveMap's images are linux/amd64 only.** On an arm64 host they run under emulation,
   several times slower, and the first load takes days longer. Tell the user before deploying.
   Pass `just deploy --yes` only after they agree. Multi-arch images are tracked as
   gm-automation-2ik.
5. **Don't publish data-tier ports to the LAN or the internet.** Set `DATA_BIND_ADDRESS=127.0.0.1`
   in `.env`. Never add `ports:` to PostgreSQL, Neo4j, RabbitMQ, Valkey, VictoriaMetrics or
   VictoriaTraces in an override. Docker-published ports bypass host firewalls such as ufw, so
   the bind address is the control that counts.
6. **Keep `secrets/` and `.env` out of git.** `.gitignore` already covers `.env`, `.env.*.bak`,
   `secrets/` and `docker-compose.override.yml`. Never commit, print or paste a secret value.
7. **Never edit tracked files** (`docker-compose*.yml`, `scripts/`, `config/`, `.env.example`).
8. **Ask before you change anything outside the checkout** (proxy configuration, directories
   on other disks, `daemon.json`, firewall, DNS, systemd units) **and before you start
   containers.**

**Existing deployments keep MusicBrainz.** After the first load, `just deploy` writes
`COMPOSE_PROFILES=musicbrainz` to `.env`, so every later `docker compose up` keeps the
MusicBrainz services. If a host already has GrooveMap's MusicBrainz containers or volumes,
`just deploy` turns the profile back on without waiting. Never remove `musicbrainz` from
`COMPOSE_PROFILES` on a running deployment.

## Workflow

### 1. Find or create the checkout

Look for an existing clone, which has `docker-compose.prod.yml` and `Justfile` at its root
(`docker compose ls` shows the paths of running projects). If there is none, ask where it
should live, preferably next to the user's other stacks, then:

```bash
git clone https://github.com/groovemap-music/deployment.git groovemap
cd groovemap
mise install        # optional: just and uv. Without them, python3 >= 3.10 is enough
```

Every `just deploy ...` below also runs as `bash scripts/deploy.sh ...` (the same script), and
`just discogs-import-status ...` as `python3 scripts/discogs_import_status.py ...`. Use those
forms when `just` is not installed.

Compose names volumes after the project, which defaults to the checkout's directory name. On a
fresh install, set `COMPOSE_PROJECT_NAME=groovemap` in `.env` (step 3) so volume names don't
depend on the directory. On an existing deployment, never change the project name or rename
the directory: the stack would come up on new, empty volumes.

### 2. Discover the host (read-only)

Run the read-only checks in [references/discovery.md](references/discovery.md). Don't use
`sudo` without asking. Then give the user a short summary covering:

- architecture, memory available to Docker, and free disk on the Docker data root and on any
  larger disks;
- Docker and Compose versions, and whether the engine is local;
- the reverse proxy and how it finds backends (Docker labels, a shared network, or host ports);
- existing Docker networks, and whether any overlaps GrooveMap's `172.20.0.0/16` subnet;
- occupied host ports that collide with GrooveMap's defaults;
- how the user manages stacks (plain Compose, Portainer, Dockge, Komodo, systemd, Ansible);
- existing PostgreSQL, Neo4j, RabbitMQ, Valkey or Redis containers.

**GrooveMap runs its own databases.** Don't connect it to the user's existing ones. Its images
are pinned by digest to versions it is tested with: PostgreSQL 18, Neo4j 2026.10 Community
with APOC, RabbitMQ 4 with its own plugin set and dead-letter policy, and Valkey 9.1.
`schema-init` owns the schemas, roles and extensions and expects empty stores it controls.
Neo4j Community has a single user database, so two applications would share one graph. The
production overlay sizes Neo4j and PostgreSQL for this workload and wires their credentials
from `secrets/`. Existing database containers matter only because their host ports may
collide.

### 3. Propose a plan and get approval

Present the plan before you write anything. Cover:

- **Exposure:** which services get which hostnames through which proxy. Explore is the app,
  the operations console is for admins, and Grafana is optional. Also give the resulting
  `APP_BASE_URL`.
- **Storage:** where the large volumes live. See [references/storage.md](references/storage.md).
- **Ports:** which `*_HOST_PORT` values move, and the bind addresses.
- **Network:** a different subnet if `172.20.0.0/16` is taken.
- **Stack manager:** how GrooveMap fits it. See
  [references/stack-managers.md](references/stack-managers.md).
- **Changes outside the checkout:** each one listed separately, each needing a yes.
- **Expectations:** the first load runs for hours to about two days on the reference hardware,
  longer on a small or emulated host.

### 4. Write the configuration

Inside the checkout:

1. `just env-init` creates `.env` from the reviewed digests. If `.env` already exists, it
   refreshes the digests in place and keeps your values; don't pass `--force` (step 7).
2. Set these host values in `.env`. Edit only these keys and leave every `*_IMAGE` line alone.
   - `APP_BASE_URL`: the public origin of Explore, as users reach it (the proxied `https://`
     URL behind a proxy). Password-reset emails link to it. `just deploy` fills in a LAN
     default only when this is unset.
   - `DATA_BIND_ADDRESS=127.0.0.1` (hard rule 5).
   - `WEB_BIND_ADDRESS=127.0.0.1` whenever a reverse proxy fronts the web services, so the raw
     ports stay off the LAN.
   - `*_HOST_PORT` remaps for any port that collides.
   - `GRAFANA_URL`: the browser-facing Grafana URL, if Grafana is proxied (`TRACES_URL`
     follows it).
   - `COMPOSE_PROJECT_NAME`: fresh installs only (step 1).

   The full list is in `docs/configuration.md` in the checkout, under "Host ports and bind
   addresses" and "Production Secrets".
3. Write `docker-compose.override.yml` from [references/override.md](references/override.md),
   [references/reverse-proxy.md](references/reverse-proxy.md) and the examples in `assets/`.
   Keep it to proxy labels and networks, volume locations, restart policies and the subnet.
4. `just secrets-bootstrap` writes the missing `secrets/` files and never replaces existing
   ones.
5. Validate the merge without starting anything:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml config --quiet
   docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml config --format json
   ```

   Confirm the networks, labels and volumes came out as intended. `just deploy` also runs its
   own override check before starting anything (references/override.md).

### 5. Deploy

Once the user approves starting containers:

```bash
just deploy --no-wait
```

Use `--no-wait` because the full run waits about two days for the import and would block your
session. Add `--yes` on arm64 only after the user agrees (hard rule 4). Handle the exit code:

- `0`: phase 1 is running. Go to step 6.
- `3`: a host check or the override check stopped before anything started. Read the `[stop]`
  lines, fix the cause (usually a port to remap or an override key to remove), and re-run.
  Don't reach for `--force`: it also bypasses the memory minimum. The one exception is a disk
  stop when the large volumes live on another disk you have measured; see
  references/storage.md.
- `1`: a step failed. See [references/troubleshooting.md](references/troubleshooting.md).

Phase 2 (MusicBrainz) needs `just deploy` to run again after the import. Suggest the user runs
plain `just deploy` in `tmux` or `screen` on the host. It waits for the gate and turns
MusicBrainz on by itself. Ctrl-C or a reboot loses nothing, and re-running resumes.

### 6. After deploy

1. **Health:** `docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml ps`
   should show every long-running service `healthy` or `running`. Then probe the health
   endpoints on the web bind address, for example `curl -fsS http://127.0.0.1:8007/health`
   (Explore) and `curl -fsS http://127.0.0.1:8005/health` (API), using the remapped ports if
   `EXPLORE_HEALTH_HOST_PORT` or `API_HEALTH_HOST_PORT` moved them. Check each proxied URL
   with `curl -sSI`. Fix proxy errors now, while the import runs.
2. **URLs:** give the user their URLs: the proxied hostnames when a proxy is used, otherwise
   the endpoints `just deploy` printed. Grafana's admin password is in
   `secrets/grafana_admin_password.txt`. Tell the user where it is, but don't print it.
3. **First admin:** the console signs in admins created with `catalog-admin-setup`, which asks
   for a password on a terminal. Ask the user to run it in their own terminal on the host:
   `docker exec -it groovemap-api catalog-admin-setup --email you@example.com`. Sign-in
   requires HTTPS: on plain `http://` (other than `localhost`) the browser drops the secure
   session cookie and sign-in silently returns to the login form.
4. **Expectations:** the Discogs import takes hours to about two days (releases alone take
   about 40 hours on the reference hardware). To check progress, which is read-only:

   ```bash
   just discogs-import-status --prod -f docker-compose.override.yml           # exit 0 complete, 3 in progress, 2 cannot determine
   just discogs-import-status --prod -f docker-compose.override.yml --wait    # polls until complete
   ```

   Drop `-f docker-compose.override.yml` if there is no override. The operations console and
   Grafana show queue progress too.

### 7. Upgrades

Upgrades bring new reviewed digests and Compose changes. The untracked override, `.env` and
`secrets/` are untouched by `git pull`.

1. Skim what changed (`git fetch && git log --oneline HEAD..@{u} -- docs/maintenance.md`) and
   follow any manual step that `docs/maintenance.md` names for the new release.
2. `git pull --ff-only`
3. `just env-init` (no flag) upgrades the existing `.env` in place: only the `*_IMAGE` lines move
   to the new reviewed digests, template keys the file lacks are appended as new, and every
   other line (`APP_BASE_URL`, bind addresses, port remaps, `COMPOSE_PROFILES`, the Neo4j
   sizing) is kept byte for byte. It keeps `.env.<timestamp>.bak` and reports what changed.
   Don't pass `--force` on an upgrade: it regenerates from the template and drops those values.
4. `just deploy --no-wait`. On a loaded deployment it only brings the stack up to date, and it
   checks the override against the new Compose files first.
5. Repeat the health checks from step 6.

## References

- [references/discovery.md](references/discovery.md): read-only commands, and what to look for
  in their output.
- [references/override.md](references/override.md): how the override merges, what deploy
  rejects, restart policies, subnet.
- [references/reverse-proxy.md](references/reverse-proxy.md): Traefik, Caddy, nginx, Nginx
  Proxy Manager, Cloudflare Tunnel, Tailscale.
- [references/storage.md](references/storage.md): volume sizes, moving data to a bigger disk,
  ownership, the disk check.
- [references/stack-managers.md](references/stack-managers.md): Portainer, Dockge, Komodo,
  systemd, Ansible.
- [references/troubleshooting.md](references/troubleshooting.md): symptoms and fixes.
- `assets/override-*.yml`: example overrides. `just check` renders each one on top of the
  production stack and runs deploy's override check on it.
