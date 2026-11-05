# Query performance baseline

How to collect query statistics from PostgreSQL and Neo4j, what each step costs, and who has to approve it.
This is the procedure behind the query performance audit; the dated results live in
[`query-performance-audit-2026-11-04.md`](query-performance-audit-2026-11-04.md) and later
`query-performance-baseline-<date>.md` files. Nothing here runs in `just check` or CI.

**Nothing is enabled by this repository.** Every step marked *approval* changes or loads a live environment and waits for the operator.

## What is collected where

| Signal | Source | Needs |
| --- | --- | --- |
| Postgres: top statements by total time, mean time, calls, rows, temp spill, JIT | `pg_stat_statements` | step 1 |
| Postgres: slow single statements | server log, `log_min_duration_statement` (already 1 s on lux) | nothing |
| Neo4j: plan, DB hits and time of a named Cypher case | `PROFILE` through `scripts/perf-baseline.sh neo4j` | step 3 |
| Neo4j: what is running right now | `SHOW TRANSACTIONS` (available in Community) | read-only role |
| Per-route p50/p95 and cold versus warm | `scripts/perf-baseline.sh api` | step 4 |
| Neo4j: every query over a threshold | **not available**: the query log is Enterprise-only | catalog-api instrumentation, see the audit |

Neo4j runs the Community edition here (`neo4j:<version>-community` in `docker-compose.yml`), and the operations manual marks
the query log as Enterprise-only. `NEO4J_db_logs_query_*` settings were therefore removed from the base and production overlays; `/logs/query.log` on lux is 0 bytes.
Do not rely on a query log. Slow Cypher has to be measured from the API side or profiled by hand.

## Procedure

1. **Postgres: create the extension** *(approval; owner of the shared server)*. The library is already preloaded on lux. The
   extension is not created in the `groovemap` database, and nothing in this repository creates it either, so the views do not exist.
   Run as a superuser, in the `groovemap` database only:

   ```sql
   SELECT name, default_version, installed_version FROM pg_available_extensions WHERE name = 'pg_stat_statements';
   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
   GRANT pg_read_all_stats TO <the role that runs the report>;
   ```

   `pg_stat_statements` is not a trusted extension, so a database owner without superuser cannot run `CREATE EXTENSION`. The shared
   server is owned by the homelab operator; GrooveMap's role needs no new privilege other than `pg_read_all_stats`, which lets
   the report see statements run by other roles (the loaders, the API). It needs no restart, because the library is preloaded.
   Check first with `SHOW shared_preload_libraries;` and `SHOW pg_stat_statements.track;`.

2. **Postgres: capture twice instead of resetting.** Take one report before the measured window and one after, and subtract
   (`calls`, `total_exec_time`, `rows`). Do not call `pg_stat_statements_reset()` unless the operator asks for it: it discards the
   history other people may be reading, and it is a separate approval. Leave `pg_stat_statements.track_planning` off (the
   PostgreSQL manual warns of a noticeable penalty under many connections) and `track_io_timing` as it is until a separate
   decision; turning it on needs a fast clock source (`pg_test_timing`).

3. **Neo4j: profile the named cases** *(approval)*. `scripts/perf-baseline.sh neo4j` runs six `PROFILE` cases one at a time with a
   120 second cap each and stops at the first failure or timeout. It needs a command that runs `cypher-shell --access-mode read`
   and keeps the password inside the container (see the script header). `PROFILE` executes the statement, so it costs what the
   query costs: the `Not On Label` and `Rock` cases traverse millions of relationships. Run it off-peak, never while an import or
   enricher is writing, and stop if `SHOW TRANSACTIONS` shows other work queueing.

4. **API journeys** *(approval)*. `scripts/perf-baseline.sh api` sends GET requests only, one at a time, 2.2 seconds apart (under the
   30 per minute limits). Per journey it reports the first request as cold (before Valkey holds the result) and the remaining ones
   as warm, with p50, p95 and max. It stops a journey on a 429, a 5xx or a connection failure. `PERF_CONFIRM_LIVE=yes` is
   required so the script cannot start by accident. Authenticated journeys (`/api/fit/...`, `/api/user/...`) are deliberately
   absent: they need a user's session, and a baseline must not borrow one.

5. **Postgres report** *(approval)*. `scripts/perf-baseline.sh postgres` pipes `scripts/perf-baseline.sql` into `psql` inside
   `BEGIN READ ONLY ... ROLLBACK`: settings, table sizes, top 20 by total, mean, calls and rows, spill and JIT, cache hit ratio,
   unused indexes.

6. **Write the dated report** with the commands above, the image digests, the row counts at the time (load state matters), and the
   raw files kept outside the repository (`perf-baseline-results/` is gitignored; reports carry no query parameters from users).

## Overhead

| Step | Cost |
| --- | --- |
| `pg_stat_statements`, `track=all`, `max=10000` | One shared-memory hash update per statement. The PostgreSQL manual gives no figure; it is commonly a low single-digit percentage of CPU. **Not measured here**: confirm it by comparing the `api` summary before and after step 1. Shared memory scales with `pg_stat_statements.max`. |
| `track_planning` | Off. The manual warns of a noticeable penalty with many concurrent connections. Do not enable. |
| Reading the views and `perf-baseline.sql` | A few catalog reads; the table list sorts `pg_total_relation_size` over user tables. Negligible beside the workload. |
| `PROFILE` | The full cost of the statement plus a small per-operator counting overhead. The cases range from about 3 seconds to the 120 second cap. |
| API journeys | 35 journeys x 5 requests, 2.2 s apart: about 6 minutes of serial traffic, plus whatever the slow journeys take. |

## PgBouncer

A PgBouncer in front of Postgres must pool by session (`docs/homelab.md`): catalog-api holds a session-level advisory lock. In
session mode:

- `pg_stat_statements` is unaffected: it counts per server backend, keyed by role, database and query id. Statements from every
  client of the pooler appear once, normalised.
- `pg_stat_statements` does **not** show time a client waited for a pooled connection, and it has no application name. A route whose
  p95 is larger than the sum of its statements is waiting for a pool slot (the stack's pools are api 2 to 8, loaders 2 to 12,
  insights 1 to 4); read `SHOW POOLS` (`cl_waiting`, `maxwait`) and `SHOW STATS` (`avg_wait_time`) on the pooler's admin console.
- To attribute statements to a service, give each its own role: that is the only per-service key `pg_stat_statements` keeps.

Reading the pooler's admin console is a separate, read-only approval.
