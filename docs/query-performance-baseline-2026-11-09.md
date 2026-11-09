# Route p95 and maintenance durations, 2026-11-09

Bead `gm-deployment-habq`, roadmap item 4 (`design` `docs/audits/performance-roadmap-2026-11-09.md`). First route percentiles, and the
first maintenance durations read from `pg_stat_statements`. Procedure: [query-performance-baseline.md](query-performance-baseline.md).
Prior state: [2026-11-07 baseline](query-performance-baseline-2026-11-07.md).

## State when this was measured

- Read-only on lux. Routes: 2026-11-09 18:20 UTC, from the operator session's read access to the API, with
  `scripts/perf-baseline.sh roadmap` (`PERF_ITERATIONS=5`, `PERF_PAUSE=2.2`): one request at a time, GET only, 2.2 s apart.
  Maintenance: 2026-11-09 about 18:10 UTC through `gm-lux-sql`, one statement at a time.
- **catalog-api v0.11.1, before v0.11.2.** Search on the new side tables and exact facets (v0.11.2) was not live; the search rows are the
  **v0.11.1 baseline** and the "after v0.11.2" slot below is open. Delayed by a GitHub Actions outage.
- First request of each journey is first-uncached for that key (Valkey held no result); requests 2 to 5 are warm (served from Valkey, hence
  2 to 5 ms). With 5 requests, four are warm, so the warm p95 equals the warm max.
- The Neo4j and Postgres page caches were warm from earlier traffic: "first" means uncached in Valkey, not cold on disk.

## Route p95 against the roadmap targets

A target is met only if the first-uncached time **and** the warm p95 are within it (a first visit is the cost users see on a new key).

| Journey | Target | First uncached | Warm p95 | Verdict |
| --- | ---: | ---: | ---: | --- |
| Search `love`, release, Jazz, 1970s, vinyl (facets, filters) | 1 s | **11,751 ms** | 4 ms | **Missed** (v0.11.1) |
| Search `love`, all types | 1 s | **1,603 ms** | 3 ms | **Missed** (v0.11.1) |
| Master search `love` (`types=master`) | 100 ms | **651 ms** | 4 ms | **Missed** (v0.11.1) |
| Ledger works window, 2026-11 | 2 s | not measured | not measured | **Unmeasured**: admin route, no token in the session (`PERF_AUTH_HEADER`). Statement time 0.28 s (roadmap) |
| Trends, genre Rock | 1 s | **3,780 ms** | 2 ms | **Missed** (roadmap item 3 not landed) |
| Trends, style Trance | 1 s | 410 ms | 3 ms | Met |
| Trends, label 1818 (Not On Label) | 1 s | 929 ms | 2 ms | Met, 71 ms of headroom |
| Explore, Bowie | 1 s | 83 ms | 2 ms | Met |
| Expand, Bowie releases | 1 s | 63 ms | 4 ms | Met |
| Expand, label 1818 releases | 1 s | 47 ms | 5 ms | Met |
| Path, Bowie to Johnny Cash | 5 s | 118 ms | 53 ms | Met |
| Path, Rock to Trance | 5 s | 2,018 ms | 2,165 ms | Met (2.2 s warm: Valkey does not cache it, or it is not cached long) |

Raw rows (`journey ok bad cold p50 p95 max`, ms): search.love.facets 5 0 11751 3 4 4; search.love.all-types 5 0 1603 3 3 3; search.master.love
5 0 651 3 4 4; trends.genre.rock 5 0 3780 2 2 2; trends.style.trance 5 0 410 2 3 3; trends.label.1818 5 0 929 2 2 2; explore.artist.bowie
5 0 83 2 2 2; expand.artist.bowie.releases 5 0 63 4 4 4; expand.label.1818.releases 5 0 47 5 5 5; path.bowie-to-cash 5 0 118 49 53 53;
path.rock-to-trance 5 0 2018 2094 2165 2165. No request failed or returned a 429.

Readings:

- **Search is the open gap**, and the roadmap's statement figures did not carry to the route. Filtered `love` was 0.67 s as a statement and is
  11.8 s as a first route request; master search was 39 ms as a statement and is 651 ms. The route does more than that statement (several
  branches, facets, highlighting) and the first request reads uncached pages. v0.11.2 is the change aimed at this; re-measure it.
- **Genre trends stay at 3.8 s first visit**, the same as the 2026-11-07 traversal (3.8 s). It is cached afterwards (2 ms), so the cost is
  every first visit and evicted key, as roadmap item 3 says. Style (410 ms) and label (929 ms) pass.
- **The Valkey layer works**: warm requests are 2 to 5 ms everywhere except path.
- **Path Rock to Trance is not faster when repeated** (2.0 s, then 2.1 s), so a hub-to-hub path pays its full cost each time; it passes the
  5 s cap with 2.8 s to spare.
- Not measured here: the "authentication under concurrent cold searches" target (needs concurrent load, which this run does not generate).

### After v0.11.2 (open)

| Journey | Target | First uncached | Warm p95 | Verdict |
| --- | ---: | ---: | ---: | --- |
| Search `love`, facets and filters | 1 s | _pending_ | _pending_ | _pending_ |
| Search `love`, all types | 1 s | _pending_ | _pending_ | _pending_ |
| Master search `love` | 100 ms | _pending_ | _pending_ | _pending_ |

