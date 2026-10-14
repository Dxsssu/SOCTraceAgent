"""Validate and atomically replace failure-recovery EM; remove the v1 graph."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from neo4j import GraphDatabase

from . import DEFAULT_KNOWLEDGE_PATH
from .build_knowledge import CATALOG_ID, entry_id


def validate_knowledge(knowledge):
    schema = json.loads(Path(__file__).with_name("episode.schema.json").read_text())
    Draft202012Validator(schema).validate(knowledge)
    entries = knowledge["entries"]
    catalog = knowledge["catalog"]
    if catalog["episodic_catalog_id"] != CATALOG_ID:
        raise ValueError("Unexpected recovery catalog")
    if catalog["entry_count"] != len(entries) or len(
        {entry_id(e) for e in entries}
    ) != len(entries):
        raise ValueError("Invalid entry count or duplicate recovery")
    if (
        catalog["error_attempt_count"]
        != catalog["supported_error_count"] + catalog["excluded_error_count"]
    ):
        raise ValueError("Error audit totals differ")
    return knowledge


def load_knowledge(path):
    return validate_knowledge(json.loads(Path(path).read_text()))


def graph_rows(knowledge):
    return [
        {
            **e,
            "recovery_id": entry_id(e),
            "position": i,
            "episodic_catalog_id": CATALOG_ID,
        }
        for i, e in enumerate(knowledge["entries"])
    ]


def import_knowledge(*, uri, username, password, database, knowledge):
    validate_knowledge(knowledge)
    rows = graph_rows(knowledge)

    def replace(tx):
        record = tx.run(
            "MATCH (c:LogCatalog {catalog_id:$id}) RETURN c.schema_hash AS hash",
            id=knowledge["semantic_catalog"]["catalog_id"],
        ).single()
        if not record or record["hash"] != knowledge["semantic_catalog"]["schema_hash"]:
            raise ValueError("Import matching Semantic Memory first")
        record = tx.run(
            "MATCH (e:Environment {environment_id:$id}) RETURN count(e) AS count",
            id=knowledge["environment_id"],
        ).single()
        if not record or record["count"] != 1:
            raise ValueError("Missing or ambiguous environment")
        tx.run(
            "MATCH (n) WHERE n.episodic_catalog_id IN $ids DETACH DELETE n",
            ids=["excytin-episodes-v1", CATALOG_ID],
        ).consume()
        tx.run(
            "MATCH (e:Environment {environment_id:$env}), (s:LogCatalog {catalog_id:$sm}) "
            "CREATE (c:EpisodeCatalog) SET c = $catalog "
            "CREATE (e)-[:HAS_EPISODIC_MEMORY]->(c) CREATE (c)-[:USES_SCHEMA]->(s)",
            env=knowledge["environment_id"],
            sm=knowledge["semantic_catalog"]["catalog_id"],
            catalog=knowledge["catalog"],
        ).consume()
        tx.run(
            "MATCH (c:EpisodeCatalog {episodic_catalog_id:$id}) UNWIND $rows AS row "
            "CREATE (r:FailureRecovery) SET r = row CREATE (c)-[:CONTAINS_RECOVERY]->(r)",
            id=CATALOG_ID,
            rows=rows,
        ).consume()
        record = tx.run(
            "MATCH (:EpisodeCatalog {episodic_catalog_id:$id})-[:CONTAINS_RECOVERY]->(r) "
            "RETURN count(r) AS count",
            id=CATALOG_ID,
        ).single()
        if record["count"] != len(rows):
            raise ValueError("Recovery import count mismatch")
        return {"recoveries": len(rows)}

    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            session.run(
                "CREATE CONSTRAINT recovery_id IF NOT EXISTS FOR (r:FailureRecovery) REQUIRE r.recovery_id IS UNIQUE"
            ).consume()
            return session.execute_write(replace)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    p.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    p.add_argument("--username", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    p.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    p.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    knowledge = load_knowledge(args.knowledge)
    if args.dry_run:
        print(json.dumps(knowledge["catalog"]))
        return
    if not args.password:
        p.error("NEO4J_PASSWORD is required")
    print(
        import_knowledge(
            uri=args.uri,
            username=args.username,
            password=args.password,
            database=args.database,
            knowledge=knowledge,
        )
    )


if __name__ == "__main__":
    main()
