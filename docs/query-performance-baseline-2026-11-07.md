# Query performance baseline, 2026-11-07 (after the full loads)

Bead `gm-deployment-5pxz`. This re-measures the cases the
[pre-load audit](query-performance-audit-2026-11-04.md) and the catalog-api and analytics-engine SQL audits
(`sql-performance-audit-2026-11-04.md`, `sql_performance_audit_2026-11-04.md`) measured on partial data or the old schema
shape. Procedure: [query-performance-baseline.md](query-performance-baseline.md).

## State when this was measured

Read-only, on lux, 2026-11-07 about 19:30 to 20:00 UTC, one statement at a time through the operator's `gm-lux-sql` and
`gm-lux-cypher` wrappers. Nothing was written, reset or deployed. Postgres had no other active statement at any check.

| Item | 2026-11-04 audit (partial) | Now |
| --- | --- | --- |
| Discogs load | 3.97M of 19.6M releases | complete (version 20261101; `releases` estimate 19.15M) |
| MusicBrainz load | empty | complete (20261104-001002) |
| `groovemap` database | 56 GB | 252 GB (`releases` 70 GB, `track_credited_on` 23 GB, `credited_on` 13 GB) |
| `credited_on`, `track_credited_on`, `same_as` rows | 18.0M, 21.7M, 2.85M | 70.9M, 91.3M, 7.38M (estimates) |
| Neo4j | 42.5M nodes, 263.0M relationships | 42.47M nodes, 269.99M relationships |
| Schema, services | v0.6.0 indexes | schema v0.10.0; catalog-api v0.10.0, analytics-engine v0.7.0 |
| `ExtractionCompletion.stats_version` | not applicable | **1**, `maintained_at` 2026-11-06T20:50:16Z (version 20261101) |
| Postgres cache hit ratio, whole database | not read | 96.85% (19.44B hits, 632M reads), 446 temp files, 68 GB temp since the statistics were last reset |

Unchanged on lux: `shared_buffers` 6 GB, `work_mem` 64 MB, `jit` on, `max_parallel_workers_per_gather` 4, `track_io_timing` on.
`pg_stat_statements` is **still not created** (`pg_extension` lists only `pg_trgm` and `plpgsql`), so nothing here ranks by real traffic.

How to read the Neo4j timings: the page cache was already warm from the loads, so a "first" run is not cold. Cold cannot be forced
without a restart, which is not read-only. The audit's 3.3 s cold Bowie run and 17.4 s Rock run were taken on a cache that had not
yet touched those pages; compare **DB hits**, which do not depend on the cache, before comparing times.

## Neo4j: heavy named cases (`scripts/perf-baseline.sh neo4j`, through `gm-lux-cypher`)

Each case ran twice, 3 s apart, serially. The script reads Cypher on stdin; the wrapper takes it as an argument, so a one-line adapter
(`gm-lux-cypher "$(cat)"`) was used as `PERF_CYPHER_CMD`. The case statements are the script's own, which mirror older catalog-api
statements: see "Script drift" below.

| Case | 2026-11-04 | Now, first / repeat | DB hits then → now | Verdict |
| --- | --- | --- | --- | --- |
| explore.artist.bowie | 3,281 ms first, 88 ms repeat | 105 / 59 ms | 339,676 → 339,666 | Same work; the old cold figure was page faults |
| trends.artist.bowie | 509 ms | 67 / 32 ms | 46,551 → 46,551 | Same work |
| trends.label 1818 (Not On Label) | 8,371 ms | 898 / 882 ms | 2,287,786 → 2,287,787 | Same work; 113 years, 24 MB |
| trends.genre Rock | 17,450 ms | **3,807 / 3,877 ms** | 20,715,337 → 20,715,337 | Same work; still 3.8 s with a warm cache |
| path Bowie to Johnny Cash | 241 ms | 133 / 48 ms | 156,653 → 157,323 | Same work, 4 hops |
| expand label 1818 releases | not measured | 287 / 285 ms | 1,525,193 | 762,594 releases |

Label 1818 now has its name (`Not On Label`, 762,594 releases, 490,698 artists), so audit finding 8 is resolved on the data.

### Person-led reads of a heavy name (Bob Ludwig, `Person` node in Neo4j)

| Statement (catalog-api `credits_queries.py`) | First / repeat | DB hits | Rows |
| --- | --- | --- | --- |
| `get_person_credits`, page 1 (limit 100) | 361 / 208 ms | 433,066 | 100 |
| `get_person_profile` | 171 / 138 ms | 243,045 | 1 |
| `get_person_role_breakdown` | 84 / 74 ms | 182,279 | 7 |

