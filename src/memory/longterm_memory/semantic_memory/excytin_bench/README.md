# ExCyTIn-Bench Semantic Memory

Semantic Memory stores scenario-independent knowledge of the available logs.
Version 2 keeps exactly five properties on each JSON table and field object.
Names, IDs and types come from the union of the 365 actual `.meta` files in
`data/excytin-bench/huggingface/data.zip`: 60 tables, 2324 fields and 18 join keys.
It does not read event rows, investigation labels or benchmark answers.

## JSON format

The snapshot root contains `format_version`, `generated_at`, `generation_policy`,
`environment`, `catalog`, `tables`, and `join_keys`. The format and catalog
versions are `2.0`; existing node IDs are retained so episodic and procedural
references remain valid.

Each object in `tables` has only:

- `table_id`: stable table identifier.
- `name`: exact queryable table name.
- `description`: English description for table selection.
- `description_zh`: equivalent Chinese description.
- `field`: array of field objects (singular key, replacing v1 `fields`).

Each object in `field` has only `field_id`, `name`, `data_type`, `description`,
and `description_zh`. The exact shape is enforced by `knowledge.schema.json`,
including rejection of extra properties. `data_type` preserves the most common
source `.meta` type for a field when schema variants disagree; it does not adopt
newer Microsoft documentation types or add columns missing from the export.

Table descriptions explain the record scope, investigation questions,
useful pivots, and differences from neighboring tables. Field descriptions
explain the specific object, actor, time, or result represented and how to use
it in an investigation. Examples include target versus initiating processes,
email sender headers versus envelope senders, and NAT original versus translated
destinations. Descriptions qualify interpretations where provider-specific
export fields lack an exact documented meaning. They do not invent JSON paths.

Descriptions were drafted with DeepSeek-V4.1-Flash against the actual schemas
and Microsoft Learn references, then reviewed and corrected. They are stored
in `descriptions.json`, including a source URL per table and supplementary
Defender XDR references. See, for example,
[DeviceProcessEvents](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/deviceprocessevents),
[Defender process fields](https://learn.microsoft.com/en-us/defender-xdr/advanced-hunting-deviceprocessevents-table),
and [AzureDiagnostics](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/azurediagnostics).

## Rebuild

```bash
uv run python -m \
  src.memory.longterm_memory.semantic_memory.excytin_bench.build_knowledge
```

The builder reads every `.meta` file, combines schema variants, loads the
reviewed description catalog, and writes `semantic_memory.json`. Rebuilds are
deterministic except for `generated_at` and need no LLM call or web request.
If a table or field is added or removed in the source, description coverage
must be updated explicitly; the builder fails instead of silently substituting
a generic description. No embedding or vector index is generated.

## Retrieval and other memories

Agent-facing semantic table/field objects use the same five-key shape and
include both descriptions. `runtime_view.py` derives the internal semantic
categories, text for lexical matching and join membership needed by existing
retrieval and episodic/procedural algorithms. These internal properties are
not stored on v2 JSON table/field objects. Exact schema-name matching uses
identifier boundaries; `Id` does not match the word `Identify`.

`join_keys` remains a separate root-level collection. Its IDs and field
references are retained. Cross-service identity namespaces must still be
verified when applying a suggested join.

## Neo4j

```text
(Environment)-[:HAS_CATALOG]->(LogCatalog)
(LogCatalog)-[:CONTAINS_TABLE]->(LogTable)-[:HAS_FIELD]->(Field)
(LogCatalog)-[:DEFINES_JOIN_KEY]->(JoinKey)
(Field)-[:MAPS_TO_JOIN_KEY]->(JoinKey)
```

The importer represents the JSON `field` array as `HAS_FIELD` edges. Graph
nodes additionally carry catalog/table ownership properties for indexing and
scoped imports. V2 full-text indexes use `name`, `description` and
`description_zh`.

Start the database using `docker/neo4j/README.md`, load its local credentials,
and run:

```bash
set -a
source docker/neo4j/.env
set +a
uv run python -m \
  src.memory.longterm_memory.semantic_memory.excytin_bench.import_neo4j
```

Rebuilding JSON does not modify an existing Neo4j database. The semantic import
replaces the nodes owned by `excytin-schema-v1`; if episodic or procedural
memories already link to those nodes, reimport them afterward to restore their
cross-memory edges.

```cypher
MATCH (:LogTable {name: 'DeviceProcessEvents'})-[:HAS_FIELD]->(field:Field)
RETURN field.name, field.data_type, field.description, field.description_zh
ORDER BY field.name;
```
