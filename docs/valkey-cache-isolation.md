# Keeping catalog caches out of the auth Valkey

`valkey` runs `noeviction` and holds auth state: revoked token ids (`revoked:jti:*`) and password
changes (`password_changed:*`) live only there. Catalog caches (Explore, trends, strata, search,
recommendations) share it, and their keys come from values the caller chooses. A filled cache makes
Valkey reject every memory-adding command, so an auth write can fail with `OOM command not allowed`.
`catalog-api` already caps how many keys each cache may hold (`api/cache_budget.py`); that bounds
the growth but leaves the cause, which is that the two kinds of data share one memory limit.

## Options compared

| Option | Verdict |
| --- | --- |
| A second **database** in the same Valkey | Does not help. `maxmemory` and `maxmemory-policy` are per server, so every database shares the cap and the policy. |
| Switch the shared instance to `allkeys-lru` | Unsafe. Eviction would drop revoked-token and password-change keys under pressure and silently re-validate revoked tokens. `tests/deploy/test_docker_compose_prod.py` pins `noeviction` for this reason. |
| `volatile-lru` on the shared instance | Unsafe for the same reason wherever an auth key has a TTL, and it leaves cache keys without a TTL unable to be evicted. |
| **A second instance** (`valkey-cache`, `allkeys-lru`, no persistence) | Chosen. The auth instance keeps `noeviction` and its own memory; the cache instance evicts and can fill without affecting it. |

## Proof

`tests/deploy/test_valkey_cache_isolation.py` runs real Valkey containers from the stack's pinned
image with the cap shrunk to 8 MB and writes 20,000 distinct 1 KB cache keys:

| | Cache writes rejected | Keys evicted | Auth write |
| --- | --- | --- | --- |
| One shared `noeviction` instance (today) | 14,488 of 20,000 | 0 | `OOM command not allowed` |
| Separate `allkeys-lru` cache instance | 0 of 20,000 | 14,505 | `OK`, earlier revoked key still present, 0 evictions on the auth instance |

## What is in this repository

`docker-compose.cache-isolation.yml` is an opt-in overlay, applied after `docker-compose.prod.yml`
and never by `just deploy`:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.cache-isolation.yml up -d valkey-cache api
```

- `valkey-cache`: `--maxmemory ${CATALOG_CACHE_MAXMEMORY:-256mb} --maxmemory-policy allkeys-lru`,
  `--save "" --appendonly no` (a cache is rebuilt, not restored), the same pinned image and
  password secret as `valkey`, not published on the host.
- `api` receives `CATALOG_CACHE_VALKEY_HOST=valkey-cache` and `CATALOG_CACHE_VALKEY_PORT=6379` and
  waits for the cache to be healthy.

## What catalog-api needs

The overlay changes nothing until `catalog-api` reads those two variables. It needs:

1. A second Valkey client built from `CATALOG_CACHE_VALKEY_HOST`/`PORT` (falling back to the existing
   client when they are unset, so a stack without the overlay behaves as today), using the same
   password file and key prefix.
2. The Explore, trends, strata, search and recommendation caches, and the budget index keys of
   `api/cache_budget.py`, using that client. Auth keys (`revoked:jti:*`, `password_changed:*`),
   the import-cache marker and sync state stay on the existing client.
3. Cache reads and writes treat the cache instance as best effort: a miss or an unavailable cache is
   served from the source, never an error.

Until then the overlay is inert for the application. The `catalog-api` change is a separate bead.

## Applying it on lux

Operator approval is required, and nothing here has been applied there. The host stays untouched
while the Discogs load runs: no restart of `valkey` is needed (the overlay only adds a container),
but adding it and recreating `api` interrupts the API, so wait for the load to finish.
