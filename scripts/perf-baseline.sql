-- Read-only Postgres baseline report. Run by `scripts/perf-baseline.sh postgres` inside BEGIN READ ONLY ... ROLLBACK.
-- Needs pg_stat_statements created in the groovemap database (docs/query-performance-baseline.md, step 1). Nothing here
-- writes, resets statistics or changes settings. Query text is normalised by pg_stat_statements (constants become $n).
\pset pager off
\pset null '-'

\echo '== settings that shape plans (SHOW values as the server runs them)'
SELECT name, setting, unit, source
FROM pg_settings
WHERE name IN ('shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem', 'random_page_cost', 'seq_page_cost',
               'effective_io_concurrency', 'jit', 'jit_above_cost', 'max_parallel_workers_per_gather', 'max_parallel_workers',
               'default_statistics_target', 'max_connections', 'huge_pages', 'log_min_duration_statement', 'track_io_timing',
               'shared_preload_libraries', 'pg_stat_statements.max', 'pg_stat_statements.track', 'pg_stat_statements.track_planning',
               'autovacuum_max_workers', 'max_wal_size')
ORDER BY name;

\echo '== collection window (when statistics were last reset; compare with the load window)'
SELECT stats_reset, dealloc FROM pg_stat_statements_info;

\echo '== table sizes and live-row estimates (the scale the numbers below were measured at)'
SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS total_size, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 25;

\echo '== top 20 statements by total execution time'
SELECT calls, round(total_exec_time::numeric, 0) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, round(max_exec_time::numeric, 0) AS max_ms,
       rows, shared_blks_read, temp_blks_written, left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC LIMIT 20;

\echo '== top 20 statements by mean execution time (calls >= 5)'
SELECT calls, round(mean_exec_time::numeric, 2) AS mean_ms, round(stddev_exec_time::numeric, 2) AS stddev_ms, rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND calls >= 5
ORDER BY mean_exec_time DESC LIMIT 20;

\echo '== top 20 statements by calls'
SELECT calls, round(total_exec_time::numeric, 0) AS total_ms, round(mean_exec_time::numeric, 3) AS mean_ms, rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY calls DESC LIMIT 20;

\echo '== top 20 statements by rows returned or affected'
SELECT rows, calls, round(mean_exec_time::numeric, 2) AS mean_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY rows DESC LIMIT 20;

\echo '== statements that spill to temp files (work_mem too small for them) or compile with JIT'
SELECT calls, temp_blks_written, round((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 0) AS jit_ms,
       round(total_exec_time::numeric, 0) AS total_ms, left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND (temp_blks_written > 0 OR jit_functions > 0)
ORDER BY temp_blks_written DESC, jit_ms DESC LIMIT 20;

\echo '== buffer cache hit ratio for the database (shared_buffers + OS cache is invisible here)'
SELECT datname, blks_hit, blks_read, round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS hit_pct, temp_files, temp_bytes
FROM pg_stat_database WHERE datname = current_database();

\echo '== indexes never used since the last stats reset (candidates only: a rare job may still need them)'
SELECT relname, indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes WHERE idx_scan = 0 ORDER BY pg_relation_size(indexrelid) DESC LIMIT 20;
