# Neo4j for SOCAgent Semantic Memory

This directory contains the local Neo4j Community Edition deployment used by
the long-term semantic memory work.

## Start

From the repository root:

```bash
docker compose \
  --env-file docker/neo4j/.env \
  --file docker/neo4j/compose.yml \
  up -d
```

Neo4j Browser: <http://localhost:7474>

Bolt URI: `bolt://localhost:7687`

Username: `neo4j`

The local password is stored in the ignored `docker/neo4j/.env` file. Change
it before using this deployment outside a local development machine.

## Inspect

```bash
docker compose \
  --env-file docker/neo4j/.env \
  --file docker/neo4j/compose.yml \
  ps

docker compose \
  --env-file docker/neo4j/.env \
  --file docker/neo4j/compose.yml \
  logs -f neo4j
```

## Stop

```bash
docker compose \
  --env-file docker/neo4j/.env \
  --file docker/neo4j/compose.yml \
  down
```

The `down` command keeps the named data and log volumes. To remove them, use
`down --volumes` only when the stored graph data is no longer needed.

## Persistence

- Database data: Docker volume `socagent-neo4j-data`
- Neo4j logs: Docker volume `socagent-neo4j-logs`
- Import staging area: `docker/neo4j/import/`

No APOC or Graph Data Science plugin is required for Neo4j's native vector
indexes and Cypher `SEARCH` queries.
