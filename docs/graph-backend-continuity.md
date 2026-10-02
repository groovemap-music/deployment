# Graph backend continuity and rollback

Neo4j is the production graph authority. The API no longer has a graph backend
selector: its Neo4j client is wired directly, and deployment continues to give
it the Neo4j host, credential, service dependency, and health gate. PostgreSQL
remains a relational store, and the PostgreSQL 19 plus pgvector embedding
program is independent of graph backend selection.

## Recovery record

The deployment baseline before this clarification is commit
`77449dd807ac97fa0d2dfbb1f4374f72b40b30df`. It contains no SQL/PGQ activation
variable, service, or deployment test, so there are no deployment paths to
archive. Git history is the recovery mechanism; do not create an archive
branch.

The reviewed source cleanup inputs are:

| Repository | PGQ removal revision |
| --- | --- |
| `database-schema` | `9a50949b1810f3e61adae2f89b86acec2e20c4a3` |
| `catalog-api` | `24c95868af6f0f5f28bc8bd61418b32ac0039185` |
| `discogs-sql-loader` | `baa289b6264d567384648d8c6ab7a013a9287325` |
| `musicbrainz-sql-loader` | `cd4e59931d586eab5d6ba0c2462ac4ce7853fc38` |

These source revisions are review inputs, not deployable image references.
Production promotion still requires each repository to publish the reviewed
revision and the operator to put its immutable registry manifest digest in the
corresponding `*_IMAGE` variable. Never substitute a Git SHA, mutable tag, or
guessed digest for that release step.

Inspect or recover the deployment baseline without creating a branch:

```sh
git show 77449dd807ac97fa0d2dfbb1f4374f72b40b30df:docker-compose.yml
git diff 77449dd807ac97fa0d2dfbb1f4374f72b40b30df..HEAD -- \
  docker-compose.yml docker-compose.prod.yml docs/graph-backend-continuity.md
```

For an upstream repository, inspect the parent of its removal revision to see
the retired implementation, for example:

```sh
git -C ../database-schema show 9a50949b1810f3e61adae2f89b86acec2e20c4a3^
git -C ../catalog-api show 24c95868af6f0f5f28bc8bd61418b32ac0039185^
git -C ../discogs-sql-loader show baa289b6264d567384648d8c6ab7a013a9287325^
git -C ../musicbrainz-sql-loader show cd4e59931d586eab5d6ba0c2462ac4ce7853fc38^
```

## Disposable continuity exercise

Run this only with approved release digests in an ignored environment file.
The exercise uses a unique Compose project and must never target a live
environment.

1. Render the production configuration without credentials or live services:

   ```sh
   docker compose --env-file config/validation.env \
     -f docker-compose.yml -f docker-compose.prod.yml config > /tmp/groovemap-graph-prod.yml
   grep -A60 '^  api:' /tmp/groovemap-graph-prod.yml | grep 'NEO4J_HOST: neo4j'
   if grep -q 'GRAPH_BACKEND' /tmp/groovemap-graph-prod.yml; then exit 1; fi
   ```

2. Copy `config/validation.env` to an ignored temporary env file and replace
   the four schema/API/SQL-loader placeholders with approved image digests
   published from the revisions above. Start the disposable media smoke using
   a unique project name as described in [Testing](testing-guide.md). The smoke
   proves schema initialization, both loaders and enrichers, and Neo4j-backed
   API reads through released images.

3. Before restart, write a sentinel through `cypher-shell` and read it through
   the API path under test. Stop and start only the disposable `neo4j` service,
   wait for its health check, and repeat both reads. The sentinel and catalog
   result must survive because the named Neo4j data volume remains attached.

4. Exercise failure behavior by stopping disposable `neo4j`. The API health or
   graph request must fail rather than fall through to PostgreSQL. Start Neo4j,
   wait for `service_healthy`, and confirm the same API query recovers.

5. Record the project name, image digests, source revisions, health output,
   pre/post-restart query result, failure result, recovery result, and cleanup
   output in the operator change record. Tear down with `docker compose down
   --volumes` for that exact project and verify its containers, network, and
   volumes are gone.

## Rollback

Rollback means restoring the exact pre-change deployment tree and previously
recorded image digests, not re-enabling SQL/PGQ. From a disposable checkout:

```sh
git worktree add /tmp/groovemap-deployment-rollback \
  77449dd807ac97fa0d2dfbb1f4374f72b40b30df
```

Render that checkout with the previously recorded digest-pinned environment,
then repeat the continuity exercise. A source or image rollback does not imply
a Neo4j data rollback. Preserve the Neo4j volume and complete the backup
evidence in [Maintenance](maintenance.md#backups-and-restore-drills) before
any stateful change; restore data only when the change record identifies an
incompatible data migration and its approved restore point.
