# Storage

## Where the space goes

| Volume | Holds | Size |
| --- | --- | --- |
| `postgres_data` | PostgreSQL | about 48 GB with Discogs loaded |
| `neo4j_data` | Neo4j's graph | about 28 GB with Discogs loaded |
| `discogs_data` | the monthly Discogs dump the extractor keeps | about 11.3 GB |
| `musicbrainz_data` | the MusicBrainz dump | not measured yet |
| `victoria_metrics_data`, `victoria_traces_data`, `grafana_data`, `rabbitmq_data` | metrics, traces, broker | small to moderate |
| `*_logs`, `neo4j_import`, `neo4j_plugins`, `valkey_data` | logs and small state | small |

Images (several GB) and container layers always live under Docker's data root. `just deploy`
refuses a new install with less than 100 GB free on the data root and recommends 200 GB. The
200 GB figure assumes MusicBrainz is about as large as Discogs, which is an estimate.

Use local disks for the databases. Network filesystems (NFS, SMB) and FUSE layers (mergerfs,
Unraid's `/mnt/user`) are poor homes for PostgreSQL and Neo4j. On Unraid, use a pool or cache
path such as `/mnt/cache/...` or `/mnt/<pool>/...` instead **(general guidance, not measured
with GrooveMap)**.

## Choosing an approach

### A. Move the large volumes (fresh install, preferred)

`../assets/override-storage.yml` keeps each volume's name and points it at a directory with the
local driver's bind options. The services don't change, and deploy still recognizes the volumes
by name. Before the first start, and only after the user approves changes outside the checkout:

```bash
sudo mkdir -p /mnt/tank/groovemap/{postgres,neo4j,discogs,musicbrainz}
sudo chown 1000:1000 /mnt/tank/groovemap/discogs /mnt/tank/groovemap/musicbrainz
```

Ownership:

- `discogs_data` and `musicbrainz_data` are written by the extractors, which run as
  `${UID}:${GID}` from `.env` (default 1000:1000). Make those directories theirs.
- PostgreSQL, Neo4j and RabbitMQ use their official images' entrypoints, which start as root and
  set the ownership of their data directories. Leave those directories owned by root, then
  check the first start's logs for `permission denied`
  (`docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml logs postgres neo4j`).

Moving other volumes works the same way, but the four in the example are the ones that count.

### B. Move Docker's data root

If the user would rather move all of Docker to the big disk, set `"data-root"` in
`/etc/docker/daemon.json` and move the existing data while Docker is stopped, following
Docker's documentation. This affects every container on the host, so it is the user's decision
and needs their explicit approval. It also makes `just deploy`'s disk check measure the right
disk.

### C. Bind-mount by container path

A service-level `volumes:` entry such as `/mnt/tank/pg:/var/lib/postgresql/data` replaces the
named volume at that path (Compose merges service volumes by target). It works, but approach A
keeps the volume names that deploy and `docker volume ls` rely on, so prefer A.

## The disk check when data lives elsewhere

`just deploy`'s preflight measures free space on Docker's data root only. With approach A or C,
the data root can be small while the big disk has plenty of room, and preflight may stop with
"Only N GB free under /var/lib/docker". `--force` gets past that, but it also bypasses the
memory minimum. Use it only when all of these hold:

- the disk stop is the only `[stop]` line, and memory shows `[ok  ]` or `[warn]`;
- you have measured the big disk (`df -h /mnt/tank/groovemap`) and it has at least the
  recommended free space;
- the data root still has room for the images, about 20 GB.

Tell the user why you're passing `--force`.

## Existing deployments

Approach A doesn't move data. A named volume that already exists keeps its old location, and
Compose refuses or warns when the override's options differ. Recreating the volume deletes its
data. Moving a loaded deployment is a migration, so plan it with the user:

1. Back up first (`docs/maintenance.md` in the checkout, "Backups and restore drills").
2. Stop the stack with `down` (never `down -v`).
3. Copy each volume's contents to its new directory, preserving ownership. For example:
   `docker run --rm -v <project>_postgres_data:/from -v /mnt/tank/groovemap/postgres:/to alpine cp -a /from/. /to/`.
   List the real volume names with
   `docker volume ls --filter label=com.docker.compose.project=<project>`.
4. Remove the old volume (`docker volume rm`) only after the user confirms the copy, then add
   the override and run `just deploy`.

Each of these steps changes data outside the checkout and needs the user's explicit approval.
