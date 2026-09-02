"""Idempotently import sanitized ExCyTIn-Bench episodes into Neo4j."""

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
        "CREATE CONSTRAINT episodic_catalog_id IF NOT EXISTS "
        "FOR (node:EpisodeCatalog) REQUIRE node.episodic_catalog_id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT episodic_episode_id IF NOT EXISTS "
        "FOR (node:InvestigationEpisode) REQUIRE node.episode_id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT episodic_attempt_id IF NOT EXISTS "
        "FOR (node:QueryAttempt) REQUIRE node.attempt_id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT episodic_error_type_id IF NOT EXISTS "
        "FOR (node:QueryErrorType) REQUIRE node.error_type_id IS UNIQUE"
    ),
    (
        "CREATE INDEX episodic_episode_filter IF NOT EXISTS "
        "FOR (node:InvestigationEpisode) ON "
        "(node.episodic_catalog_id, node.source_split, node.outcome)"
    ),
    (
        "CREATE INDEX episodic_episode_incident IF NOT EXISTS "
        "FOR (node:InvestigationEpisode) ON "
        "(node.episodic_catalog_id, node.source_incident)"
    ),
    (
        "CREATE INDEX episodic_attempt_status IF NOT EXISTS "
        "FOR (node:QueryAttempt) ON "
        "(node.episodic_catalog_id, node.execution_status, node.result_status)"
    ),
    (
        "CREATE INDEX episodic_attempt_kind IF NOT EXISTS "
        "FOR (node:QueryAttempt) ON (node.episodic_catalog_id, node.query_kind)"
    ),
    (
        "CREATE FULLTEXT INDEX episodic_episode_text IF NOT EXISTS "
        "FOR (node:InvestigationEpisode) ON EACH [node.task_template, node.retrieval_text]"
    ),
    (
        "CREATE FULLTEXT INDEX episodic_attempt_text IF NOT EXISTS "
        "FOR (node:QueryAttempt) ON EACH "
        "[node.sql_template, node.error_detail, node.retrieval_text]"
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
        "catalog",
        "error_types",
        "episodes",
    }
    missing = required.difference(knowledge)
    if missing:
        raise ValueError(f"Knowledge JSON is missing keys: {', '.join(sorted(missing))}")
    policy = knowledge["generation_policy"]
    if policy.get("source_split") != "train" or not policy.get("sanitized"):
        raise ValueError("Only sanitized train episodic knowledge can be imported")
    if any(
        policy.get(key)
        for key in ("contains_raw_observations", "contains_agent_thoughts", "contains_answers", "contains_test_data")
    ):
        raise ValueError("Episodic knowledge contains prohibited raw or evaluation data")

    catalog = knowledge["catalog"]
    episodes = knowledge["episodes"]
    attempts = [attempt for episode in episodes for attempt in episode["attempts"]]
    if catalog["episode_count"] != len(episodes):
        raise ValueError("catalog.episode_count does not match episodes")
    if catalog["attempt_count"] != len(attempts):
        raise ValueError("catalog.attempt_count does not match nested attempts")
    if catalog["error_attempt_count"] != sum("error_type_id" in attempt for attempt in attempts):
        raise ValueError("catalog.error_attempt_count does not match attempts")

    episode_ids = [episode["episode_id"] for episode in episodes]
    attempt_ids = [attempt["attempt_id"] for attempt in attempts]
    error_ids = {error["error_type_id"] for error in knowledge["error_types"]}
    if len(set(episode_ids)) != len(episode_ids):
        raise ValueError("Duplicate episode_id values")
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("Duplicate attempt_id values")
    all_attempt_ids = set(attempt_ids)
    for episode in episodes:
        sequences = [attempt["sequence_no"] for attempt in episode["attempts"]]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError(f"Non-contiguous attempt sequence in {episode['episode_id']}")
        for transition in episode["transitions"]:
            if not {transition["from_attempt_id"], transition["to_attempt_id"]} <= all_attempt_ids:
                raise ValueError(f"Transition references an unknown attempt in {episode['episode_id']}")
        for attempt in episode["attempts"]:
            if "error_type_id" in attempt and attempt["error_type_id"] not in error_ids:
                raise ValueError(f"Unknown error type in {attempt['attempt_id']}")
    return knowledge


def catalog_properties(catalog: dict[str, Any]) -> dict[str, Any]:
    grouped_properties = {
        "outcome_counts",
        "result_counts",
        "source_match_counts",
        "trajectory_alignment",
    }
    result = {
        key: value
        for key, value in catalog.items()
        if key not in grouped_properties
    }
    for group_name in sorted(grouped_properties):
        for name, count in catalog[group_name].items():
            result[f"{group_name}_{name}"] = count
    unsupported = {
        key: type(value).__name__
        for key, value in result.items()
        if isinstance(value, dict | list) and not (
            isinstance(value, list)
            and all(not isinstance(item, dict | list) for item in value)
        )
    }
    if unsupported:
        raise TypeError(f"Catalog contains unsupported Neo4j properties: {unsupported}")
    return result


