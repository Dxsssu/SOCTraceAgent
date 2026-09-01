"""Idempotently import ExCyTIn-Bench procedural skills into Neo4j."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

from . import DEFAULT_KNOWLEDGE_PATH

CONSTRAINTS_AND_INDEXES = (
    (
        "CREATE CONSTRAINT procedural_catalog_id IF NOT EXISTS "
        "FOR (node:ProcedureCatalog) REQUIRE node.procedural_catalog_id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT procedural_procedure_id IF NOT EXISTS "
        "FOR (node:InvestigationProcedure) REQUIRE node.procedure_id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT procedural_phase_id IF NOT EXISTS "
        "FOR (node:ProcedurePhase) REQUIRE node.phase_id IS UNIQUE"
    ),
    (
        "CREATE INDEX procedural_procedure_filter IF NOT EXISTS "
        "FOR (node:InvestigationProcedure) ON "
        "(node.procedural_catalog_id, node.status, node.category)"
    ),
    (
        "CREATE INDEX procedural_procedure_skill IF NOT EXISTS "
        "FOR (node:InvestigationProcedure) ON (node.procedural_catalog_id, node.skill_id)"
    ),
    (
        "CREATE INDEX procedural_phase_sequence IF NOT EXISTS "
        "FOR (node:ProcedurePhase) ON (node.procedure_id, node.sequence_no)"
    ),
    (
        "CREATE FULLTEXT INDEX procedural_procedure_text IF NOT EXISTS "
        "FOR (node:InvestigationProcedure) ON EACH "
        "[node.name, node.goal, node.description, node.retrieval_text]"
    ),
    (
        "CREATE FULLTEXT INDEX procedural_phase_text IF NOT EXISTS "
        "FOR (node:ProcedurePhase) ON EACH "
        "[node.name, node.goal, node.success_condition, node.retrieval_text]"
    ),
)


def chunked(rows: list[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


def load_knowledge(path: Path) -> dict[str, Any]:
    knowledge = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "format_version",
        "generation_policy",
        "environment_id",
        "semantic_catalog",
        "episodic_catalog",
        "catalog",
        "procedures",
    }
    missing = required.difference(knowledge)
    if missing:
        raise ValueError(f"Knowledge JSON is missing keys: {', '.join(sorted(missing))}")
    policy = knowledge["generation_policy"]
    if policy.get("source_split") != "train" or not policy.get("skill_oriented"):
        raise ValueError("Only skill-oriented train procedural knowledge can be imported")
    if any(
        policy.get(key)
        for key in ("contains_sql", "contains_log_values", "contains_answers", "contains_test_data")
    ):
        raise ValueError("Procedural knowledge contains prohibited query or evaluation data")

    procedures = knowledge["procedures"]
    phases = [phase for procedure in procedures for phase in procedure["phases"]]
    catalog = knowledge["catalog"]
    if catalog["procedure_count"] != len(procedures):
        raise ValueError("catalog.procedure_count does not match procedures")
    if catalog["phase_count"] != len(phases):
        raise ValueError("catalog.phase_count does not match nested phases")
    if catalog["support_edge_count"] != sum(
        len(procedure["support_episode_ids"]) for procedure in procedures
    ):
        raise ValueError("catalog.support_edge_count does not match procedures")
    if catalog["exemplar_edge_count"] != sum(
        len(phase["exemplar_attempt_ids"]) for phase in phases
    ):
        raise ValueError("catalog.exemplar_edge_count does not match phases")

    procedure_ids = {procedure["procedure_id"] for procedure in procedures}
    phase_ids = {phase["phase_id"] for phase in phases}
    if len(procedure_ids) != len(procedures) or len(phase_ids) != len(phases):
        raise ValueError("Duplicate procedure_id or phase_id values")
    for procedure in procedures:
        parent_id = procedure.get("extends_procedure_id")
        if parent_id is not None and parent_id not in procedure_ids:
            raise ValueError(f"Unknown parent procedure in {procedure['procedure_id']}")
        sequences = [phase["sequence_no"] for phase in procedure["phases"]]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError(f"Non-contiguous phase sequence in {procedure['procedure_id']}")
        for transition in procedure["transitions"]:
            if not {transition["from_phase_id"], transition["to_phase_id"]} <= phase_ids:
                raise ValueError(f"Unknown phase in transition for {procedure['procedure_id']}")
    return knowledge


def graph_rows(knowledge: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    catalog_id = knowledge["catalog"]["procedural_catalog_id"]
    procedures: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    procedure_phases: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    extensions: list[dict[str, Any]] = []
    table_refs: list[dict[str, Any]] = []
    join_key_refs: list[dict[str, Any]] = []
    supports: list[dict[str, Any]] = []
    exemplars: list[dict[str, Any]] = []

    for procedure in knowledge["procedures"]:
        procedures.append(
            {
                **{
                    key: value
                    for key, value in procedure.items()
                    if key
                    not in {
                        "phases",
                        "transitions",
                        "semantic_table_ids",
                        "semantic_join_key_ids",
                        "support_episode_ids",
                    }
                    and value is not None
                },
                "procedural_catalog_id": catalog_id,
            }
        )
        if procedure.get("extends_procedure_id"):
            extensions.append(
                {
                    "procedure_id": procedure["procedure_id"],
                    "parent_procedure_id": procedure["extends_procedure_id"],
                }
            )
        supports.extend(
            {"procedure_id": procedure["procedure_id"], "episode_id": episode_id}
            for episode_id in procedure["support_episode_ids"]
        )
        transitions.extend(procedure["transitions"])
        for phase in procedure["phases"]:
            phases.append(
                {
                    **{
                        key: value
                        for key, value in phase.items()
                        if key
                        not in {
                            "semantic_table_ids",
                            "semantic_join_key_ids",
                            "exemplar_attempt_ids",
                        }
                    },
                    "procedure_id": procedure["procedure_id"],
                    "procedural_catalog_id": catalog_id,
                }
            )
            procedure_phases.append(
                {
                    "procedure_id": procedure["procedure_id"],
                    "phase_id": phase["phase_id"],
                    "sequence_no": phase["sequence_no"],
                }
            )
            table_refs.extend(
                {"phase_id": phase["phase_id"], "table_id": table_id}
                for table_id in phase["semantic_table_ids"]
            )
            join_key_refs.extend(
                {"phase_id": phase["phase_id"], "join_key_id": join_key_id}
                for join_key_id in phase["semantic_join_key_ids"]
            )
            exemplars.extend(
                {"phase_id": phase["phase_id"], "attempt_id": attempt_id}
                for attempt_id in phase["exemplar_attempt_ids"]
            )
    return {
        "procedures": procedures,
        "phases": phases,
        "procedure_phases": procedure_phases,
        "transitions": transitions,
        "extensions": extensions,
        "table_refs": table_refs,
        "join_key_refs": join_key_refs,
        "supports": supports,
        "exemplars": exemplars,
    }


def catalog_properties(catalog: dict[str, Any]) -> dict[str, Any]:
    return dict(catalog)


def import_knowledge(
    *,
    uri: str,
    username: str,
    password: str,
    database: str,
    knowledge: dict[str, Any],
) -> dict[str, int]:
    catalog_id = knowledge["catalog"]["procedural_catalog_id"]
    semantic_catalog_id = knowledge["semantic_catalog"]["catalog_id"]
    episodic_catalog_id = knowledge["episodic_catalog"]["episodic_catalog_id"]
    rows = graph_rows(knowledge)
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            for statement in CONSTRAINTS_AND_INDEXES:
                session.run(statement).consume()

            semantic = session.run(
                "MATCH (catalog:LogCatalog {catalog_id: $catalog_id}) "
                "RETURN catalog.schema_hash AS schema_hash",
                catalog_id=semantic_catalog_id,
            ).single()
            if semantic is None or semantic["schema_hash"] != knowledge["semantic_catalog"]["schema_hash"]:
                raise ValueError("The required Semantic catalog is missing or has a different schema hash")
            episodic = session.run(
                "MATCH (catalog:EpisodeCatalog {episodic_catalog_id: $catalog_id}) "
                "RETURN catalog.source_sha256 AS source_sha256",
                catalog_id=episodic_catalog_id,
            ).single()
            if episodic is None or episodic["source_sha256"] != knowledge["episodic_catalog"]["source_sha256"]:
                raise ValueError("The required Episodic catalog is missing or has a different source hash")
            environment_count = session.run(
                "MATCH (environment:Environment {environment_id: $environment_id}) "
                "RETURN count(environment) AS count",
                environment_id=knowledge["environment_id"],
            ).single(strict=True)["count"]
            if int(environment_count) != 1:
                raise ValueError(f"Environment {knowledge['environment_id']!r} is missing or ambiguous")

            session.run(
                "MATCH (node) WHERE node.procedural_catalog_id = $catalog_id DETACH DELETE node",
                catalog_id=catalog_id,
            ).consume()
            session.run(
                """
                MATCH (environment:Environment {environment_id: $environment_id})
                MATCH (semantic:LogCatalog {catalog_id: $semantic_catalog_id})
                MATCH (episodic:EpisodeCatalog {episodic_catalog_id: $episodic_catalog_id})
                CREATE (catalog:ProcedureCatalog {procedural_catalog_id: $catalog.procedural_catalog_id})
                SET catalog += $catalog
                CREATE (environment)-[:HAS_PROCEDURAL_MEMORY]->(catalog)
                CREATE (catalog)-[:USES_SCHEMA]->(semantic)
                CREATE (catalog)-[:USES_EPISODIC_MEMORY]->(episodic)
                """,
                environment_id=knowledge["environment_id"],
                semantic_catalog_id=semantic_catalog_id,
                episodic_catalog_id=episodic_catalog_id,
                catalog=catalog_properties(knowledge["catalog"]),
            ).consume()

            for batch in chunked(rows["procedures"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (catalog:ProcedureCatalog {procedural_catalog_id: $catalog_id})
                    CREATE (procedure:InvestigationProcedure {procedure_id: row.procedure_id})
                    SET procedure += row
                    CREATE (catalog)-[:CONTAINS_PROCEDURE]->(procedure)
                    """,
                    rows=batch,
                    catalog_id=catalog_id,
                ).consume()
            for batch in chunked(rows["phases"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    CREATE (phase:ProcedurePhase {phase_id: row.phase_id})
                    SET phase += row
                    """,
                    rows=batch,
                ).consume()
            for batch in chunked(rows["procedure_phases"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (procedure:InvestigationProcedure {procedure_id: row.procedure_id})
                    MATCH (phase:ProcedurePhase {phase_id: row.phase_id})
                    CREATE (procedure)-[:HAS_PHASE {sequence_no: row.sequence_no}]->(phase)
                    """,
                    rows=batch,
                ).consume()

            relationship_queries = (
                (
                    "transitions",
                    """
                    UNWIND $rows AS row
                    MATCH (source:ProcedurePhase {phase_id: row.from_phase_id})
                    MATCH (target:ProcedurePhase {phase_id: row.to_phase_id})
                    CREATE (source)-[:NEXT]->(target)
                    """,
                ),
                (
                    "extensions",
                    """
                    UNWIND $rows AS row
                    MATCH (procedure:InvestigationProcedure {procedure_id: row.procedure_id})
                    MATCH (parent:InvestigationProcedure {procedure_id: row.parent_procedure_id})
                    CREATE (procedure)-[:EXTENDS]->(parent)
                    """,
                ),
                (
                    "table_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (phase:ProcedurePhase {phase_id: row.phase_id})
                    MATCH (table:LogTable {table_id: row.table_id})
                    CREATE (phase)-[:REQUIRES_TABLE]->(table)
                    """,
                ),
                (
                    "join_key_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (phase:ProcedurePhase {phase_id: row.phase_id})
                    MATCH (join_key:JoinKey {join_key_id: row.join_key_id})
                    CREATE (phase)-[:REQUIRES_JOIN_KEY]->(join_key)
                    """,
                ),
                (
                    "supports",
                    """
                    UNWIND $rows AS row
                    MATCH (procedure:InvestigationProcedure {procedure_id: row.procedure_id})
                    MATCH (episode:InvestigationEpisode {episode_id: row.episode_id})
                    CREATE (procedure)-[:SUPPORTED_BY]->(episode)
                    """,
                ),
                (
                    "exemplars",
                    """
                    UNWIND $rows AS row
                    MATCH (phase:ProcedurePhase {phase_id: row.phase_id})
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    CREATE (phase)-[:EXEMPLIFIED_BY]->(attempt)
                    """,
                ),
            )
            for row_name, statement in relationship_queries:
                for batch in chunked(rows[row_name]):
                    session.run(statement, rows=batch).consume()

            result = session.run(
                """
                MATCH (:ProcedureCatalog {procedural_catalog_id: $catalog_id})
                      -[:CONTAINS_PROCEDURE]->(procedure:InvestigationProcedure)
                WITH count(DISTINCT procedure) AS procedures
                MATCH (phase:ProcedurePhase {procedural_catalog_id: $catalog_id})
                WITH procedures, count(DISTINCT phase) AS phases
                OPTIONAL MATCH (:InvestigationProcedure {procedural_catalog_id: $catalog_id})
                               -[support:SUPPORTED_BY]->()
                WITH procedures, phases, count(support) AS supports
                OPTIONAL MATCH (:ProcedurePhase {procedural_catalog_id: $catalog_id})
                               -[exemplar:EXEMPLIFIED_BY]->()
                WITH procedures, phases, supports, count(exemplar) AS exemplars
                OPTIONAL MATCH (:ProcedurePhase {procedural_catalog_id: $catalog_id})
                               -[table_ref:REQUIRES_TABLE]->()
                WITH procedures, phases, supports, exemplars, count(table_ref) AS table_refs
                OPTIONAL MATCH (:ProcedurePhase {procedural_catalog_id: $catalog_id})
                               -[join_ref:REQUIRES_JOIN_KEY]->()
                RETURN procedures, phases, supports, exemplars, table_refs,
                       count(join_ref) AS join_key_refs
                """,
                catalog_id=catalog_id,
            ).single(strict=True)
    finally:
        driver.close()

    counts = {
        key: int(result[key])
        for key in (
            "procedures",
            "phases",
            "supports",
            "exemplars",
            "table_refs",
            "join_key_refs",
        )
    }
    expected = {
        "procedures": len(rows["procedures"]),
        "phases": len(rows["phases"]),
        "supports": len(rows["supports"]),
        "exemplars": len(rows["exemplars"]),
        "table_refs": len(rows["table_refs"]),
        "join_key_refs": len(rows["join_key_refs"]),
    }
    if counts != expected:
        raise RuntimeError(f"Neo4j import verification failed: expected {expected}, found {counts}")
    return counts


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Import ExCyTIn-Bench procedural memory.")
    parser.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--username", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    args = parser.parse_args()
    if not args.password:
        parser.error("Neo4j password is required via --password or NEO4J_PASSWORD")
    return args


def main() -> None:
    args = parse_args()
    knowledge_path = args.knowledge.resolve()
    knowledge = load_knowledge(knowledge_path)
    counts = import_knowledge(
        uri=args.uri,
        username=args.username,
        password=args.password,
        database=args.database,
        knowledge=knowledge,
    )
    print(
        f"Imported {knowledge_path}: {counts['procedures']} procedures, {counts['phases']} phases, "
        f"{counts['supports']} supports, {counts['exemplars']} exemplars, "
        f"{counts['table_refs']} table requirements, and "
        f"{counts['join_key_refs']} join-key requirements."
    )


if __name__ == "__main__":
    main()
