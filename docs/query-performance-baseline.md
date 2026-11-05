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

   **Who owns it.** This repository owns the reviewed script, [`scripts/pg-stat-statements.sql`](../scripts/pg-stat-statements.sql),
   and this runbook. The **server owner (the homelab operator, who holds superuser) runs it**, once per database, after approving.
   It is not a `database-schema` migration: `pg_stat_statements` is not a trusted extension, so a role without superuser cannot
   `CREATE EXTENSION` it, and schema migrations run as the application role. It is not run by `just deploy`, CI or `just check`.
   A bundled stack (`docker-compose.prod.yml` preloads the library) gets it the same way, by running the script as the container's
   `postgres` superuser; no `initdb` hook is added, because one fires only on an empty data directory and would hide the
   existing-volume case.

   ```sh
   # as a superuser, against the groovemap database only; <reader> is the role that runs perf-baseline.sh postgres
   psql -v ON_ERROR_STOP=1 -v reader=<reader> -d groovemap -f scripts/pg-stat-statements.sql
   ```

   **Two security conditions.** (1) `pg_stat_statements` keeps the text of utility statements by default
   (`pg_stat_statements.track_utility = on`), and on PostgreSQL 18 that text includes the literal in `CREATE ROLE ... PASSWORD '...'` and
   `ALTER ROLE ... PASSWORD '...'` (verified on `postgres:18-alpine`: the plaintext password is readable in the view). The script therefore
   refuses to run while it is on. Before running it, the server owner sets it off, which needs a reload and no restart
   (`ALTER SYSTEM SET pg_stat_statements.track_utility = off; SELECT pg_reload_conf();`), and must purge entries
   already collected (`pg_stat_statements_reset()` is a separate approval, and any password set while tracking was on should be rotated):
   the script also refuses, rolling back, while any stored statement mentions a password.
   The production overlay does not set it, because changing the Postgres command line restarts the service; add
   `-c pg_stat_statements.track_utility=off` to it at the next planned restart. (2) `pg_read_all_stats` is cluster-wide: it shows every
   role's statements in every database. Grant it only to a dedicated reporting role, never to an application or loader role, and undo it with
   `REVOKE pg_read_all_stats FROM <reader>`.

   The script checks `shared_preload_libraries` first (no restart is needed, and it refuses to create views that would stay
   empty), runs `CREATE EXTENSION IF NOT EXISTS pg_stat_statements` and `GRANT pg_read_all_stats TO <reader>` in one transaction, and
   prints the installed version and whether the reader holds the grant. `pg_read_all_stats` lets the report see statements run by
   other roles (the loaders, the API); GrooveMap's roles need no other new privilege. Re-running it changes nothing. To undo:
   `DROP EXTENSION pg_stat_statements;` (the preloaded library then keeps collecting but nothing can read it). Check first with
   `SHOW shared_preload_libraries;` and `SHOW pg_stat_statements.track;`.

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
| `pg_stat_statements`, `track=all`, `max=10000` | **Creating the extension adds no cost**: the preloaded library already counts every statement in shared memory, and `CREATE EXTENSION` only adds the views and functions that read it. Measured on `postgres:18-alpine` (8 clients, 20 s runs, 3 interleaved rounds, pgbench scale 20, `track_io_timing=on` as on lux): `SELECT`-only 56,169 / 66,547 / 61,295 tps without the extension versus 57,749 / 68,336 / 57,610 with it, and TPC-B-like 7,422 / 6,983 / 7,697 versus 7,581 / 7,242 / 7,642. The difference is inside run-to-run noise (about 10%). The *preload itself* (what lux already pays) measured about 3 to 8% on `SELECT`-only (78,397 / 79,295 / 81,152 tps without versus 75,982 / 75,737 / 74,590 with) and was indistinguishable on TPC-B-like (6,331 / 7,543 / 7,747 versus 5,318 / 7,930 / 8,182). A laptop container is a different host; confirm on lux by comparing the `api` summary before and after step 1. Shared memory scales with `pg_stat_statements.max`. |
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
