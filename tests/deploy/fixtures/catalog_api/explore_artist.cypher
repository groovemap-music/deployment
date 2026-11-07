MATCH (a:Artist {name: $name})
CALL {
    WITH a
    OPTIONAL MATCH (a)-[:ALIAS_OF]->(alias:Artist)
    WITH a, collect(DISTINCT alias.id) AS alias_ids
    OPTIONAL MATCH (a)-[:MEMBER_OF]->(grp:Artist)
    WITH a, alias_ids, collect(DISTINCT grp.id) AS group_ids
    OPTIONAL MATCH (m:Artist)-[:MEMBER_OF]->(a)
    WITH alias_ids, group_ids, collect(DISTINCT m.id) AS member_ids
    UNWIND (alias_ids + group_ids + member_ids) AS related_id
    WITH DISTINCT related_id WHERE related_id IS NOT NULL
    RETURN count(related_id) AS alias_count
}
RETURN a.id AS id, a.name AS name,
       a.release_count AS release_count,
       a.label_count AS label_count,
       alias_count