def graph_rows(knowledge: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    episodic_catalog_id = knowledge["catalog"]["episodic_catalog_id"]
    episodes: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    episode_attempts: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    table_refs: list[dict[str, Any]] = []
    field_refs: list[dict[str, Any]] = []
    join_key_refs: list[dict[str, Any]] = []
    error_refs: list[dict[str, Any]] = []

    for episode in knowledge["episodes"]:
        episodes.append(
            {
                **{key: value for key, value in episode.items() if key not in {"attempts", "transitions"}},
                "episodic_catalog_id": episodic_catalog_id,
            }
        )
        for attempt in episode["attempts"]:
            attempts.append(
                {
                    **{
                        key: value
                        for key, value in attempt.items()
                        if key not in {"table_refs", "field_refs", "join_key_ids"}
                    },
                    "episodic_catalog_id": episodic_catalog_id,
                    "episode_id": episode["episode_id"],
                }
            )
            episode_attempts.append(
                {
                    "episode_id": episode["episode_id"],
                    "attempt_id": attempt["attempt_id"],
                    "sequence_no": attempt["sequence_no"],
                }
            )
            table_refs.extend(
                {"attempt_id": attempt["attempt_id"], **reference}
                for reference in attempt["table_refs"]
            )
            field_refs.extend(
                {"attempt_id": attempt["attempt_id"], **reference}
                for reference in attempt["field_refs"]
            )
            join_key_refs.extend(
                {"attempt_id": attempt["attempt_id"], "join_key_id": join_key_id}
                for join_key_id in attempt["join_key_ids"]
            )
            if "error_type_id" in attempt:
                error_refs.append(
                    {
                        "attempt_id": attempt["attempt_id"],
                        "error_type_id": attempt["error_type_id"],
                    }
                )
        transitions.extend(episode["transitions"])

    return {
        "episodes": episodes,
        "attempts": attempts,
        "episode_attempts": episode_attempts,
        "transitions": transitions,
        "table_refs": table_refs,
        "field_refs": field_refs,
        "join_key_refs": join_key_refs,
        "error_refs": error_refs,
    }


def import_knowledge(
    *,
    uri: str,
    username: str,
    password: str,
    database: str,
    knowledge: dict[str, Any],
) -> dict[str, int]:
    episodic_catalog_id = knowledge["catalog"]["episodic_catalog_id"]
    semantic_catalog_id = knowledge["semantic_catalog"]["catalog_id"]
    expected_schema_hash = knowledge["semantic_catalog"]["schema_hash"]
    rows = graph_rows(knowledge)
    # Validate/flatten every catalog property before the existing graph is
    # replaced. This prevents a malformed snapshot from deleting a healthy
    # catalog and then failing during CREATE.
    catalog_row = catalog_properties(knowledge["catalog"])
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
            if semantic is None:
                raise ValueError(
                    f"Semantic catalog {semantic_catalog_id!r} must be imported before episodic memory"
                )
            if semantic["schema_hash"] != expected_schema_hash:
                raise ValueError(
                    f"Semantic catalog hash mismatch: expected {expected_schema_hash}, "
                    f"found {semantic['schema_hash']}"
                )
            environment = session.run(
                "MATCH (environment:Environment {environment_id: $environment_id}) "
                "RETURN count(environment) AS count",
                environment_id=knowledge["environment_id"],
            ).single(strict=True)
            if int(environment["count"]) != 1:
                raise ValueError(f"Environment {knowledge['environment_id']!r} is missing or ambiguous")

            session.run(
                "MATCH (node) WHERE node.episodic_catalog_id = $episodic_catalog_id "
                "DETACH DELETE node",
                episodic_catalog_id=episodic_catalog_id,
            ).consume()
            session.run(
                """
                MATCH (environment:Environment {environment_id: $environment_id})
                MATCH (semantic:LogCatalog {catalog_id: $semantic_catalog_id})
                CREATE (catalog:EpisodeCatalog {episodic_catalog_id: $catalog.episodic_catalog_id})
                SET catalog += $catalog
                CREATE (environment)-[:HAS_EPISODIC_MEMORY]->(catalog)
                CREATE (catalog)-[:USES_SCHEMA]->(semantic)
                """,
                environment_id=knowledge["environment_id"],
                semantic_catalog_id=semantic_catalog_id,
                catalog=catalog_row,
            ).consume()

            error_rows = [
                {**error, "episodic_catalog_id": episodic_catalog_id}
                for error in knowledge["error_types"]
            ]
            session.run(
                """
                UNWIND $rows AS row
                MATCH (catalog:EpisodeCatalog {episodic_catalog_id: $episodic_catalog_id})
                CREATE (error:QueryErrorType {error_type_id: row.error_type_id})
                SET error += row
                CREATE (catalog)-[:DEFINES_ERROR_TYPE]->(error)
                """,
                rows=error_rows,
                episodic_catalog_id=episodic_catalog_id,
            ).consume()

            for batch in chunked(rows["episodes"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (catalog:EpisodeCatalog {episodic_catalog_id: $episodic_catalog_id})
                    CREATE (episode:InvestigationEpisode {episode_id: row.episode_id})
                    SET episode += row
                    CREATE (catalog)-[:CONTAINS_EPISODE]->(episode)
                    """,
                    rows=batch,
                    episodic_catalog_id=episodic_catalog_id,
                ).consume()
            for batch in chunked(rows["attempts"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    CREATE (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    SET attempt += row
                    """,
                    rows=batch,
                ).consume()
            for batch in chunked(rows["episode_attempts"]):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (episode:InvestigationEpisode {episode_id: row.episode_id})
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    CREATE (episode)-[:HAS_ATTEMPT {sequence_no: row.sequence_no}]->(attempt)
                    """,
                    rows=batch,
                ).consume()

            next_rows = [row for row in rows["transitions"] if row["relation_type"] == "NEXT"]
            repair_rows = [
                row for row in rows["transitions"] if row["relation_type"] == "REPAIRED_BY"
            ]
            for relationship, relationship_rows in (("NEXT", next_rows), ("REPAIRED_BY", repair_rows)):
                for batch in chunked(relationship_rows):
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MATCH (source:QueryAttempt {{attempt_id: row.from_attempt_id}})
                        MATCH (target:QueryAttempt {{attempt_id: row.to_attempt_id}})
                        CREATE (source)-[:{relationship}]->(target)
                        """,
                        rows=batch,
                    ).consume()

            relationship_queries = (
                (
                    "table_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    MATCH (table:LogTable {table_id: row.table_id})
                    CREATE (attempt)-[:TARGETS_TABLE {roles: row.roles}]->(table)
                    """,
                ),
                (
                    "field_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    MATCH (field:Field {field_id: row.field_id})
                    CREATE (attempt)-[:USES_FIELD {roles: row.roles}]->(field)
                    """,
                ),
                (
                    "join_key_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    MATCH (join_key:JoinKey {join_key_id: row.join_key_id})
                    CREATE (attempt)-[:USES_JOIN_KEY]->(join_key)
                    """,
                ),
                (
                    "error_refs",
                    """
                    UNWIND $rows AS row
                    MATCH (attempt:QueryAttempt {attempt_id: row.attempt_id})
                    MATCH (error:QueryErrorType {error_type_id: row.error_type_id})
                    CREATE (attempt)-[:FAILED_WITH]->(error)
                    """,
                ),
            )
            for row_name, statement in relationship_queries:
                for batch in chunked(rows[row_name]):
                    session.run(statement, rows=batch).consume()

            result = session.run(
                """
                MATCH (catalog:EpisodeCatalog {episodic_catalog_id: $catalog_id})
                OPTIONAL MATCH (catalog)-[:CONTAINS_EPISODE]->(episode:InvestigationEpisode)
                WITH catalog, count(DISTINCT episode) AS episodes
                OPTIONAL MATCH (:InvestigationEpisode {episodic_catalog_id: $catalog_id})
                               -[:HAS_ATTEMPT]->(attempt:QueryAttempt)
                WITH catalog, episodes, count(DISTINCT attempt) AS attempts
                OPTIONAL MATCH (:QueryAttempt {episodic_catalog_id: $catalog_id})-[table_rel:TARGETS_TABLE]->()
                WITH catalog, episodes, attempts, count(table_rel) AS table_links
                OPTIONAL MATCH (:QueryAttempt {episodic_catalog_id: $catalog_id})-[field_rel:USES_FIELD]->()
                WITH catalog, episodes, attempts, table_links, count(field_rel) AS field_links
                OPTIONAL MATCH (:QueryAttempt {episodic_catalog_id: $catalog_id})-[error_rel:FAILED_WITH]->()
                RETURN episodes, attempts, table_links, field_links, count(error_rel) AS error_links
                """,
                catalog_id=episodic_catalog_id,
            ).single(strict=True)
    finally:
        driver.close()

    counts = {
        key: int(result[key])
        for key in ("episodes", "attempts", "table_links", "field_links", "error_links")
    }
    expected = {
        "episodes": len(rows["episodes"]),
        "attempts": len(rows["attempts"]),
        "table_links": len(rows["table_refs"]),
        "field_links": len(rows["field_refs"]),
        "error_links": len(rows["error_refs"]),
    }
    if counts != expected:
        raise RuntimeError(f"Neo4j import verification failed: expected {expected}, found {counts}")
    return counts


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Import ExCyTIn-Bench episodic memory into Neo4j.")
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
        f"Imported {knowledge_path}: {counts['episodes']} episodes, {counts['attempts']} attempts, "
        f"{counts['table_links']} table links, {counts['field_links']} field links, and "
        f"{counts['error_links']} error links."
    )


if __name__ == "__main__":
    main()
