"""Validate the query-template memory and replace only the procedural catalog."""

from __future__ import annotations

import argparse
import json
from src.memory.longterm_memory.semantic_memory.excytin_bench.store import load_knowledge as load_semantic_knowledge
import os
from pathlib import Path

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from neo4j import GraphDatabase

from . import DEFAULT_KNOWLEDGE_PATH
from .build_knowledge import (
    CATALOG_ID,
    DEFAULT_EPISODIC_KNOWLEDGE,
    DEFAULT_SEMANTIC_KNOWLEDGE,
)


def validate_knowledge(knowledge, semantic=None, episodic=None):
    schema = json.loads(Path(__file__).with_name("procedure.schema.json").read_text())
    Draft202012Validator(schema).validate(knowledge)
    semantic = semantic or load_semantic_knowledge(DEFAULT_SEMANTIC_KNOWLEDGE)
    episodic = episodic or json.loads(DEFAULT_EPISODIC_KNOWLEDGE.read_text())
    if (
        knowledge["semantic_catalog"]["schema_hash"]
        != semantic["catalog"]["schema_hash"]
        or knowledge["episodic_catalog"]["source_sha256"]
        != episodic["catalog"]["source_sha256"]
    ):
        raise ValueError(
            "Procedural source catalogs differ from current training snapshots"
        )
    procedures = knowledge["procedures"]
    if len({p["id"] for p in procedures}) != len(procedures):
        raise ValueError("Template IDs must be unique")
    if knowledge["catalog"]["procedure_count"] != len(procedures) or knowledge[
        "catalog"
    ]["active_count"] != sum(p["status"] == "active" for p in procedures):
        raise ValueError("Catalog counts mismatch")
    from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import (
        retrieval_view,
    )

    tables = {
        t["name"]: {f["name"] for f in t["fields"]}
        for t in retrieval_view(semantic)["tables"]
    }
    from .templates import validate_template
    for procedure in procedures:
        validate_template(procedure, tables)
    return knowledge


def load_knowledge(path):
    return validate_knowledge(json.loads(Path(path).read_text()))


def graph_rows(knowledge):
    return [{"id": p["id"], "name": p["name"], "status": p["status"], "purpose": p["purpose"],
             "template_json": json.dumps(p, ensure_ascii=False), "procedural_catalog_id": CATALOG_ID}
            for p in knowledge["procedures"]]


def import_knowledge(*, uri, username, password, database, knowledge):
    validate_knowledge(knowledge)
    rows = graph_rows(knowledge)

    def replace(tx):
        for query, params, expected in [
            (
                "MATCH (c:EpisodeCatalog {episodic_catalog_id:$id}) RETURN c.source_sha256 AS value",
                {"id": knowledge["episodic_catalog"]["episodic_catalog_id"]},
                knowledge["episodic_catalog"]["source_sha256"],
            ),
        ]:
            record = tx.run(query, **params).single()
            if record is None or record["value"] != expected:
                raise ValueError("Import matching Episodic dependency first")
        env = tx.run(
            "MATCH (e:Environment {environment_id:$id}) RETURN count(e) AS n",
            id=knowledge["environment_id"],
        ).single()
        if env["n"] != 1:
            raise ValueError("Missing environment")
        tx.run(
            "MATCH (n) WHERE n.procedural_catalog_id IN $ids DETACH DELETE n",
            ids=[CATALOG_ID],
        ).consume()
        tx.run(
            "MATCH (e:Environment {environment_id:$env}) CREATE (c:ProcedureCatalog {procedural_catalog_id:$id, snapshot_json:$snapshot}) CREATE (e)-[:HAS_PROCEDURAL_MEMORY]->(c)",
            env=knowledge["environment_id"],
            id=CATALOG_ID,
            snapshot=json.dumps(knowledge, ensure_ascii=False),
        ).consume()
        tx.run(
            "MATCH (c:ProcedureCatalog {procedural_catalog_id:$id}) UNWIND $rows AS r CREATE (p:QueryTemplate) SET p=r CREATE (c)-[:CONTAINS_PROCEDURE]->(p)",
            id=CATALOG_ID,
            rows=rows,
        ).consume()
        count = tx.run(
            "MATCH (:ProcedureCatalog {procedural_catalog_id:$id})-[:CONTAINS_PROCEDURE]->(p) RETURN count(p) AS n",
            id=CATALOG_ID,
        ).single()["n"]
        if count != len(rows):
            raise ValueError("Graph count mismatch; replacement rolled back")
        return {"procedures": count, "active": knowledge["catalog"]["active_count"]}

    with (
        GraphDatabase.driver(uri, auth=(username, password)) as driver,
        driver.session(database=database) as session,
    ):
        session.run(
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:ProcedureCatalog) REQUIRE c.procedural_catalog_id IS UNIQUE"
        ).consume()
        session.run(
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:QueryTemplate) REQUIRE (p.procedural_catalog_id,p.id) IS UNIQUE"
        ).consume()
        return session.execute_write(replace)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    knowledge = load_knowledge(args.knowledge)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "procedures": len(graph_rows(knowledge)),
                    "active": knowledge["catalog"]["active_count"],
                }
            )
        )
        return
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parents[5] / "docker/neo4j/.env")
    print(
        import_knowledge(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            username=os.getenv("NEO4J_USERNAME", "neo4j"),
            password=os.environ["NEO4J_PASSWORD"],
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
            knowledge=knowledge,
        )
    )


if __name__ == "__main__":
    main()
