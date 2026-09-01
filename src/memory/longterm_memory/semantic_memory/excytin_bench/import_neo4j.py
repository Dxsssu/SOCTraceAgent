from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

from . import DEFAULT_KNOWLEDGE_PATH


CONSTRAINTS_AND_INDEXES = (
    "CREATE CONSTRAINT semantic_environment_id IF NOT EXISTS "
    "FOR (node:Environment) REQUIRE node.environment_id IS UNIQUE",
    "CREATE CONSTRAINT semantic_catalog_id IF NOT EXISTS "
    "FOR (node:LogCatalog) REQUIRE node.catalog_id IS UNIQUE",
    "CREATE CONSTRAINT semantic_table_id IF NOT EXISTS "
    "FOR (node:LogTable) REQUIRE node.table_id IS UNIQUE",
    "CREATE CONSTRAINT semantic_field_id IF NOT EXISTS "
    "FOR (node:Field) REQUIRE node.field_id IS UNIQUE",
    "CREATE CONSTRAINT semantic_join_key_id IF NOT EXISTS "
    "FOR (node:JoinKey) REQUIRE node.join_key_id IS UNIQUE",
    "CREATE INDEX semantic_table_name IF NOT EXISTS "
    "FOR (node:LogTable) ON (node.catalog_id, node.name)",
    "CREATE INDEX semantic_field_name IF NOT EXISTS "
    "FOR (node:Field) ON (node.catalog_id, node.name)",
    "CREATE INDEX semantic_field_type IF NOT EXISTS "
    "FOR (node:Field) ON (node.catalog_id, node.semantic_type)",
    "CREATE FULLTEXT INDEX semantic_table_text IF NOT EXISTS "
    "FOR (node:LogTable) ON EACH [node.name, node.description_zh, node.description_en, node.semantic_text]",
    "CREATE FULLTEXT INDEX semantic_field_text IF NOT EXISTS "
    "FOR (node:Field) ON EACH [node.name, node.description_zh, node.description_en, node.semantic_text]",
)