The person routes run on Neo4j, so the schema audit's hash-key regression (18 ms to 382 ms cold on `credited_on` by name) cannot reach them.
**The Postgres side differs from what that audit assumed:** lux has no `person_name` index on `graph.credited_on` or
`graph.track_credited_on` (only the `credit_key` unique index and `role_category`), so a person-led SQL read plans as a Parallel Seq Scan over
the 71.8M-row table (planner cost 935,117; `EXPLAIN` only, not executed). No catalog-api statement does this today.

## Neo4j: catalog-api acceptance cases that the 2026-11-05 comment asked to include

| Case | Baseline | Now, first / repeat | DB hits | Acceptance |
| --- | --- | --- | --- | --- |
| `gm-catalog-api-575h`: `_CLUSTER_NEIGHBOURS`, Bowie (10263), limit 50 | 29.6 s, 37.5M hits | 79 / 43 ms | 290,786 | **Met** (under 2M hits, under 1 s) |
| same, The Beatles (82730) | 3.4 s, 31M hits | 108 / 91 ms | 547,042 | **Met** |
| `gm-catalog-api-j4ml`: label 1866 `/similar`, `_CANDIDATES_STORED` (HAS_STYLE) | 54.7M hits, 28.3 s | 401 / 308 ms | 743,731 | **Met** for the candidate step (under 10M); the genre-vector phase was not run |
| `gm-catalog-api-j4ml`: label 1866 DNA, sum of the five statements below | about 40M hits (Columbia) | 1.4 s the heaviest | **27.86M** (30% fewer) | **Not met**: the bead asks for at least 40% fewer; peak statement 39.6 MB is under 60 MB |

Label 1866 DNA statements, DB hits and peak memory: identity 5 hits; genre and style in one pass 8,535,301 (38.9 MB, 1.4 s); year counts
1,053,555; format profile 1,053,555 (13.3 MB); media family counts 8,253,821 (37.2 MB); medium counts 8,962,312 (39.6 MB). The fingerprint
equality and the similarity-quality check the bead also asks for were not run (no Lux writes, and no evaluation harness on lux).

## PostgreSQL: analytics credited-edge plans on the v0.6.1 hash indexes

`EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`, one run each (the 120 s wrapper cap leaves no room for a repeat on the track query), the
statements verbatim from `analytics-engine` `insights/embedding_pipeline.py` with the kept categories.

| Statement | 2026-11-04 (partial) | Now | Plan |
| --- | --- | --- | --- |
| `_CREDITED_ARTIST_EDGE_SQL` | 25.9 s for 14.6M kept rows (1.8 µs/row), old btree plan, 425 MB temp | **63.96 s for 57.93M rows (1.10 µs/row)**, no temp | Seq Scan `credited_on`, Nested Loop, Memoize over `same_as_person_name_hash`: 48.5M hits, 9.57M misses, 8.58M evictions; 788k pages read |
| `_TRACK_CREDITED_ARTIST_EDGE_SQL` | 26.2 s for 21.8M rows (1.2 µs/row), 637 MB temp | **51.05 s for 90.0M rows (0.57 µs/row)**, no temp | Same shape: 85.6M hits, 5.89M misses, 4.83M evictions; 1.10M pages read |
| `_CREDITED_RELEASE_IDS_SQL` (`SELECT DISTINCT release_id`) | not measured in this form | **29.7 s**, 9.71M groups | Index Only Scan `credited_on_role_category`, HashAggregate planned for 1.93M groups, **5 batches, 1.04 GB temp** |

The audit predicted 1.5 to 2.5 minutes per edge query at full size and a possible nested-loop cost increase on the hash index. The edge queries ran
in about one minute each, per-row cost down 39% and 53%, with the nested loop plus Memoize the audit predicted, and no spill (the statements no longer use
`DISTINCT`). The "2x cost" hash-join comparison needs `SET enable_nestloop`, which the wrapper refuses, so it was not repeated live.
The DISTINCT release-id query shows a 5x row-estimate miss (1.93M planned, 9.71M actual) that sends 1.04 GB to temp files.

## PostgreSQL: Release FTS (`idx_releases_fts`, release branch only, rank-ordered, LIMIT 100)

The statement is the release branch alone using the indexed expression `to_tsvector('english', COALESCE(data->>'title',''))`. It is **not**
the route's four-branch statement, so the audit's route-level figures (H1: `love` 8.79 s) are not directly comparable. Parallel, 4 workers.

