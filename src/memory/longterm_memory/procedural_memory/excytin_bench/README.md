# ExCyTIn-Bench Procedural Memory

This package stores investigation direction as skill-oriented procedures.
Procedural Memory chooses the overall workflow, Semantic Memory resolves valid
tables, fields, and join scopes, and Episodic Memory supplies concrete query
examples for the current phase.

The files under `skills/` resemble `SKILL.md` documents, but they are runtime
memory artifacts for SOCTraceAgent rather than installable Codex skills.

## Memory boundary

```text
Procedure: what investigation phase should happen next?
Semantic:  which current schema objects can implement that phase?
Episodic:  which prior query attempts illustrate the implementation?
```

Procedures contain no SQL, log values, answers, test questions, or incident
specific entities. Each active procedure is linked to training episodes as
support and each executable phase may link to a small set of sanitized
`QueryAttempt` exemplars.

## Graph model

```text
(Environment)-[:HAS_PROCEDURAL_MEMORY]->(ProcedureCatalog)
    -[:USES_SCHEMA]->(LogCatalog)
    -[:USES_EPISODIC_MEMORY]->(EpisodeCatalog)
    -[:CONTAINS_PROCEDURE]->(InvestigationProcedure)
         -[:HAS_PHASE]->(ProcedurePhase)-[:NEXT]->(ProcedurePhase)
                  |              |
                  |              `-[:EXEMPLIFIED_BY]->(QueryAttempt)
                  ├-[:REQUIRES_TABLE]->(LogTable)
                  └-[:REQUIRES_JOIN_KEY]->(JoinKey)

(InvestigationProcedure)-[:SUPPORTED_BY]->(InvestigationEpisode)
(InvestigationProcedure)-[:EXTENDS]->(InvestigationProcedure)
```

## Included skills

- `alert-centered-investigation`: base workflow for all alert-led investigations.
- `email-threat-investigation`: message, URL, delivery, and remediation workflow.
- `endpoint-process-investigation`: device, process, file, network, and timeline workflow.
- `identity-signin-investigation`: account, sign-in, risk, and cloud-activity workflow.
- `network-activity-investigation`: indicator, direction, device, process, and network-control workflow.

## Rebuild

From the repository root:

```bash
uv run python -m \
  src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge
```

The build validates every referenced log type and join key against the current
Semantic snapshot. Evidence counts and exemplar IDs are derived from the
sanitized Episodic snapshot; the 151 Expel insights are provenance inputs, not
copied into prompts verbatim.

## Import into Neo4j

Import Semantic and Episodic Memory first, then run:

```bash
set -a
source docker/neo4j/.env
set +a
uv run python -m \
  src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j
```

## Example retrieval

Select an active workflow from the task's entity types:

```cypher
MATCH (procedure:InvestigationProcedure {status: 'active'})
WHERE any(entity IN procedure.entry_entity_types
          WHERE entity IN ['URL', 'EMAIL_ADDRESS'])
RETURN procedure.name, procedure.goal, procedure.skill_path,
       procedure.support_episode_count
ORDER BY procedure.category DESC, procedure.support_episode_count DESC;
```

Retrieve its phases, current Semantic requirements, and Episodic exemplars:

```cypher
MATCH (procedure:InvestigationProcedure {skill_id: 'email-threat-investigation'})
      -[:HAS_PHASE]->(phase:ProcedurePhase)
OPTIONAL MATCH (phase)-[:REQUIRES_TABLE]->(table:LogTable)
OPTIONAL MATCH (phase)-[:EXEMPLIFIED_BY]->(attempt:QueryAttempt)
RETURN phase.sequence_no, phase.name, phase.goal,
       collect(DISTINCT table.name) AS candidate_tables,
       collect(DISTINCT attempt.sql_template) AS episodic_examples,
       phase.success_condition
ORDER BY phase.sequence_no;
```
