from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import (
    DEFAULT_EPISODIC_KNOWLEDGE,
    DEFAULT_INSIGHTS,
    DEFAULT_QUESTIONS_DIR,
    DEFAULT_SEMANTIC_KNOWLEDGE,
    build_knowledge,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j import (
    graph_rows,
    load_knowledge,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/procedural_memory/excytin_bench"
)
KNOWLEDGE_PATH = PACKAGE_DIR / "procedural_memory.json"
SCHEMA_PATH = PACKAGE_DIR / "procedure.schema.json"
SEMANTIC_PATH = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)
EPISODIC_PATH = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/episodic_memory/excytin_bench/episodic_memory.json"
)


def load_snapshot() -> dict:
    return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))


def test_snapshot_contains_the_expected_investigation_skills() -> None:
    knowledge = load_snapshot()
    procedures = {procedure["skill_id"]: procedure for procedure in knowledge["procedures"]}
    assert set(procedures) == {
        "alert-centered-investigation",
        "email-threat-investigation",
        "endpoint-process-investigation",
        "identity-signin-investigation",
        "network-activity-investigation",
    }
    assert knowledge["catalog"]["procedure_count"] == 5
    assert knowledge["catalog"]["phase_count"] == 27
    assert knowledge["catalog"]["support_edge_count"] == 460
    assert knowledge["catalog"]["exemplar_edge_count"] == 100
    assert knowledge["catalog"]["insight_count"] == 151

    assert procedures["alert-centered-investigation"]["support_episode_count"] == 237
    assert procedures["email-threat-investigation"]["support_episode_count"] == 48
    assert procedures["endpoint-process-investigation"]["support_episode_count"] == 98
    assert procedures["identity-signin-investigation"]["support_episode_count"] == 49
    assert procedures["network-activity-investigation"]["support_episode_count"] == 28
    assert all(procedure["status"] == "active" for procedure in procedures.values())
    assert all(procedure["retrieval_eligible"] for procedure in procedures.values())


def test_snapshot_is_schema_valid_and_has_consistent_sequences() -> None:
    knowledge = load_snapshot()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(knowledge)
    load_knowledge(KNOWLEDGE_PATH)

    for procedure in knowledge["procedures"]:
        phases = procedure["phases"]
        assert [phase["sequence_no"] for phase in phases] == list(
            range(1, len(phases) + 1)
        )
        assert len(procedure["transitions"]) == len(phases) - 1
        assert procedure["reference_solution_count"] == procedure["support_episode_count"]
        assert procedure["reference_path_count"] == procedure["support_episode_count"]


def test_semantic_and_episodic_references_are_valid() -> None:
    knowledge = load_snapshot()
    semantic = json.loads(SEMANTIC_PATH.read_text(encoding="utf-8"))
    episodic = json.loads(EPISODIC_PATH.read_text(encoding="utf-8"))
    table_ids = {table["table_id"] for table in semantic["tables"]}
    join_key_ids = {item["join_key_id"] for item in semantic["join_keys"]}
    episode_ids = {episode["episode_id"] for episode in episodic["episodes"]}
    attempt_ids = {
        attempt["attempt_id"]
        for episode in episodic["episodes"]
        for attempt in episode["attempts"]
    }
    assert knowledge["semantic_catalog"] == {
        "catalog_id": semantic["catalog"]["catalog_id"],
        "schema_hash": semantic["catalog"]["schema_hash"],
    }
    assert knowledge["episodic_catalog"] == {
        "episodic_catalog_id": episodic["catalog"]["episodic_catalog_id"],
        "source_sha256": episodic["catalog"]["source_sha256"],
    }
    for procedure in knowledge["procedures"]:
        assert set(procedure["semantic_table_ids"]) <= table_ids
        assert set(procedure["semantic_join_key_ids"]) <= join_key_ids
        assert set(procedure["support_episode_ids"]) <= episode_ids
        for phase in procedure["phases"]:
            assert set(phase["semantic_table_ids"]) <= table_ids
            assert set(phase["semantic_join_key_ids"]) <= join_key_ids
            assert set(phase["exemplar_attempt_ids"]) <= attempt_ids


def test_skill_documents_exist_and_keep_query_details_out_of_procedural_memory() -> None:
    knowledge = load_snapshot()
    policy = knowledge["generation_policy"]
    assert policy == {
        "source_split": "train",
        "contains_sql": False,
        "contains_log_values": False,
        "contains_answers": False,
        "contains_test_data": False,
        "skill_oriented": True,
    }
    for procedure in knowledge["procedures"]:
        skill_path = PACKAGE_DIR / procedure["skill_path"]
        text = skill_path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert f"name: {procedure['skill_id']}" in text
        assert "## Procedure" in text
        assert "## Semantic requirements" in text
        assert "## Episodic retrieval" in text
        assert "## Completion criteria" in text
        assert "```sql" not in text.lower()
        assert "sql_template" not in text

    procedure_text = json.dumps(knowledge["procedures"], ensure_ascii=False)
    assert "sql_template" not in procedure_text
    assert "source_split\": \"test" not in procedure_text
    assert not re.search(r"https?://", procedure_text, re.IGNORECASE)
    assert not re.search(r"\bS-\d(?:-\d+){2,}\b", procedure_text, re.IGNORECASE)
    assert not re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", procedure_text, re.IGNORECASE)


def test_graph_rows_have_expected_cardinality() -> None:
    rows = graph_rows(load_snapshot())
    assert len(rows["procedures"]) == 5
    assert len(rows["phases"]) == 27
    assert len(rows["procedure_phases"]) == 27
    assert len(rows["transitions"]) == 22
    assert len(rows["extensions"]) == 4
    assert len(rows["table_refs"]) == 241
    assert len(rows["join_key_refs"]) == 61
    assert len(rows["supports"]) == 460
    assert len(rows["exemplars"]) == 100


@pytest.mark.skipif(
    not (
        DEFAULT_SEMANTIC_KNOWLEDGE.exists()
        and DEFAULT_EPISODIC_KNOWLEDGE.exists()
        and DEFAULT_INSIGHTS.exists()
        and DEFAULT_QUESTIONS_DIR.exists()
    ),
    reason="The ignored ExCyTIn-Bench source data is not installed",
)
def test_rebuild_is_deterministic_except_for_generation_time() -> None:
    rebuilt = build_knowledge(
        semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE,
        episodic_path=DEFAULT_EPISODIC_KNOWLEDGE,
        insights_path=DEFAULT_INSIGHTS,
        questions_dir=DEFAULT_QUESTIONS_DIR,
    )
    committed = deepcopy(load_snapshot())
    rebuilt.pop("generated_at")
    committed.pop("generated_at")
    assert rebuilt == committed