def chunked(rows: list[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


def load_knowledge(path: Path) -> dict[str, Any]:
    knowledge = json.loads(path.read_text(encoding="utf-8"))
    required = {"format_version", "environment", "catalog", "tables", "join_keys"}
    missing = required.difference(knowledge)
    if missing:
        raise ValueError(f"Knowledge JSON is missing keys: {', '.join(sorted(missing))}")

    catalog = knowledge["catalog"]
    if catalog["table_count"] != len(knowledge["tables"]):
        raise ValueError("catalog.table_count does not match tables")
    if catalog["field_count"] != sum(len(table["fields"]) for table in knowledge["tables"]):
        raise ValueError("catalog.field_count does not match nested fields")
    if catalog["join_key_count"] != len(knowledge["join_keys"]):
        raise ValueError("catalog.join_key_count does not match join_keys")
    return knowledge


def import_knowledge(
    *,
    uri: str,
    username: str,
    password: str,
    database: str,
    knowledge: dict[str, Any],
) -> dict[str, int]:
    catalog_id = knowledge["catalog"]["catalog_id"]
    driver = GraphDatabase.driver(uri, auth=(username, password))
    driver.verify_connectivity()

    with driver.session(database=database) as session:
        for statement in CONSTRAINTS_AND_INDEXES:
            session.run(statement).consume()

        # A generated catalog is an immutable snapshot. Re-importing the same
        # catalog replaces only its owned nodes and leaves other environments
        # and catalogs untouched.
        session.run(
            "MATCH (node) WHERE node.catalog_id = $catalog_id DETACH DELETE node",
            catalog_id=catalog_id,
        ).consume()

        environment = dict(knowledge["environment"])
        catalog = dict(knowledge["catalog"])
        session.run(
            """
            MERGE (environment:Environment {environment_id: $environment.environment_id})
            SET environment += $environment
            CREATE (catalog:LogCatalog {catalog_id: $catalog.catalog_id})
            SET catalog += $catalog
            MERGE (environment)-[:HAS_CATALOG]->(catalog)
            """,
            environment=environment,
            catalog=catalog,
        ).consume()

        table_rows: list[dict[str, Any]] = []
        field_rows: list[dict[str, Any]] = []
        for table in knowledge["tables"]:
            table_properties = {key: value for key, value in table.items() if key != "fields"}
            table_properties["catalog_id"] = catalog_id
            table_rows.append(table_properties)
            for field in table["fields"]:
                field_properties = dict(field)
                field_properties["catalog_id"] = catalog_id
                field_properties["table_id"] = table["table_id"]
                # Neo4j properties cannot store null values; retaining these
                # keys in JSON is useful, while omitting them on graph nodes is
                # the idiomatic representation.
                field_properties = {
                    key: value for key, value in field_properties.items() if value is not None
                }
                field_rows.append(field_properties)

        for rows in chunked(table_rows):
            session.run(
                """
                UNWIND $rows AS row
                MATCH (catalog:LogCatalog {catalog_id: $catalog_id})
                CREATE (table:LogTable {table_id: row.table_id})
                SET table += row
                CREATE (catalog)-[:CONTAINS_TABLE]->(table)
                """,
                rows=rows,
                catalog_id=catalog_id,
            ).consume()

        for rows in chunked(field_rows):
            session.run(
                """
                UNWIND $rows AS row
                MATCH (table:LogTable {table_id: row.table_id})
                CREATE (field:Field {field_id: row.field_id})
                SET field += row
                CREATE (table)-[:HAS_FIELD]->(field)
                """,
                rows=rows,
            ).consume()

        join_key_rows: list[dict[str, Any]] = []
        mapping_rows: list[dict[str, Any]] = []
        for join_key in knowledge["join_keys"]:
            properties = {key: value for key, value in join_key.items() if key != "field_ids"}
            properties["catalog_id"] = catalog_id
            join_key_rows.append(properties)
            mapping_rows.extend(
                {
                    "join_key_id": join_key["join_key_id"],
                    "field_id": field_id,
                    "confidence": join_key["confidence"],
                    "match_method": join_key["match_method"],
                    "join_scope": join_key["join_scope"],
                }
                for field_id in join_key["field_ids"]
            )

        for rows in chunked(join_key_rows):
            session.run(
                """
                UNWIND $rows AS row
                MATCH (catalog:LogCatalog {catalog_id: $catalog_id})
                CREATE (join_key:JoinKey {join_key_id: row.join_key_id})
                SET join_key += row
                CREATE (catalog)-[:DEFINES_JOIN_KEY]->(join_key)
                """,
                rows=rows,
                catalog_id=catalog_id,
            ).consume()

        for rows in chunked(mapping_rows):
            session.run(
                """
                UNWIND $rows AS row
                MATCH (field:Field {field_id: row.field_id})
                MATCH (join_key:JoinKey {join_key_id: row.join_key_id})
                CREATE (field)-[mapping:MAPS_TO_JOIN_KEY]->(join_key)
                SET mapping.confidence = row.confidence,
                    mapping.match_method = row.match_method,
                    mapping.join_scope = row.join_scope
                """,
                rows=rows,
            ).consume()

        result = session.run(
            """
            MATCH (catalog:LogCatalog {catalog_id: $catalog_id})
            OPTIONAL MATCH (catalog)-[:CONTAINS_TABLE]->(table:LogTable)
            WITH catalog, count(DISTINCT table) AS tables
            OPTIONAL MATCH (catalog)-[:CONTAINS_TABLE]->(:LogTable)-[:HAS_FIELD]->(field:Field)
            WITH catalog, tables, count(DISTINCT field) AS fields
            OPTIONAL MATCH (join_key:JoinKey {catalog_id: $catalog_id})
            WITH catalog, tables, fields, count(DISTINCT join_key) AS join_keys
            OPTIONAL MATCH (:Field {catalog_id: $catalog_id})-[mapping:MAPS_TO_JOIN_KEY]->(:JoinKey)
            RETURN tables, fields, join_keys, count(mapping) AS mappings
            """,
            catalog_id=catalog_id,
        ).single(strict=True)

    driver.close()
    return {key: int(result[key]) for key in ("tables", "fields", "join_keys", "mappings")}


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Import ExCyTIn-Bench semantic memory into Neo4j.")
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
        f"Imported {knowledge_path}: {counts['tables']} tables, {counts['fields']} fields, "
        f"{counts['join_keys']} join keys, and {counts['mappings']} field mappings."
    )


if __name__ == "__main__":
    main()
