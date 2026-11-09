-- Read-only maintenance-duration report. Run by `scripts/perf-baseline.sh maintenance` inside BEGIN READ ONLY ... ROLLBACK.
-- Needs pg_stat_statements. Durations are cumulative per statement entry since stats_since; calls cannot be split by code version,
-- so read mean and max next to stats_since. Nothing here writes or resets statistics.
\echo == maintenance statements over 5 s mean, by total time
SELECT left(regexp_replace(query, '\s+', ' ', 'g'), 100) AS statement, calls,
       round(total_exec_time / 1000) AS total_s, round((mean_exec_time / 1000)::numeric, 1) AS mean_s,
       round((max_exec_time / 1000)::numeric, 1) AS max_s, round(temp_blks_written * 8 / 1024.0) AS temp_mb,
       round(wal_bytes / 1048576.0) AS wal_mb, stats_since::timestamp(0)
FROM pg_stat_statements
WHERE mean_exec_time > 5000
  AND query ~* 'identity_ledger|refresh_vertex_degree|genre_stats|style_stats|completeness|refresh_artist_member|loader_derived|REFRESH MATERIALIZED'
ORDER BY total_exec_time DESC LIMIT 40;
\echo == composition of identity_ledger_refresh: statements that name identity_ledger tables
SELECT left(regexp_replace(query, '\s+', ' ', 'g'), 100) AS statement, calls,
       round((mean_exec_time / 1000)::numeric, 1) AS mean_s, round((max_exec_time / 1000)::numeric, 1) AS max_s,
       round(total_exec_time / 1000) AS total_s, rows, stats_since::timestamp(0)
FROM pg_stat_statements
WHERE query ~* 'identity_ledger' AND query !~* '^SELECT (public\.)?identity_ledger_(refresh|rebuild|maintain)'
ORDER BY total_exec_time DESC LIMIT 20;
\echo == ledger refresh state
SELECT * FROM public.identity_ledger_refresh_state;
\echo == extension stats window
SELECT min(stats_since)::timestamp(0) AS oldest, max(stats_since)::timestamp(0) AS newest, count(*) AS entries FROM pg_stat_statements;
