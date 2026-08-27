#!/usr/bin/env bash
set -euo pipefail

# Backfill precomputed Label aggregate properties in Neo4j.
# The default mode reports the remaining work; --apply is required to mutate data.

APPLY=0
case "${1:-}" in
"") ;;
--apply) APPLY=1 ;;
*) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

NEO4J_CONTAINER="${NEO4J_CONTAINER:-discogsography-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
if [[ -n "${NEO4J_PASSWORD_FILE:-}" ]]; then
  [[ -f "$NEO4J_PASSWORD_FILE" ]] || { echo "NEO4J_PASSWORD_FILE is not readable" >&2; exit 2; }
  IFS= read -r NEO4J_PASSWORD <"$NEO4J_PASSWORD_FILE"
else
  NEO4J_PASSWORD="${NEO4J_PASSWORD:-discogsography}"
fi
export NEO4J_USER NEO4J_PASSWORD
trap 'unset NEO4J_PASSWORD' EXIT

run_neo4j() {
  docker exec --env NEO4J_USER --env NEO4J_PASSWORD "$NEO4J_CONTAINER" \
    sh -c 'exec cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" "$@"' cypher-shell "$@"
}

docker inspect --format '{{.State.Running}}' "$NEO4J_CONTAINER" | grep -qx true
remaining="$(run_neo4j --format plain \
  'MATCH (l:Label) WHERE l.release_count IS NULL OR l.artist_count IS NULL OR l.genre_count IS NULL RETURN count(l);' | tail -1)"
printf 'Labels requiring aggregate statistics: %s\n' "$remaining"

if ((APPLY == 0)); then
  echo "DRY RUN: no data changed; review the count before using --apply."
  exit 0
fi

run_neo4j "CALL {
  MATCH (l:Label)
  CALL { WITH l MATCH (l)<-[:ON]-(r:Release) RETURN count(DISTINCT r) AS rc }
  CALL { WITH l MATCH (l)<-[:ON]-(r:Release)-[:BY]->(a:Artist) RETURN count(DISTINCT a) AS ac }
  CALL { WITH l MATCH (l)<-[:ON]-(r:Release)-[:IS]->(g:Genre) RETURN count(DISTINCT g) AS gc }
  SET l.release_count = rc, l.artist_count = ac, l.genre_count = gc
} IN TRANSACTIONS OF 100 ROWS;"
echo "Label statistics backfill complete."
