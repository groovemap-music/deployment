# Route p95 and maintenance durations, 2026-11-09

Bead `gm-deployment-habq`, roadmap item 4 (`design` `docs/audits/performance-roadmap-2026-11-09.md`). First route percentiles, and the
first maintenance durations read from `pg_stat_statements`. Procedure: [query-performance-baseline.md](query-performance-baseline.md).
Prior state: [2026-11-07 baseline](query-performance-baseline-2026-11-07.md).

## State when this was measured

- Read-only on lux. Routes: 2026-11-09 18:20 UTC, from the operator session's read access to the API, with
  `scripts/perf-baseline.sh roadmap` (`PERF_ITERATIONS=5`, `PERF_PAUSE=2.2`): one request at a time, GET only, 2.2 s apart.
  Maintenance: 2026-11-09 about 18:10 UTC through `gm-lux-sql`, one statement at a time.
- **catalog-api v0.11.1 for the route table above.** Search on the new side tables and exact facets (v0.11.2) was not live at that point; the
  search rows there are the **v0.11.1 baseline**. v0.11.2 was measured afterwards; see "After v0.11.2" below. Delayed by a GitHub Actions outage.
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

- **Search was the open gap on v0.11.1**, and the roadmap's statement figures did not carry to the route. Filtered `love` was 0.67 s as a
  statement and is 11.8 s as a first route request; master search was 39 ms as a statement and is 651 ms. The route does more than that
  statement (several branches, facets, highlighting) and the first request reads uncached pages. On v0.11.2 the two `love` searches now meet
  1 s on first uncached request (702 ms and 778 ms), but master search is still 584 ms against 100 ms; see "After v0.11.2".
- **Genre trends stay at 3.8 s first visit**, the same as the 2026-11-07 traversal (3.8 s). It is cached afterwards (2 ms), so the cost is
  every first visit and evicted key, as roadmap item 3 says. Style (410 ms) and label (929 ms) pass.
- **The Valkey layer works**: warm requests are 2 to 5 ms everywhere except path.
- **Path Rock to Trance is not faster when repeated** (2.0 s, then 2.1 s), so a hub-to-hub path pays its full cost each time; it passes the
  5 s cap with 2.8 s to spare.
- Not measured here: the "authentication under concurrent cold searches" target (needs concurrent load, which this run does not generate).

### After v0.11.2

Measured on lux 2026-11-09 19:29 UTC, after the api recreate that deployed v0.11.2, with `scripts/perf-baseline.sh roadmap`
(`PERF_ITERATIONS=5`, `PERF_PAUSE=2.2`). The first-uncached figures are the ones this run recorded; the warm p95 was not recorded for these three
journeys, so it is left blank rather than estimated.

| Journey | Target | First uncached (v0.11.1 → v0.11.2) | Warm p95 | Verdict |
| --- | ---: | ---: | ---: | --- |
| Search `love`, facets and filters | 1 s | 11,751 ms → **702 ms** | not recorded | **Met** (first uncached) |
| Search `love`, all types | 1 s | 1,603 ms → **778 ms** | not recorded | **Met** (first uncached) |
| Master search `love` (`types=master`) | 100 ms | 651 ms → **584 ms** | not recorded | **Missed** (484 ms over; v0.11.2 improves it but does not meet it; see the catalog-api master-route bead) |

Other uncached terms on v0.11.2 (same run): moonlight, xylophone, blue train and paradise each took 49 to 133 ms.

Trends are unchanged in this run: the trends rows read 2 to 3 ms only because Valkey still cached them. Trends stay at the v0.11.1 numbers
(Rock 3,780 ms first visit) until discogs-graph-enricher v0.7.0 ships.

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
| `identity_ledger_refresh()` (latest, 2026-11-09 03:15) | 1 | 461 s | 461 s | 2026-11-09 | under 60 s | Not a steady-state run: the requested full rebuild during the v0.12.0 rollout (see readings) |
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

- **Ledger-refresh note: the 461 s run (2026-11-09 03:15) was the requested full rebuild during the v0.12.0 rollout, not a steady-state
  incremental refresh.** It does not measure the incremental refresh against the under-60 s target, either way. The steady-state refresh
  still has no isolated run in this record. The `identity_ledger_refresh($1::jsonb)` rows above (means 62 to 283 s) are the closest data,
  and they are not isolated per call either.
- The 461 s call's bookkeeping row in `identity_ledger_refresh_state` records `duration` 9 ms, which is not the work (read it as a state
  record). The per-day rollup statement inside the run is 36 s, so about 425 s of the 461 s sits in statements this run did not isolate. A
  `pg_stat_statements` entry for the refresh function does not break down its inner statements per call; the next step is the server log at
  `log_min_duration_statement` 1 s for the 2026-11-09 03:14 window.
- **The whole-ledger rebuild is dominated by two statements**: alias resolution (180 s) and `edition_parents` (143 s), about 86% of the
  four statements listed above. Those are the ones to look at first if the refresh recomputes them.
- Genre and style stats each take about 35 min on the 2026-11-06 run. The 11-05 runs (8,576 s, 4,380 s) were earlier runs; why they took longer was not established.
  Roadmap C3 asks that the yearly counts fold into the same pass without lengthening it; this is the figure to compare against.
- The `completeness` count statement and the embedding edge queries (item 9) did not exceed the 5 s filter in this read and are not
  re-timed here; the 2026-11-07 figures stand.

## Findings for the planner or operator (not filed)

1. **Search on v0.11.2: both `love` searches meet 1 s first-uncached (702 ms, 778 ms); master search is 584 ms against 100 ms.** On v0.11.1 they
   were 11.8 s, 1.6 s and 651 ms. The master-route gap is open; see the catalog-api master-route bead.
2. **Genre trends first visit is 3.8 s against 1 s**; roadmap item 3 is the fix (stored per-year counts).
3. **Steady-state `identity_ledger_refresh` has no isolated run against its 60 s target.** The 461 s run was the full rebuild for the v0.12.0
   rollout. Get a steady-state run and isolate its inner statements.
4. **The ledger window route is unmeasured** (admin token). `PERF_AUTH_HEADER` supports it when an operator runs the script.
5. **Master search: route 651 ms against a 39 ms statement.** The 600 ms gap is the route, not the statement; check what the route does
   beyond the ranked read on first request.

## Re-running

```sh
PERF_CONFIRM_LIVE=yes PERF_API_BASE=<origin> scripts/perf-baseline.sh roadmap   # route p95 vs the roadmap targets
PERF_CONFIRM_LIVE=yes PERF_PSQL_CMD='<psql on stdin, groovemap database>' scripts/perf-baseline.sh maintenance
```

Both are read-only; `roadmap` sends GET requests only. Raw outputs stay outside the repository (`perf-baseline-results/` is gitignored).
