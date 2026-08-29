#!/usr/bin/env bash
set -euo pipefail

# Convert existing Neo4j Master.year string values to integers.
# The default mode reports counts only; --apply is required to mutate data.

APPLY=0
case "${1:-}" in
"") ;;
--apply) APPLY=1 ;;
*) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

NEO4J_CONTAINER="${NEO4J_CONTAINER:-groovemap-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
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

docker inspect --format '{{.State.Running}}' "$NEO4J_CONTAINER" | grep -qx true
remaining="$(run_neo4j --format plain \
  "MATCH (m:Master) WHERE m.year IS NOT NULL AND valueType(m.year) STARTS WITH 'STRING' RETURN count(m);" | tail -1)"
printf 'Master nodes with string years: %s\n' "$remaining"

if ((APPLY == 0)); then
  echo "DRY RUN: no data changed; review the count before using --apply."
  exit 0
fi

run_neo4j "MATCH (m:Master)
  WHERE m.year IS NOT NULL AND valueType(m.year) STARTS WITH 'STRING'
  CALL {
    WITH m
    WITH m, toInteger(m.year) AS int_year
    SET m.year = CASE WHEN int_year > 0 THEN int_year ELSE null END
  } IN TRANSACTIONS OF 50000 ROWS;"
echo "Master.year migration complete."
