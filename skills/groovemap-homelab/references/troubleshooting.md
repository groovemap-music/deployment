# Troubleshooting

In the commands below, `gm` stands for
`docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml`,
run in the checkout. Leave out the override when there is none.

Never run `down -v`, `docker volume rm` or `docker system prune --volumes` to "start fresh"
without the user's explicit approval. They delete the loaded data, and the first load takes
days to rebuild.

## `just deploy` exit codes

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | Done, or phase 1 started with `--no-wait` | Continue with SKILL.md step 6 |
| 1 | A step failed | Read the last lines, then `gm ps` and `gm logs --tail=100 <service>` |
| 2 | Bad arguments | Check `just deploy --help` |
| 3 | A host check or the override check stopped it before anything started | Fix each `[stop]` line and re-run |
| 130 | Interrupted while waiting | Nothing is lost; the import keeps running. Re-run `just deploy` |

## Host and override checks (exit 3)

- **"Port N (...) is already in use":** set the named `*_HOST_PORT` variable in `.env` to a
  free port. `--force` doesn't override a port conflict.
- **"linux/amd64 only" on arm64:** explain the emulation cost to the user. With their consent,
  re-run with `--yes`. On Linux, register binfmt first (deploy prints the command).
- **"Only N GB free under …":** the named disk is short for the volumes listed on it; see [storage.md](storage.md#the-disk-check-when-data-lives-elsewhere).
- **"Docker has about N GB of memory":** on Docker Desktop, raise the VM's memory. Elsewhere, the
  host is below the minimum. Going ahead with `--force` is the user's call, and it will be slow.
- **"docker-compose.override.yml changes …":** remove that key from the override. The hint line
  says which `.env` setting to use instead ([override.md](override.md#what-deploy-rejects)).
- **"docker compose could not read the stack with docker-compose.override.yml":** a YAML or
  merge error. Run `gm config --quiet` to see it.

## Starting the stack

- **`network proxy declared as external, but could not be found`:** the override's external
  network name doesn't match `docker network ls`. Set `name:` under the network to the real
  name.
- **`Pool overlaps with other one on this address space`:** another network uses
  `172.20.0.0/16`. Change the subnet ([override.md](override.md#subnet)).
- **A container keeps restarting with `permission denied` on a bind-mounted directory:** fix
  the directory's ownership ([storage.md](storage.md#a-move-the-large-volumes-fresh-install-preferred)).
- **`APP_BASE_URL must be set to the public frontend origin`:** `.env` lacks `APP_BASE_URL`.
  Set it (deploy fills a LAN default only when it runs its configure step).
- **A service is `unhealthy`:** `gm logs --tail=200 <service>`. Dependencies start in order
  (`schema-init` must complete before the loaders and the API), so look at the first service
  that failed.

## Existing stores, shared monitoring and one-shots

- **`store-check-postgres` or `store-check-valkey` cannot resolve the store host:** that check
  container is not on the network the store lives on. The message names the store, host, service
  and networks. Attach every client of the store, the check included, as in
  `assets/override-external-store-network.yml` ([override.md](override.md#attaching-to-an-existing-stores-network)).
  A refusal by the server itself (authentication, TLS, version, pooling) is not a network problem.
- **A service crash-loops with `permission denied` on `/run/secrets/...`:** it is not running as
  the owner of the 0600 secret files. Do not loosen file modes or add a `user:` in the override;
  find which secret was written by another account (`ls -ln secrets/`) and fix its owner.
- **`up --wait` fails on a service that exited 0:** a one-shot (such as `rabbitmq-dlq-policy-init`)
  without a dependent using `service_completed_successfully`. The repository's Compose files have
  them; an override must not remove them.
- **The console shows UNREACHABLE for shared monitoring services or the PostgreSQL exporter:** check
  the `SHARED_*` values and `EXTERNAL_POSTGRES_EXPORTER_ADDRESS` in `.env` and that `dashboard`
  and `otel-collector` are attached to the monitoring network
  (`assets/override-shared-monitoring-network.yml`).
- **An operator recipe says `uv: command not found`:** it should not; operator recipes run on
  python3 3.10 or newer. Update the checkout. Only maintainer recipes (`just check`) need `uv`.
- **The gate stays in progress after every queue drained:** empty queues are not completion
  evidence. Keep MusicBrainz disabled and check the effective image pins plus all four SQL latch
  records and four graph completion markers. discogs-sql-loader v0.3.0 did not write its latch;
  discogs-graph-enricher v0.3.0 also has a CI-discovered race that can leave two graph markers
  missing after a concurrent cache-version reset. The source fix is merged into its source epic,
  but a reviewed graph image is not published yet.
- **A completion latch or graph marker is missing:** do not restart the extractor, write a marker,
  or replay broker messages manually. After a reviewed graph-enricher release and healthy new
  consumers are deployed, recovery requires the operator's approval, a read-only baseline, an
  encrypted recovery bundle and confirmed database/storage headroom. The supported option is a
  full new Discogs reprocess through the authenticated Catalog API admin endpoint
  `POST /api/admin/extractions/trigger` with `{"source":"discogs"}`; it may download and process
  the selected dump again. A `202` means accepted; `409` means the producer is already running,
  so re-check instead of retrying. Keep MusicBrainz off until the pass finishes, all four SQL
  latch records and graph markers are fresh, the queues are drained and the gate exits 0. See
  [late Discogs recovery](../../../docs/homelab.md#late-discogs-recovery).
- **`just status`** (read-only) shows per service state, the gate, the recorded decisions and any
  image that is not the reviewed digest.

## Through the proxy

- **502, 504 or "Bad gateway" from Traefik:** `traefik.docker.network` is missing or names the
  wrong network, or `loadbalancer.server.port` is wrong. Check that the container is on the
  proxy's network with
  `docker inspect groovemap-explore --format '{{json .NetworkSettings.Networks}}'`.
- **404 from Traefik:** the router isn't loaded. Check `traefik.enable`, the entrypoint name,
  and Traefik's own logs.
- **Console sign-in returns to the login form:** the console is reached over plain `http://`
  (the `Secure` cookie is dropped; fix it with a TLS proxy, or on a trusted LAN only set
  `GROOVEMAP_ALLOW_INSECURE_CONSOLE=true` in `.env` and recreate `dashboard`), the proxy strips `Cookie` or `Set-Cookie`, or the `Host`
  header is rewritten. See the console requirements in [reverse-proxy.md](reverse-proxy.md).
- **Console loads but live updates never arrive:** `/ws` isn't forwarded with `Upgrade`. In
  NPM, turn on Websockets Support. In nginx, add the `/ws` location.
- **Password-reset emails link to `localhost` or a LAN address:** `APP_BASE_URL` isn't the
  public Explore origin. Fix it in `.env` and run `just deploy` again.
- **Console links to Grafana open `localhost:3000`:** set `GRAFANA_URL` to the browser-facing
  Grafana URL.

## The first load

- **Checking progress:** `just discogs-import-status --prod -f docker-compose.override.yml`
  reports each completion signal (PostgreSQL, Neo4j, RabbitMQ queues). Exit 0 means complete,
  3 in progress, 2 cannot determine.
- **Exit 2 (cannot determine):** usually the stack isn't running or a store isn't answering.
  Run `gm ps` and check `postgres`, `neo4j` and `rabbitmq`.
- **Slow import:** expect about two days on the reference hardware, longer on smaller or
  emulated hosts. Watch queue depths in the operations console or Grafana. A queue that drains
  slowly is still progressing.
- **MusicBrainz didn't start after the import:** something has to run `just deploy` again. It
  turns the profile on when the gate reports complete. Never add `musicbrainz` to
  `COMPOSE_PROFILES` by hand on a first load.
