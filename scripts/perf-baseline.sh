#!/usr/bin/env bash
# Read-only query-performance baseline workload for a running GrooveMap environment.
# Procedure, overhead and approvals: docs/query-performance-baseline.md. Never part of `just check` or CI.
#
#   scripts/perf-baseline.sh list                 # print the journeys; touches nothing
#   scripts/perf-baseline.sh api                  # time the API journeys (GET only), one request at a time
#   scripts/perf-baseline.sh neo4j                # PROFILE the heavy Cypher cases, serially, 120 s cap each
#   scripts/perf-baseline.sh postgres             # run scripts/perf-baseline.sql in a READ ONLY transaction
#
# Every command except `list` talks to a live service and needs operator approval first.
#
# Environment (all optional unless stated):
#   PERF_API_BASE        API origin (default http://127.0.0.1:8004)
#   PERF_ITERATIONS      requests per journey (default 5); the first is reported as cold, the rest as warm
#   PERF_PAUSE           seconds between requests (default 2.2: the 30/minute per-IP limits on search,
#                        autocomplete, credits and genre-tree stay unreached)
#   PERF_REQUEST_TIMEOUT per-request curl limit in seconds (default 130)
#   PERF_OUT             results directory (default perf-baseline-results/, gitignored)
#   PERF_CYPHER_CMD      required for `neo4j`: a command that reads Cypher on stdin and runs it read-only, e.g.
#                        ssh <host> "docker exec -i <neo4j container> sh -c 'exec cypher-shell -u neo4j -p \"\$(cat /run/secrets/neo4j_password)\" --access-mode read --format verbose'"
#                        (verbose prints the operator table with page cache hits and misses; plain prints only totals)
#                        The password stays inside the container; this script never reads, prints or stores it.
#   PERF_PSQL_CMD        required for `postgres`: a command that reads SQL on stdin and runs psql against the
#                        groovemap database as a role that may read pg_stat_statements
#   PERF_CONFIRM_LIVE    must be "yes" for api, neo4j and postgres: the operator approved this run
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="${PERF_API_BASE:-http://127.0.0.1:8004}"
iterations="${PERF_ITERATIONS:-5}"
pause="${PERF_PAUSE:-2.2}"
req_timeout="${PERF_REQUEST_TIMEOUT:-130}"
out="${PERF_OUT:-perf-baseline-results}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

# journey name | path with query string. GET only. Names with spaces are percent-encoded.
# Heavy cases the audit names: David Bowie (artist 10263), the Not On Label label, the Rock genre.
journeys=(
  "explore.artist.bowie|/api/explore?name=David%20Bowie&type=artist"
  "explore.label.not-on-label|/api/explore?name=Not%20On%20Label&type=label"
  "explore.genre.rock|/api/explore?name=Rock&type=genre"
  "explore.style.trance|/api/explore?name=Trance&type=style"
  "expand.artist.bowie.releases|/api/expand?node_id=10263&type=artist&category=releases&limit=50"
  "expand.artist.bowie.labels|/api/expand?node_id=10263&type=artist&category=labels&limit=50"
  "expand.genre.rock.releases|/api/expand?node_id=Rock&type=genre&category=releases&limit=50"
  "expand.label.not-on-label.releases|/api/expand?node_id=Not%20On%20Label&type=label&category=releases&limit=50"
  "trends.artist.bowie|/api/trends?name=David%20Bowie&type=artist"
  "trends.genre.rock|/api/trends?name=Rock&type=genre"
  "trends.label.not-on-label|/api/trends?name=Not%20On%20Label&type=label"
  "trends.style.trance|/api/trends?name=Trance&type=style"
  "path.bowie-to-cash|/api/path?from_name=David%20Bowie&from_type=artist&to_name=Johnny%20Cash&to_type=artist&max_depth=6"
  "path.bowie-to-indecent-noise|/api/path?from_name=David%20Bowie&from_type=artist&to_name=Indecent%20Noise&to_type=artist&max_depth=6"
  "path.rock-to-trance|/api/path?from_name=Rock&from_type=genre&to_name=Trance&to_type=style&max_depth=6"
  "search.bowie|/api/search?q=bowie"
  "search.rock|/api/search?q=rock"
  "search.not-on-label|/api/search?q=not%20on%20label"
  "search.bowie.filtered|/api/search?q=bowie&types=release&year_min=1970&year_max=1980"
  "autocomplete.artist.bow|/api/autocomplete?q=bow&type=artist"
  "autocomplete.label.not|/api/autocomplete?q=not&type=label"
  "insights.top-artists|/api/insights/top-artists"
  "insights.genre-trends|/api/insights/genre-trends"
  "insights.label-longevity|/api/insights/label-longevity"
  "insights.data-completeness|/api/insights/data-completeness"
  "genres.tree|/api/genre-tree"
  "genres.emergence.1990|/api/explore/genre-emergence?before_year=1990"
  "credits.person.bob-ludwig|/api/credits/person/Bob%20Ludwig"
  "credits.person.bob-ludwig.timeline|/api/credits/person/Bob%20Ludwig/timeline"
  "credits.person.bob-ludwig.profile|/api/credits/person/Bob%20Ludwig/profile"
  "credits.role.mastering.top|/api/credits/role/mastering/top?limit=20"
  "credits.shared.ludwig-flood|/api/credits/shared?person1=Bob%20Ludwig&person2=Flood"
  "credits.autocomplete.flo|/api/credits/autocomplete?q=flo"
)

