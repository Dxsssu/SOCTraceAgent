# ExCyTIn-Bench Semantic Memory

This directory contains the scenario-independent semantic memory generated
from the actual `.meta` schemas bundled in
`data/excytin-bench/huggingface/data.zip`. It does not read event rows,
investigation labels, attack scenarios, or benchmark answers.

## Graph model

```text
(Environment)
    -[:HAS_CATALOG]-> (LogCatalog)
        -[:CONTAINS_TABLE]-> (LogTable)
            -[:HAS_FIELD]-> (Field)
                -[:MAPS_TO_JOIN_KEY]-> (JoinKey)
        -[:DEFINES_JOIN_KEY]-> (JoinKey)
```

`JoinKey` is a compact semantic anchor such as `device_id`, `alert_id`, or
`ip_address`. It avoids creating a dense all-pairs graph between compatible
fields. Each mapping records its confidence, matching method, and required
scope (for example tenant or time window).

## Files

- `semantic_memory.json`: generated knowledge snapshot committed with the code.
- `knowledge.schema.json`: JSON Schema for the snapshot format.
- `build_knowledge.py`: deterministic schema extractor and knowledge builder.
- `import_neo4j.py`: idempotent importer for the local Neo4j database.

## Rebuild JSON

From the repository root:

```bash
uv run python -m \
  src.memory.longterm_memory.semantic_memory.excytin_bench.build_knowledge
```

## Import into Neo4j

Start the database using `docker/neo4j/README.md`, then load its local
credentials and run the importer:

```bash
set -a
source docker/neo4j/.env
set +a
uv run python -m \
  src.memory.longterm_memory.semantic_memory.excytin_bench.import_neo4j
```

Re-running the command replaces only the catalog identified by
`excytin-schema-v1`; other catalogs are not modified.

## Example exact queries

List tables and their time fields:

```cypher
MATCH (:LogCatalog {catalog_id: 'excytin-schema-v1'})
      -[:CONTAINS_TABLE]->(table:LogTable)
RETURN table.name, table.log_type, table.default_time_field
ORDER BY table.name;
```

Find the schema for a table:

```cypher
MATCH (:LogTable {catalog_id: 'excytin-schema-v1', name: 'DeviceProcessEvents'})
      -[:HAS_FIELD]->(field:Field)
RETURN field.name, field.data_type, field.semantic_type, field.description_zh
ORDER BY field.name;
```

Find fields that can be joined through the same semantic key:

```cypher
MATCH (:LogTable {name: 'AlertInfo'})-[:HAS_FIELD]->(source:Field)
      -[:MAPS_TO_JOIN_KEY]->(key:JoinKey)
      <-[:MAPS_TO_JOIN_KEY]-(candidate:Field)<-[:HAS_FIELD]-(table:LogTable)
WHERE source <> candidate
RETURN source.name, key.name, table.name, candidate.name,
       key.match_method, key.join_scope, key.confidence
ORDER BY key.name, table.name;
```

The importer creates exact-property indexes and full-text indexes. The JSON
also stores `semantic_text` for tables and fields so embeddings can be added
later after an embedding model and vector dimension are selected.
