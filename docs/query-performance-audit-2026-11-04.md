# Query performance audit: pre-load review, 2026-11-04

Bead `gm-deployment-0x54`. This is the **pre-load** report: a static review plus read-only live evidence (Neo4j profiles, Postgres settings and one search plan). It is not the baseline. The
baseline is captured once the operator has enabled collection and the Discogs load has finished
([procedure](query-performance-baseline.md)); the perf-audit beads in the other repositories should read that file, whose name is
[`query-performance-baseline-2026-11-07.md`](query-performance-baseline-2026-11-07.md) in this directory.

## State when this was written

| Store | State | Consequence |
| --- | --- | --- |
| Neo4j on lux | Complete (19.6M releases) | Cypher profiles taken now are representative. |
| PostgreSQL on lux | Partial: 3.97M of 19.6M releases (20%), 7.9M of 10.3M artists (77%); labels and masters complete; load resumes soon | Any Postgres timing taken now is a **lower bound**. |

## What was measured

Read-only, on lux, 2026-11-04 about 20:00 UTC, one statement at a time through the operator's `gm-lux-cypher` and `gm-lux-sql`
wrappers (the first Neo4j profile, taken earlier, used a direct read-only `cypher-shell`; the second attempt was then blocked and
the work paused until the wrappers were approved). Postgres is idle (the load is stalled) and **partial**: 3.97M of 19.6M releases. Neo4j is complete.
The API journeys were not run (live traffic; not requested); the script was tested against a local stub only.

### Neo4j (complete graph: 42.5M nodes, 263.0M relationships)

| Case | Time | DB hits | Result |
| --- | --- | --- | --- |
| Explore artist, David Bowie (10263), **first run** | 3,281 ms | 339,676 | 14,698 releases, 1,462 labels |
| Explore artist, David Bowie, same query again | 88 ms | 339,676 | same (37x faster: pages now cached) |
| Trends artist, David Bowie | 509 ms | 46,551 | 61 years |
| Trends label 1818 ("Not On Label"), by id | 8,371 ms | 2,287,786 | 113 years, 615,750 releases; 24 MB |
| Trends genre, Rock | 17,450 ms | 20,715,337 | 82 years, 5,744,250 releases |
| Find Path, Bowie to Johnny Cash, depth 6 | 241 ms | 156,653 | 4 hops |
| Find Path, Bowie to Indecent Noise, depth 6 | 23 ms | 29,246 | 4 hops |
| Find Path, genre Rock to style Trance, depth 6 | 2,277 ms | 7,172,151 | 2 hops |

All `SLOTTED` runtime, `COST` planner. Times are single runs; the ones after the first Bowie run are warm for the pages they touch.
Neo4j effective settings: heap 5 GiB (initial equals max), page cache 11 GiB, `dbms.memory.transaction.total.max` 3 GiB,
transaction timeout 10 minutes, Bolt thread pool max 400, query log `INFO` at `5s`.

### PostgreSQL 18.6 (partial data: lower bound, scaled below)

Effective settings on lux (from `pg_settings`; every row source is the configuration file unless noted):

| Setting | lux | Production overlay in this repository |
| --- | --- | --- |
| `shared_buffers` | 6 GB (786432 x 8 kB) | 2 GB |
| `effective_cache_size` | 18 GB | 6 GB |
| `work_mem` / `maintenance_work_mem` | 64 MB / 1 GB | 64 MB / 512 MB |
| `max_connections` | 300 | 100 |
| `max_wal_size` | 8 GB | 4 GB |
| `random_page_cost` / `effective_io_concurrency` | 1.1 / 200 | same |
| `jit` (`jit_above_cost`) | on (100000, default) | on |
| `max_parallel_workers_per_gather` / `max_parallel_workers` | 4 / 8 | same |
| `default_statistics_target`, `autovacuum_max_workers` | 200, 4 | same |
| `log_min_duration_statement`, `log_temp_files` | 1000 ms, 0 | same |
| `shared_preload_libraries`, `pg_stat_statements.max/.track/.track_planning` | `pg_stat_statements`, 10000, `all`, `off` | same |
| `track_io_timing` | **on** | not set |

