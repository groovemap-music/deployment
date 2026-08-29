#!/usr/bin/env bash
set -euo pipefail

# Repair existing Discogs release/master years in Neo4j and PostgreSQL.
# The default mode reports counts only; --apply is required to mutate data.

APPLY=0
case "${1:-}" in
"") ;;
--apply) APPLY=1 ;;
*) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

NEO4J_CONTAINER="${NEO4J_CONTAINER:-groovemap-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-groovemap-postgres}"
POSTGRES_USER="${POSTGRES_USER:-groovemap}"
POSTGRES_DB="${POSTGRES_DB:-groovemap}"
MIN_YEAR=1860
MAX_YEAR="$(($(date +%Y) + 1))"

if [[ -n "${NEO4J_PASSWORD_FILE:-}" ]]; then
  [[ -f "$NEO4J_PASSWORD_FILE" ]] || { echo "NEO4J_PASSWORD_FILE is not readable" >&2; exit 2; }
  IFS= read -r NEO4J_PASSWORD <"$NEO4J_PASSWORD_FILE"
else
  NEO4J_PASSWORD="${NEO4J_PASSWORD:-groovemap}"
fi
export NEO4J_USER NEO4J_PASSWORD
trap 'unset NEO4J_PASSWORD' EXIT

run_neo4j() {
  docker exec --env NEO4J_USER --env NEO4J_PASSWORD "$NEO4J_CONTAINER" \
    sh -c 'exec cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" "$@"' cypher-shell "$@"
}

run_postgres() {
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A "$@"
}

docker inspect --format '{{.State.Running}}' "$NEO4J_CONTAINER" | grep -qx true
docker inspect --format '{{.State.Running}}' "$POSTGRES_CONTAINER" | grep -qx true

printf 'Plausible release-year range: [%s, %s]\n' "$MIN_YEAR" "$MAX_YEAR"
((APPLY == 1)) && echo "APPLY mode enabled." || echo "DRY RUN: no data will be changed."

for label in Release Master; do
  count="$(run_neo4j --format plain \
    "MATCH (n:${label}) WHERE n.year IS NOT NULL AND (n.year < ${MIN_YEAR} OR n.year > ${MAX_YEAR}) RETURN count(n);" | tail -1)"
  printf 'Neo4j %s: %s affected node(s)\n' "$label" "$count"
  if ((APPLY == 1)) && ((count > 0)); then
    run_neo4j "MATCH (n:${label})
      WHERE n.year IS NOT NULL AND (n.year < ${MIN_YEAR} OR n.year > ${MAX_YEAR})
      CALL { WITH n SET n.year = null } IN TRANSACTIONS OF 50000 ROWS;"
  fi
done

for table in releases masters; do
  count="$(run_postgres -c \
    "SELECT count(*) FROM ${table} WHERE data->>'year' ~ '^[0-9]+$' AND ((data->>'year')::int < ${MIN_YEAR} OR (data->>'year')::int > ${MAX_YEAR});")"
  printf 'PostgreSQL %s: %s affected row(s)\n' "$table" "$count"
  if ((APPLY == 1)) && ((count > 0)); then
    run_postgres -c \
      "UPDATE ${table} SET data = jsonb_set(data, '{year}', 'null'::jsonb)
       WHERE data->>'year' ~ '^[0-9]+$' AND ((data->>'year')::int < ${MIN_YEAR} OR (data->>'year')::int > ${MAX_YEAR});"
  fi
done

((APPLY == 1)) && echo "Cleanup complete." || echo "Dry run complete; review counts before using --apply."
