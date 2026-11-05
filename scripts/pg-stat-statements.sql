-- Create pg_stat_statements in the groovemap database, idempotently, and grant read access.
--
-- Run ONCE, by the owner of the Postgres server (a superuser), and only after the operator approves
-- (docs/query-performance-baseline.md, step 1). Nothing in this repository runs it: not `just check`,
-- not CI, not `just deploy`. It needs no restart, because the library is already preloaded
-- (docker-compose.prod.yml: shared_preload_libraries). It writes no rows and resets nothing.
--
--   psql -v ON_ERROR_STOP=1 -v reader=<role that runs the report> -d groovemap -f scripts/pg-stat-statements.sql
--
-- `reader` must be passed; without it psql stops here instead of granting to a guessed role.
-- pg_read_all_stats is CLUSTER-WIDE, not per database: it shows the text and timings of every role's
-- statements in every database on the server. Grant it only to a dedicated reporting role, never to an
-- application or loader role. To undo: REVOKE pg_read_all_stats FROM <reader>;

\if :{?reader}
\else
  \echo 'pass -v reader=<role>' 
  \quit
\endif

BEGIN;

-- Fail loudly, not silently, if the library is not preloaded: the views would exist but stay empty.
DO $$
BEGIN
  IF current_setting('shared_preload_libraries') NOT LIKE '%pg_stat_statements%' THEN
    RAISE EXCEPTION 'pg_stat_statements is not in shared_preload_libraries; the view would stay empty';
  END IF;
END
$$;

-- Utility statements are tracked by default, and their text includes literals: on PostgreSQL 18 a
-- `CREATE ROLE x PASSWORD '...'` or `ALTER ROLE x PASSWORD '...'` is stored with the plaintext password
-- (verified), readable by every holder of pg_read_all_stats. Refuse until utility tracking is off.
DO $$
BEGIN
  IF current_setting('pg_stat_statements.track_utility') = 'on' THEN
    RAISE EXCEPTION 'pg_stat_statements.track_utility is on: role passwords in DDL would be readable through the view. As a superuser run ALTER SYSTEM SET pg_stat_statements.track_utility = off; SELECT pg_reload_conf(); (no restart), then re-run';
  END IF;
END
$$;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- Turning tracking off does not purge what the preloaded library has already collected (it has been
-- counting since the server started). Superuser sees every role's text, so this covers all roles. If any
-- stored statement mentions a password, roll back (the extension and the grant with it): the server owner
-- decides on pg_stat_statements_reset() (a separate approval) and rotates those passwords, then re-runs.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_stat_statements WHERE query ~* '\m(password|encrypted)\M') THEN
    RAISE EXCEPTION 'stored statements already contain password text; turning track_utility off does not purge them. Reset with pg_stat_statements_reset() (separate approval), rotate those passwords, then re-run';
  END IF;
END
$$;

GRANT pg_read_all_stats TO :"reader";

-- Verify: one row, and the reader can see other roles' statements once it has the grant.
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_stat_statements';
SELECT pg_has_role(:'reader', 'pg_read_all_stats', 'member') AS reader_can_read_all_stats;

COMMIT;