Installed extensions in `groovemap`: `pg_trgm` 1.6 and `plpgsql` only. The `groovemap` database is 56 GB at this partial load; the
largest tables: `releases` 16 GB (2.4 GB of indexes, 3.96M rows), `track_credited_on` 7.7 GB (21.8M), `provider_aliases` 6.3 GB (25.4M),
`artists` 5.4 GB (7.9M), `credited_on` 4.3 GB (18.1M), `masters` 3.3 GB (2.6M).

Full-text search on releases is served by `idx_releases_fts` (GIN on `to_tsvector('english', data->>'title')`). One `EXPLAIN (ANALYZE, BUFFERS)`:

| Search term (release branch, rank-ordered, LIMIT 100) | Matches | Time | Buffers |
| --- | --- | --- | --- |
| `rock` | 29,563 | **259 ms** (4 parallel workers) | 97,177 hit, 50,923 read |
| `not on label` | 881 | 42 ms | 2,606 hit, 2,116 read |

For `rock`, the bitmap index scan took 15 ms; the rest is fetching about 5 heap and TOAST pages per matching row (the title lives inside a
`jsonb` document) and ranking each one. A label search for `not on label` matches **319,153** label rows (the labels table is complete).

## Findings

Each one gives evidence, a confidence, and what would fix the intended behavior (the fix is not a mitigation of the symptom).

### 1. Neo4j slow queries may not be visible: the query log is documented as Enterprise-only  (medium confidence)

- Evidence: `docker-compose.yml` pins `neo4j:2026.10.0-community`. The Neo4j operations manual marks the query log section
  `enterprise-edition`. `docker-compose.yml` sets `NEO4J_db_logs_query_enabled: INFO` / `threshold: 10s`, and
  `docker-compose.prod.yml` sets `threshold: 5s` and `plan_description_enabled`. `docs/performance-guide.md` says the production overlay
  "logs queries over five seconds".
- Live: `SHOW SETTINGS` on lux lists `db.logs.query.enabled = INFO` and `threshold = 5s`, so the server accepts the settings; that does not show it writes a log. The wrappers cannot read the log directory, so it is still unconfirmed whether `query.log` exists. Operator check: list the Neo4j logs directory (logs are on tmpfs). The Rock trends case took 17 s, so it would have been logged if logging worked.
- Fix of the intended behavior: record per-query Cypher latency where the queries run: a duration histogram per named query in
  catalog-api's Neo4j wrapper (`run_query` / `run_single`), exported to VictoriaMetrics, with the query name and a slow-query log
  line above a threshold. Then correct the guide and drop the inert settings. Correcting the text alone only removes the false claim.
- Interim: `SHOW TRANSACTIONS` sampling (works in Community) and `PROFILE`.

### 2. `pg_stat_statements` is preloaded everywhere but created nowhere  (high confidence)

- Evidence: `docker-compose.prod.yml` preloads it (`shared_preload_libraries`, `.max=10000`, `.track=all`). `grep` finds no
  `CREATE EXTENSION pg_stat_statements` in this repository. Live: `pg_extension` in lux's `groovemap` lists only `pg_trgm` and `plpgsql`. Without the extension
  the views do not exist, so the preload costs shared memory and yields nothing.
- Fix: create it where the schema is created (the database-schema repository owns the schema; the bundled Postgres needs it in its
  init path), and on lux, as the shared server's owner, per procedure step 1. Proposal only; nothing was changed.

### 3. Artist Explore counts are computed per request, unlike genre and label  (high confidence)

- Evidence: the profile table above: **3,281 ms cold, 88 ms warm** for the same 339,676 hits, so most of the cold cost is page-cache faults, not computation. The `explore_genre`, `explore_label` and `explore_style` docstrings record that they were moved to
  precomputed node properties (Rock was about 200M DB hits, now about 3). `explore_artist` still counts releases and labels by traversal
  (`COUNT { MATCH (r:Release)-[:BY]->(a) RETURN DISTINCT r }`, then again joined to labels). Cost grows with the artist's release count:
  339,676 hits for 14,698 releases is about 23 hits per release.
- Mitigated only by Valkey for repeat visits (`explore:artist:*`); the first visit to every popular artist pays this.
- Fix: precompute `release_count` and `label_count` on `Artist` in the enricher, as was done for labels. Unmeasured for artists with
  100k+ releases (for example Various): extrapolating linearly gives about 2.3M hits.

### 4. Heavy cold queries: label and genre trends, Find Path across hubs  (measured)

