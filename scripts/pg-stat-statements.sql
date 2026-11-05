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

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
GRANT pg_read_all_stats TO :"reader";

-- Verify: one row, and the reader can see other roles' statements once it has the grant.
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_stat_statements';
SELECT pg_has_role(:'reader', 'pg_read_all_stats', 'member') AS reader_can_read_all_stats;

COMMIT;
