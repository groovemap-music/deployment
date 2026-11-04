# Query performance audit: static review, 2026-11-04

Bead `gm-deployment-0x54`. This is the **pre-load** report: a static review plus one live `PROFILE`. It is not the baseline. The
baseline is captured once the operator has enabled collection and the Discogs load has finished
([procedure](query-performance-baseline.md)); the perf-audit beads in the other repositories should read that file, whose name will be
`query-performance-baseline-<date>.md` in this directory.

## State when this was written

| Store | State | Consequence |
| --- | --- | --- |
| Neo4j on lux | Complete (19.6M releases) | Cypher profiles taken now are representative. |
| PostgreSQL on lux | Partial: 3.97M of 19.6M releases (20%), 7.9M of 10.3M artists (77%); labels and masters complete; load resumes soon | Any Postgres timing taken now is a **lower bound**. |

## What was and was not measured

- **Measured: one Cypher profile, read-only, on lux.** `PROFILE` of the artist Explore counts for David Bowie (10263): **3.28 s**,
  **339,676 DB hits**, 1 row, 393 KB, `SLOTTED` runtime, planner `COST`. It returned 14,698 releases and 1,462 labels. One sample;
  whether the page cache was warm is unknown.
- **Blocked: everything after that.** The auto-mode classifier denied the second live query (`trends` for Rock). I did not work around it.
  The Rock, Not On Label, Find Path and Bowie trends cases are therefore profiled by reading their Cypher, not by running it, and
  are marked *unmeasured* below. `scripts/perf-baseline.sh neo4j` is ready to run them once approved.
- **Not run: any Postgres query.** I had no approved Postgres access, so there are **no Postgres timings** in this report.
  What follows for Postgres is configuration review and scaling expectation only.
- **Not run: the API journeys** (live traffic). The script was tested against a local stub server only.

## Findings

Each one gives evidence, a confidence, and what would fix the intended behavior (the fix is not a mitigation of the symptom).

### 1. Neo4j slow queries cannot be seen: the query log is an Enterprise feature  (high confidence on the claim, one check left)

- Evidence: `docker-compose.yml` pins `neo4j:2026.10.0-community`. The Neo4j operations manual marks the query log section
  `enterprise-edition`. `docker-compose.yml` sets `NEO4J_db_logs_query_enabled: INFO` / `threshold: 10s`, and
  `docker-compose.prod.yml` sets `threshold: 5s` and `plan_description_enabled`. `docs/performance-guide.md` says the production overlay
  "logs queries over five seconds".
- To confirm, read-only on lux, with approval: list `query.log` in the Neo4j logs directory (expect it absent; logs are on tmpfs).
- Fix of the intended behavior: record per-query Cypher latency where the queries run: a duration histogram per named query in
  catalog-api's Neo4j wrapper (`run_query` / `run_single`), exported to VictoriaMetrics, with the query name and a slow-query log
  line above a threshold. Then correct the guide and drop the inert settings. Correcting the text alone only removes the false claim.
- Interim: `SHOW TRANSACTIONS` sampling (works in Community) and `PROFILE`.

### 2. `pg_stat_statements` is preloaded everywhere but created nowhere  (high confidence)

- Evidence: `docker-compose.prod.yml` preloads it (`shared_preload_libraries`, `.max=10000`, `.track=all`). `grep` finds no
  `CREATE EXTENSION pg_stat_statements` in this repository; the bead reports the same for the shared server on lux. Without the extension
  the views do not exist, so the preload costs shared memory and yields nothing.
- Fix: create it where the schema is created (the database-schema repository owns the schema; the bundled Postgres needs it in its
  init path), and on lux, as the shared server's owner, per procedure step 1. Proposal only; nothing was changed.

### 3. Artist Explore counts are computed per request, unlike genre and label  (medium confidence on the extrapolation)

- Evidence: the profile above. The `explore_genre`, `explore_label` and `explore_style` docstrings record that they were moved to
  precomputed node properties (Rock was about 200M DB hits, now about 3). `explore_artist` still counts releases and labels by traversal
  (`COUNT { MATCH (r:Release)-[:BY]->(a) RETURN DISTINCT r }`, then again joined to labels). Cost grows with the artist's release count:
  339,676 hits for 14,698 releases is about 23 hits per release.
- Mitigated only by Valkey for repeat visits (`explore:artist:*`); the first visit to every popular artist pays this.
- Fix: precompute `release_count` and `label_count` on `Artist` in the enricher, as was done for labels. Unmeasured for artists with
  100k+ releases (for example Various): extrapolating linearly gives about 2.3M hits.

### 4. Unmeasured, suspected heavy: label and genre trends, expand, and Find Path  (hypotheses, with how to verify)