Re-run: `PERF_CONFIRM_LIVE=yes PERF_API_BASE=<origin> scripts/perf-baseline.sh roadmap`. Use a term whose key is not in Valkey (or a fresh
term) so the first request is uncached.

## Maintenance durations (`pg_stat_statements`)

`scripts/perf-baseline.sh maintenance` (`scripts/perf-maintenance.sql`). Entries are per statement since `stats_since`; the extension
cannot split calls by code version, so several entries exist per job (one per period in which the statement text had a stable id). Each row
is read with its own `stats_since`. Mean and max are for the calls counted.

| Job (statement) | Calls | Mean | Max | Since | Roadmap target | Verdict |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `identity_ledger_refresh($1::jsonb)` | 5 | 283 s | 732 s | 2026-11-06 | under 60 s steady state | Missed |
| `identity_ledger_refresh($1::jsonb)` | 8 | 62 s | 121 s | 2026-11-03 | under 60 s | Missed on the mean by 2 s, max 121 s |
| `identity_ledger_refresh($1::jsonb)` | 5 | 92 s | 138 s | 2026-10-31 | under 60 s | Missed |
| `identity_ledger_refresh()` (latest, 2026-11-09 03:15) | 1 | 461 s | 461 s | 2026-11-09 | under 60 s | **Missed**; the one latest run |
| `identity_ledger_rebuild()` | 4 | 399 s | 586 s | 2026-11-06 | none | n/a (full rebuild) |
| `graph.refresh_vertex_degree()` | 1 each | 228 s, 216 s | 228 s | 2026-11-06, 11-05 | none | n/a |
| `genre_stats` insert | 1 | 2,098 s (8,576 s on 11-05) | | 2026-11-06 | no longer than today after trends | baseline set; the 11-06 run is 4.1x faster than the 11-05 run |
| `style_stats` insert | 1 | 2,106 s (4,380 s on 11-05) | | 2026-11-06 | same | baseline set |
| `REFRESH MATERIALIZED VIEW CONCURRENTLY identity_ledger_density` | 7 and 4 | 47 s, 70 s | 73 s, 85 s | 11-04, 11-01 | none | n/a (pre-incremental view jobs) |
| `identity_ledger_maintain()` | 1 | 26 s | 26 s | 2026-11-07 | none | n/a |

Composition of the refresh and rebuild (statements, same period):

| Statement | Calls | Mean | Max | Share of rebuild+refresh calls |
| --- | ---: | ---: | ---: | --- |
| `item_alias` CTE insert (items by alias) | 4 | 180 s | 249 s | largest single statement |
| `identity_ledger_edition_parents` insert | 4 | 143 s | 279 s | second |
| `identity_ledger_work_summary` insert | 4 | 32 s | 56 s | |
| `identity_ledger_work_summary_day` insert (2026-11-09 03:14) | 1 | 36 s | 36 s | part of the 461 s run |

Readings:

- **"Incremental ledger refresh under 60 s" is not shown to hold.** The last refresh (2026-11-09 03:15) ran 461 s, and
  `identity_ledger_refresh_state` records `duration` 9 ms for its own bookkeeping row, which is not the work (read it as a state record). The
  one-call, one-day change is the run to explain; the per-day rollup statement inside it is 36 s, so about 425 s of the 461 s sits in
  statements this run did not isolate. Roadmap item 4's "may show incremental does not hold" is borne out for this call. A
  `pg_stat_statements` entry for the refresh function does not break down its inner statements per call; the next step is the server log
  at `log_min_duration_statement` 1 s for the 2026-11-09 03:14 window.
- **The whole-ledger rebuild is dominated by two statements**: alias resolution (180 s) and `edition_parents` (143 s), about 86% of the
  four statements listed above. Those are the ones to look at first if the refresh recomputes them.
- Genre and style stats each take about 35 min on the 2026-11-06 run. The 11-05 runs (8,576 s, 4,380 s) were earlier runs; why they took longer was not established.
  Roadmap C3 asks that the yearly counts fold into the same pass without lengthening it; this is the figure to compare against.
- The `completeness` count statement and the embedding edge queries (item 9) did not exceed the 5 s filter in this read and are not
  re-timed here; the 2026-11-07 figures stand.

## Findings for the planner or operator (not filed)

1. **Search is 1.6 to 11.8 s first-uncached on v0.11.1** against 1 s (100 ms for masters). Re-measure on v0.11.2 in the slot above.
2. **Genre trends first visit is 3.8 s against 1 s**; roadmap item 3 is the fix (stored per-year counts).
3. **`identity_ledger_refresh` is 461 s on its latest run against a 60 s target.** Isolate its inner statements for that run.
4. **The ledger window route is unmeasured** (admin token). `PERF_AUTH_HEADER` supports it when an operator runs the script.
5. **Master search: route 651 ms against a 39 ms statement.** The 600 ms gap is the route, not the statement; check what the route does
   beyond the ranked read on first request.

## Re-running

```sh
PERF_CONFIRM_LIVE=yes PERF_API_BASE=<origin> scripts/perf-baseline.sh roadmap   # route p95 vs the roadmap targets
PERF_CONFIRM_LIVE=yes PERF_PSQL_CMD='<psql on stdin, groovemap database>' scripts/perf-baseline.sh maintenance
```

Both are read-only; `roadmap` sends GET requests only. Raw outputs stay outside the repository (`perf-baseline-results/` is gitignored).
