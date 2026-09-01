# ExCyTIn-Bench Episodic Memory

This directory contains a sanitized episodic-memory snapshot built only from
the ExCyTIn-Bench training trajectories in `corrects.jsonl`. The committed
snapshot excludes raw observations, agent thoughts, submitted answers,
ground-truth answers, solutions, evaluator text, and test questions.

## Graph model

```text
(Environment)-[:HAS_EPISODIC_MEMORY]->(EpisodeCatalog)
    -[:USES_SCHEMA]->(LogCatalog)
    -[:CONTAINS_EPISODE]->(InvestigationEpisode)
        -[:HAS_ATTEMPT]->(QueryAttempt)-[:NEXT]->(QueryAttempt)
                               |              ^
                               `-[:REPAIRED_BY]'

(QueryAttempt)-[:TARGETS_TABLE]->(LogTable)
(QueryAttempt)-[:USES_FIELD]->(Field)
(QueryAttempt)-[:USES_JOIN_KEY]->(JoinKey)
(QueryAttempt)-[:FAILED_WITH]->(QueryErrorType)
```

`execution_status` records whether MySQL executed a query. `result_status`
separately records whether it was schema discovery, returned rows, returned an
empty result, or raised an error. A returned row is not automatically labeled
as useful evidence because `corrects.jsonl` does not provide step-level
evidence judgments.

The source contains 237 successful or partially successful episodes and 1,605
SQL attempts. It does not contain fully failed final episodes; failures in this
snapshot are failed or empty attempts inside those retained trajectories. One
episode submits directly without executing SQL and therefore correctly has no
`QueryAttempt` nodes.

## Files

- `episodic_memory.json`: generated, sanitized knowledge snapshot.
- `episode.schema.json`: JSON Schema for the snapshot format.
- `build_knowledge.py`: deterministic train mapping and graph builder.
- `sanitize.py`: text redaction and SQL literal parameterization.
- `sql_parser.py`: MySQL AST parsing and Semantic Memory linking.
- `import_neo4j.py`: idempotent Neo4j importer.

## Rebuild the JSON snapshot

From the repository root:

```bash
uv run python -m \
  src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge
```

The build fails if a trajectory is not uniquely attributable to a train
question, if the expected corpus totals change, or if SQL parsing fails.

## Import into Neo4j

Import Semantic Memory first. Then start Neo4j, load the ignored local
credentials, and run:

```bash
set -a
source docker/neo4j/.env
set +a
uv run python -m \
  src.memory.longterm_memory.episodic_memory.excytin_bench.import_neo4j
```

The importer verifies the `excytin-schema-v1` schema hash before writing. A
re-import replaces only nodes owned by `excytin-episodes-v1` and preserves the
Semantic Memory graph.

## Example queries

Find successful training episodes that used a log table:

```cypher
MATCH (episode:InvestigationEpisode {source_split: 'train', outcome: 'success'})
      -[:HAS_ATTEMPT]->(attempt:QueryAttempt)
      -[:TARGETS_TABLE]->(:LogTable {name: 'DeviceProcessEvents'})
RETURN DISTINCT episode.episode_id, episode.task_template, episode.reward
LIMIT 10;
```

Inspect an unknown-field failure and its immediate repair step:

```cypher
MATCH (failed:QueryAttempt)-[:FAILED_WITH]->(:QueryErrorType {name: 'unknown_column'})
MATCH (failed)-[:REPAIRED_BY]->(repair:QueryAttempt)
RETURN failed.sql_template, failed.error_detail, repair.sql_template,
       repair.result_status
LIMIT 10;
```

Find query templates that use a Semantic Memory field:

```cypher
MATCH (attempt:QueryAttempt)-[usage:USES_FIELD]->
      (:Field {field_id: 'excytin-schema-v1:AlertEvidence:AccountSid'})
RETURN attempt.sql_template, usage.roles, attempt.result_status
LIMIT 10;
```

Use the full-text index for task retrieval:

```cypher
CALL db.index.fulltext.queryNodes('episodic_episode_text', 'suspicious process')
YIELD node, score
WHERE node.source_split = 'train' AND node.retrieval_eligible = true
RETURN node.task_template, node.outcome, score
ORDER BY score DESC
LIMIT 5;
```