- `trends_label` (`Not On Label`) traverses every release on that label then groups by year; unlike `explore_label` it has no
  precomputed shape. Valkey caches the result, so the cost is the cold call. `trends_artist` is not cached at all.
- `trends_genre` for Rock was measured by its author at about 16M hits after a rewrite (150M before); not re-measured.
- `/api/path` runs `shortestPath` over six relationship types, default depth 6, up to 10, with a 120 s query timeout. Paths that touch a
  hub (`Rock`, `Not On Label`, a "Various" artist) make a bidirectional search very wide.
- `/api/explore`, `/api/expand`, `/api/trends` and `/api/path` carry **no rate limit** (`search`, `autocomplete`, `genre-tree` and credits do:
  30 or 60 per minute) and `api/limiter.py` has no default limit. A caller that can reach the API can start many 120 s traversals at once.
  Verify at the edge (the reverse proxy may limit them); if it does not, this is a service-capacity risk, not only a latency one.
- Verify by running the `neo4j` cases and the `api` journeys under approval. Fixes belong to catalog-api (limits, cached or precomputed
  trends, a path hub guard) and are ranked once there are numbers.

### 5. Neo4j memory: page cache looks small against the store  (needs one number)

- Evidence: lux runs heap 5g, page cache 11g, transaction memory 3g (bead). The production overlay's own comment sizes 16g of page cache
  for a "25GB store" and "131M relationships"; the graph is now complete at 19.6M releases, so the store is likely at least that size and
  11g caches under half of it. Transaction memory 3g is under the 70% of heap (3.5g) rule, which is consistent.
- This matters for every traversal above: a cold, wide traversal reads pages from disk.
- To confirm: the on-disk size of the `neo4j` database directory (operator, read-only) and the `Page Cache Hits/Misses` in `PROFILE`
  output (the `neo4j` command expects `--format verbose`, which prints the plan table; `--format plain` prints only totals). If hit ratio is poor, raise page cache by taking from the container limit, not from heap.
- Not recommended: any change before those two numbers.

### 6. Postgres planner settings: review of the overlay values, none verified on lux  (low to medium confidence)

lux's Postgres is a shared, homelab-owned server, so the production overlay values below are the repository's intent, not necessarily what runs.
Report step 1 of `perf-baseline.sql` prints the real ones.

| Setting (overlay) | Review |
| --- | --- |
| `shared_buffers=2GB`, `effective_cache_size=6GB` | Small for a database growing to 19.6M releases plus indexes; the planner's cache assumption should follow the real RAM left after Neo4j (48G limit by default). Compare with `blks_hit/blks_read` in the report. |
| `work_mem=64MB` with `max_parallel_workers_per_gather=4` | Each parallel worker gets `work_mem` per sort or hash node. Under load this can reach several GB for one query; with up to 100 connections the worst case exceeds the 6GB cache assumption. The `log_temp_files=0` overlay setting and the report's `temp_blks_written` column show whether queries already spill. |
| `jit=on` | JIT often hurts short analytical queries on mid-sized plans (compile time exceeds the gain). The report lists JIT time per statement; decide only from those numbers. |
| `random_page_cost=1.1`, `effective_io_concurrency=200` | Right for NVMe; wrong for a spinning or network volume. The disk type on lux is unconfirmed. |
| `default_statistics_target=200` | Fine; large skewed columns (genre, label, year) may deserve per-column targets once plans are seen. |
| `log_min_duration_statement=1000` | Logged 111 statements in 24 h on lux, slowest about 438 s. Exclude backup `COPY` and `identity_ledger_refresh` when ranking: one is expected to be big, the other is a batch job. |

### 7. PgBouncer and `pg_stat_statements`

See the [procedure](query-performance-baseline.md#pgbouncer): session mode does not change the counters, but pool wait is invisible to
them and statements cannot be split per service without per-service roles.

## How Postgres numbers should scale to the full load

No Postgres timing exists yet; when one is captured before the load finishes, treat it as a lower bound and apply this. The factors
come from the counts above: releases 19.6 / 3.97 = **4.9x**, artists 10.3 / 7.9 = **1.3x**, labels and masters **1.0x**.

| Statement shape | Expected growth at full load |
| --- | --- |
| Index probe of one row (by id or key) | Nearly flat: a tree one level deeper at most. |
| Full-text or trigram search over releases | Roughly with the number of matching rows (ranking, sorting, highlighting): **about 4.9x**. Index size grows 4.9x, so buffer-cache hit rate falls once it exceeds `shared_buffers`. |
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
7. **deployment: read the lux Postgres and Neo4j effective settings and store sizes** (findings 5 and 6): `perf-baseline.sql` step 1, and the `neo4j` directory size.