| Term | Matches (2026-11-04 → now) | 2026-11-04 | Now, first / repeat |
| --- | --- | --- | --- |
| `rock` | 29,563 → 142,590 | 259 ms | **4,152 / 1,218 ms** (audit predicted about 1.3 s at full size) |
| `miles davis` | 423 → 2,048 | 85 ms (route) | 310 / 101 ms |
| `love` | 124,549 → 497,272 | 8,792 ms (whole route) | **12,759 / 4,295 ms** |

Warm `rock` matches the extrapolation; the first run is 3.4x slower from reading TOAST pages, as the audit warned. Cost still follows the match count.

## Ledger and derived refreshes

- `identity_ledger_refresh_state`: last refresh 2026-11-07 18:19:25 UTC (1 h 15 min old at 19:34 UTC), watermark `{"writes": 141211986}`. The ledger is now
  incremental: `identity_ledger_item_mints` 45.07M rows (7.4 GB), `item_events` 3.87M, `edition_events` 2.09M, `edition_parents` 2.44M, `work_summary` 4.13M,
  `density` 12 rows. The 2026-11-04 audit's whole-history recompute (45 s for 4 rows, 3 to 6 min estimated at scale) no longer describes this schema.
- **The duration of `identity_ledger_refresh` was not measured**: running it writes, and with `pg_stat_statements` absent and the server log out of reach
  of the wrappers there is no recorded duration to read. This acceptance item is open until the extension exists or the operator reads the slow-statement log.
- `loader_derived_refresh_job`: one row, `discogs` 20261101, generation 1, `completed`, started 20:25:26.132 and completed 20:25:26.138 on 2026-11-06 (5 ms
  between the two timestamps, 1 attempt). **There is no MusicBrainz row.** The row is a state record, not evidence of the refresh work's cost.

## Not measured, and why

| Item | Reason |
| --- | --- |
| API journeys (`perf-baseline.sh api`, `PERF_CONFIRM_LIVE`), route p50/p95 | The assignment limits lux access to the two read-only wrappers, which cannot reach the API origin. Needs the operator's approval and a reachable origin |
| Real v0.5.0 maintenance including writes | Writes on lux are out of scope for this run |
| `identity_ledger_refresh` duration | See above |
| Per-queue ack rates and the 50/s `QueueBacklogGrowing` floor | RabbitMQ is not reachable through the wrappers |
| Postgres top statements by total or mean time | `pg_stat_statements` is not created |
| True cold Neo4j and Postgres reads | Not forcible read-only |
| Fingerprint equality and similarity quality for `j4ml` | No evaluation harness reachable read-only |

## Findings for the operator or planner (not filed)

1. **`j4ml` label DNA misses its 40% target:** 27.86M hits against about 40M is 30% fewer. Four of the five statements still walk every release of the label
   (8.5M, 8.3M, 9.0M hits for genre and style, media family and medium). Count them from stored label properties or fold them into one pass.
2. **`pg_stat_statements` is still absent** after the full load, so the ranking this procedure exists to produce cannot be made. Step 1 of the procedure needs the server owner.
3. **`_CREDITED_RELEASE_IDS_SQL` spills 1.04 GB** because the planner expects 1.93M distinct release ids and finds 9.71M. Check the column statistics, or
   build the id set from the edge stream as the analytics audit's F1 proposes.
4. **Heavy Release FTS terms stay slow:** `love` takes 4.3 s warm and 12.8 s on the first run for the release branch alone, `rock` 1.2 s warm. The
   audit's H1 and H2 rewrites target the route; re-measure the route once the API journeys can run.
5. **Trends for a genre are still a traversal:** Rock is 20.7M hits and 3.8 s even on a warm page cache (finding 4 of the 2026-11-04 audit stands).
6. **No Postgres index serves a person-led read on the credit tables** (Seq Scan over 71.8M rows). Harmless while every person route is on Neo4j; confirm that
   no analytics or operations job reads `graph.credited_on` by `person_name`.
7. **No `loader_derived_refresh_job` row for MusicBrainz** after the MusicBrainz load completed. Confirm whether that loader should record one.
8. **`scripts/perf-baseline.sh` drift:** the `explore.artist.bowie` case still counts releases by traversal (339,666 hits) although `Artist.release_count`
   is stored (Bowie 14,698), and `neo4j` mode needs an adapter for the wrapper's argument-style input.

## Method note

A first attempt at the FTS cases omitted `COALESCE` from the indexed expression, so the planner could not use `idx_releases_fts` and the statement
scanned `releases` (about 77M buffers per run). Four such statements ran, three finished (`rock` twice, `love` once), and the fourth was stopped by hand. They were
read-only, but they loaded the shared server for several minutes and add to the block counters above. The figures in the FTS table are from
the corrected statement. Raw outputs are kept outside the repository.