- `trends_label` for label 1818 took **8.4 s** and 2.29M hits; unlike `explore_label` it has no precomputed shape. Valkey caches genre, style and label results, so the cost is the cold call. `trends_artist` is not cached (0.5 s for Bowie).
- `trends_genre` for Rock took **17.5 s** and 20.7M hits (the code comment claims about 16M); also cold-call cost.
- Find Path between two artists was fast (23 to 241 ms), but genre Rock to style Trance took **2.3 s** and 7.2M hits at 2 hops: genre and style hubs are what make it expensive. `/api/path` runs `shortestPath` over six relationship types, default depth 6, up to 10, with a 120 s query timeout. Paths that touch a
  hub (`Rock`, `Not On Label`, a "Various" artist) make a bidirectional search very wide.
- `/api/explore`, `/api/expand`, `/api/trends` and `/api/path` carry **no rate limit** (`search`, `autocomplete`, `genre-tree` and credits do:
  30 or 60 per minute) and `api/limiter.py` has no default limit. A caller that can reach the API can start many 120 s traversals at once.
  Verify at the edge (the reverse proxy may limit them); if it does not, this is a service-capacity risk, not only a latency one.
- The API journeys remain to be run. Fixes belong to catalog-api (limits, cached or precomputed
  trends, a path hub guard) and are ranked once there are numbers.

### 5. Neo4j page cache is likely small against the store  (medium confidence; store size not obtainable)

- Evidence: lux runs heap 5 GiB, page cache 11 GiB, transaction memory 3 GiB (confirmed by `SHOW SETTINGS`). The overlay's comment sizes 16g of page cache
  for a "25GB store" and "131M relationships"; the graph now has **263M relationships and 42.5M nodes**, twice that count, so the store is probably well above
  25 GB and 11 GiB caches under half of it. The Bowie explore going from 3.3 s to 88 ms on a repeat shows the cold-to-warm gap. Transaction memory 3 GiB is under 70% of the heap (3.5 GiB), which is consistent.
- The read-only wrapper refuses `CALL` (so `dbms.queryJmx` and `apoc.monitor.store` could not report the store size). The operator can read the size of the
  `neo4j` database directory directly.