die() { echo "perf-baseline: $*" >&2; exit 2; }

require_live_approval() {
  [[ "${PERF_CONFIRM_LIVE:-}" == "yes" ]] || die "this talks to a live service; set PERF_CONFIRM_LIVE=yes once the operator has approved the run"
}

[[ "$iterations" =~ ^[1-9][0-9]*$ ]] || die "PERF_ITERATIONS must be a positive integer"
[[ "$pause" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "PERF_PAUSE must be a number of seconds"

summarize() { # tsv -> per-journey n, ok, cold, warm p50/p95/max (nearest rank), in ms
  awk -F'\t' '
    NR == 1 { next }
    { j = $1; if (!(j in seen)) { order[++k] = j; seen[j] = 1 }
      if ($3 ~ /^2/) { ok[j]++; ms = $4 * 1000; if ($2 == 1) cold[j] = ms; else { w[j, ++nw[j]] = ms } } else bad[j]++ }
    function pct(j, p,   n, i, a, t, m, idx) {
      n = nw[j]; if (n == 0) return "-"
      for (i = 1; i <= n; i++) a[i] = w[j, i]
      for (i = 2; i <= n; i++) { t = a[i]; m = i - 1; while (m > 0 && a[m] > t) { a[m + 1] = a[m]; m-- } a[m + 1] = t }
      idx = int(p * n / 100); if (idx < p * n / 100) idx++; if (idx < 1) idx = 1
      return sprintf("%.0f", a[idx])
    }
    END {
      printf "%-40s %3s %4s %9s %9s %9s %9s\n", "journey", "ok", "bad", "cold_ms", "p50_ms", "p95_ms", "max_ms"
      for (i = 1; i <= k; i++) { j = order[i]
        printf "%-40s %3d %4d %9s %9s %9s %9s\n", j, ok[j], bad[j], (j in cold) ? sprintf("%.0f", cold[j]) : "-", pct(j, 50), pct(j, 95), pct(j, 100) } }' "$1"
}

run_api() {
  require_live_approval
  command -v curl >/dev/null || die "curl is required"
  mkdir -p "$out"
  local tsv="$out/api-$stamp.tsv"
  printf 'journey\titeration\thttp_code\ttime_total_s\n' >"$tsv"
  echo "perf-baseline: $((${#journeys[@]})) journeys x $iterations against $base, one request at a time, ${pause}s apart" >&2
  local entry name path i res code t
  for entry in "${journeys[@]}"; do
    name="${entry%%|*}"
    path="${entry#*|}"
    for ((i = 1; i <= iterations; i++)); do
      res="$(curl -sS -o /dev/null --max-time "$req_timeout" -w '%{http_code}\t%{time_total}' "$base$path" 2>/dev/null || printf '000\t0')"
      code="${res%%$'\t'*}"
      t="${res#*$'\t'}"
      printf '%s\t%s\t%s\t%s\n' "$name" "$i" "$code" "$t" >>"$tsv"
      if [[ "$code" == "429" ]]; then echo "perf-baseline: $name hit a rate limit (429); raise PERF_PAUSE" >&2; break; fi
      if [[ "$code" == "000" || "$code" == 5* ]]; then echo "perf-baseline: $name returned $code; stopping this journey so a struggling service is not hammered" >&2; break; fi
      sleep "$pause"
    done
  done
  echo "raw timings: $tsv" >&2
  summarize "$tsv" | tee "$out/api-$stamp.summary.txt"
}

# Cypher cases: name, then the statement. Mirror catalog-api's queries (api/queries/neo4j_queries.py) so the plans match
# what the API runs. PROFILE executes the statement, so each is a real read; none writes.
cypher_cases() {
  cat <<'EOF'
explore.artist.bowie
PROFILE MATCH (a:Artist {name: 'David Bowie'}) RETURN a.id AS id, COUNT { MATCH (r:Release)-[:BY]->(a) RETURN DISTINCT r } AS release_count, COUNT { MATCH (r:Release)-[:BY]->(a), (r)-[:ON]->(l:Label) RETURN DISTINCT l } AS label_count;
trends.artist.bowie
PROFILE MATCH (r:Release)-[:BY]->(a:Artist {name: 'David Bowie'}) WHERE r.year > 0 WITH r.year AS year, count(DISTINCT r) AS count RETURN year, count ORDER BY year;
trends.label.not-on-label
PROFILE MATCH (r:Release)-[:ON]->(l:Label {name: 'Not On Label'}) WHERE r.year > 0 WITH r.year AS year, count(DISTINCT r) AS count RETURN year, count ORDER BY year;
trends.genre.rock
PROFILE MATCH (g:Genre {name: 'Rock'}) CALL { WITH g MATCH (g)<-[:IS]-(r:Release) RETURN r.year AS year } WITH year WHERE year IS NOT NULL AND year > 0 WITH year, count(*) AS count RETURN year, count ORDER BY year;
path.bowie-to-cash
PROFILE MATCH (a:Artist {id: '10263'}), (b:Artist {name: 'Johnny Cash'}) MATCH p = shortestPath((a)-[:BY|ON|IS|ALIAS_OF|MEMBER_OF|DERIVED_FROM*..6]-(b)) RETURN length(p) AS hops;
expand.label.not-on-label.releases
PROFILE MATCH (l:Label {name: 'Not On Label'})<-[:ON]-(r:Release) RETURN count(r) AS releases;
EOF
}

run_neo4j() {
  require_live_approval
  [[ -n "${PERF_CYPHER_CMD:-}" ]] || die "set PERF_CYPHER_CMD (see the header of this script)"
  mkdir -p "$out"
  local log="$out/neo4j-$stamp.txt" name="" line
  : >"$log"
  while IFS= read -r line; do
    if [[ -z "$name" ]]; then name="$line"; continue; fi
    echo "perf-baseline: $name (120 s cap)" >&2
    printf '== %s\n' "$name" >>"$log"
    # One query at a time, never concurrently. A timeout kills the client, which ends the transaction server-side.
    if ! printf '%s\n' "$line" | timeout 120 bash -c "$PERF_CYPHER_CMD" >>"$log" 2>&1; then
      echo "perf-baseline: $name failed or hit the 120 s cap; stopping so the service is not loaded further" >&2
      exit 1
    fi
    name=""
    sleep "$pause"
  done < <(cypher_cases)
  echo "profiles: $log" >&2
}

run_postgres() {
  require_live_approval
  [[ -n "${PERF_PSQL_CMD:-}" ]] || die "set PERF_PSQL_CMD (see the header of this script)"
  mkdir -p "$out"
  local log="$out/postgres-$stamp.txt"
  { echo "BEGIN READ ONLY;"; cat "$root/scripts/perf-baseline.sql"; echo "ROLLBACK;"; } | bash -c "$PERF_PSQL_CMD" | tee "$log"
  echo "report: $log" >&2
}

case "${1:-}" in
  list) printf '%s\n' "${journeys[@]}" | tr '|' '\t'; echo; cypher_cases | paste - - | cut -f1 ;;
  api) run_api ;;
  neo4j) run_neo4j ;;
  postgres) run_postgres ;;
  *) die "usage: perf-baseline.sh list|api|neo4j|postgres" ;;
esac