- This matters for every traversal above: a cold, wide traversal reads pages from disk.
- To confirm: that directory size, and the `Page Cache Hits/Misses` columns of `PROFILE` (the wrapper's plain output shows only totals; the `neo4j` command expects `--format verbose`). If hit ratio is poor, raise page cache by taking from the container limit, not from heap.
- Not recommended: any change before those two numbers.

### 6. Postgres planner settings: reviewed against lux's effective values  (medium confidence)

lux's Postgres is a shared, homelab-owned server and its values differ from the overlay (table above). The review below uses lux's real values. The earlier draft of this table judged the overlay's `shared_buffers=2GB` small; lux runs 6 GB, so that comment is withdrawn.

| Setting (overlay) | Review |
| --- | --- |
| `shared_buffers=6GB`, `effective_cache_size=18GB` | The database is 56 GB at 20% of the releases and will be roughly 2.5 to 3 times that (releases alone 16 GB, about 80 GB at full size), so far beyond cache. The `rock` search read 50,923 pages and hit 97,177: cache misses are the norm for wide searches. Raising memory helps only by taking it from Neo4j's 48 GB container limit; decide with the baseline hit ratios. |
| `work_mem=64MB` with `max_parallel_workers_per_gather=4`, `max_connections=300` | Each parallel worker gets `work_mem` per sort or hash node; worst case with 300 connections is far above RAM, though real concurrency is bounded by the pools. `log_temp_files=0` is on, so spills are logged; read the log for them. |
| `jit=on`, `jit_above_cost=100000` | The `rock` search plan cost 36,000, below the threshold, so JIT did not engage there. Whether larger statements pay for compilation is for the baseline's JIT columns to show; no change recommended. |
| `random_page_cost=1.1`, `effective_io_concurrency=200` | Right for NVMe; wrong for a spinning or network volume. The disk type is unconfirmed. `track_io_timing` is on, and the `rock` plan showed 120 ms of read time over 50,923 pages (about 2.4 microseconds a page), consistent with fast storage or OS cache. |
| `default_statistics_target=200` | Fine; large skewed columns (genre, label, year) may deserve per-column targets once plans are seen. |
| `track_io_timing=on` | Already on at lux, so `pg_stat_statements` will carry I/O times once created; that is its extra cost (a clock read per I/O). |
| `log_min_duration_statement=1000` | Logged 111 statements in 24 h on lux, slowest about 438 s. Exclude backup `COPY` and `identity_ledger_refresh` when ranking: one is expected to be big, the other is a batch job. |

### 7. PgBouncer and `pg_stat_statements`

See the [procedure](query-performance-baseline.md#pgbouncer): session mode does not change the counters, but pool wait is invisible to
them and statements cannot be split per service without per-service roles.

### 8. The "Not On Label" label has no name in the graph  (high confidence on the fact, cause not established)

- Evidence: in Neo4j the largest label by `release_count` is id **1818** with 762,594 releases and **`name` NULL**; its only properties are
  `id`, `artist_count`, `release_count`, `genre_count`. `MATCH (l:Label {name: 'Not On Label'})` returns nothing, and no label has exactly that name
  (there are thousands named `Not On Label (<artist>)`, up to 2,029 releases). In Postgres, `labels` has **no row** with `data_id = '1818'`.
- Consequence: `/api/explore`, `/api/trends` and `/api/expand` for "Not On Label" by name return 404, and searching for the label finds nothing, while it is the largest label in the graph.
  The audit's named heavy case is unreachable by name. The enrichers presumably create the node from release references without a label record.
- Fix of the intended behavior: give the node its real name (this is data-model work in the loaders/enrichers), not a special case in the API.

### 9. Search for common terms matches a large share of the table  (measured, partial data)

- `rock` matches 29,563 of 3.96M releases (0.75%) and the rank-ordered top 100 costs 259 ms because every match is fetched and ranked;
  `not on label` matches 319,153 label rows. Cost follows the number of matches, not the page size.

## How Postgres numbers should scale to the full load

No Postgres timing exists yet; when one is captured before the load finishes, treat it as a lower bound and apply this. The factors
come from the counts above: releases 19.6 / 3.97 = **4.9x**, artists 10.3 / 7.9 = **1.3x**, labels and masters **1.0x**.

| Statement shape | Expected growth at full load |
| --- | --- |
| Index probe of one row (by id or key) | Nearly flat: a tree one level deeper at most. |
| Full-text search over releases | Roughly with the number of matching rows (ranking, sorting, highlighting): **about 4.9x**. The `rock` search (259 ms, 29,563 matches, partial) would reach roughly 145,000 matches and about **1.3 s** at full load if the cache hit rate held; it will not, because the table is 16 GB now and about 80 GB then. Index size grows 4.9x, so buffer-cache hit rate falls once it exceeds `shared_buffers`. |
| Search over artists | About 1.3x. |
| Sequential scan, aggregate or `COUNT` over releases | Linear: **4.9x**. |
| Hash join or sort of release-sized sets | 4.9x plus a step up once the working set passes `work_mem` (64MB) and spills to disk. |
| `identity_ledger_refresh` and other batch refreshes (438 s seen) | At least 4.9x on the release side; superlinear where a join spills. A 438 s run now implies roughly 36 minutes or more at full size. This is an extrapolation. |
| Backup `COPY` | Linear in table size. |
| Joins between releases and the complete label and master tables | Driven by the release side: 4.9x. |

## Proposed follow-up beads

Not filed; for the operator or planner to file.

1. **catalog-api: per-query Cypher latency metrics and slow-query log.** Replaces the unavailable Neo4j query log (finding 1).
2. **catalog-api: precompute artist release and label counts** (finding 3), or serve them from the enricher.
3. **catalog-api: rate limits and a depth/hub guard for `/api/explore`, `/api/expand`, `/api/trends`, `/api/path`** (finding 4), after the cases are measured.
4. **database-schema / deployment: create `pg_stat_statements` in the database and the bundled Postgres init path** (finding 2).
5. **deployment: correct `docs/performance-guide.md` and the Neo4j overlay once finding 1 is confirmed** (inert query-log settings).
6. **deployment (operator-approved run): capture the baseline** after the load: steps 1 to 5 of the procedure, write `query-performance-baseline-<date>.md`.
7. **deployment (operator): read the Neo4j database directory size and the log directory** (findings 1 and 5); the Postgres and Neo4j settings are now recorded above.
8. **discogs-graph-enricher / loaders: give label 1818 its name** (finding 8), and decide how Not On Label should be searched and explored.
