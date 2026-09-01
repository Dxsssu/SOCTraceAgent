from __future__ import annotations

import json
import re
from copy import deepcopy
from itertools import pairwise
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge import (
    DEFAULT_QUESTIONS_DIR,
    DEFAULT_SEMANTIC_KNOWLEDGE,
    DEFAULT_TRAJECTORIES,
    build_knowledge,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.import_neo4j import (
    graph_rows,
    load_knowledge,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/episodic_memory/excytin_bench"
)
KNOWLEDGE_PATH = PACKAGE_DIR / "episodic_memory.json"
SCHEMA_PATH = PACKAGE_DIR / "episode.schema.json"
SEMANTIC_PATH = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)


def load_snapshot() -> dict:
    return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))


def nested_attempts(knowledge: dict) -> list[dict]:
    return [attempt for episode in knowledge["episodes"] for attempt in episode["attempts"]]


def test_snapshot_matches_the_curated_train_trajectory_corpus() -> None:
    knowledge = load_snapshot()
    catalog = knowledge["catalog"]
    attempts = nested_attempts(knowledge)

    assert catalog["episode_count"] == len(knowledge["episodes"]) == 237
    assert catalog["attempt_count"] == len(attempts) == 1605
    assert catalog["error_attempt_count"] == 81
    assert catalog["outcome_counts"] == {"partial": 7, "success": 230}
    assert catalog["result_counts"] == {
        "empty_result": 358,
        "error": 81,
        "rows_returned": 472,
        "schema_result": 694,
    }
    assert catalog["source_match_counts"] == {
        "question_context_exact": 236,
        "question_only_unique": 1,
    }
    assert sum(not episode["attempts"] for episode in knowledge["episodes"]) == 1
    assert {episode["source_split"] for episode in knowledge["episodes"]} == {"train"}
    assert all(episode["retrieval_eligible"] for episode in knowledge["episodes"])


def test_snapshot_is_schema_valid_and_internally_consistent() -> None:
    knowledge = load_snapshot()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(knowledge)
    load_knowledge(KNOWLEDGE_PATH)

    episode_ids = [episode["episode_id"] for episode in knowledge["episodes"]]
    attempts = nested_attempts(knowledge)
    attempt_ids = [attempt["attempt_id"] for attempt in attempts]
    assert len(set(episode_ids)) == len(episode_ids)
    assert len(set(attempt_ids)) == len(attempt_ids)

    expected_repair_edges = 0
    for episode in knowledge["episodes"]:
        sequence = episode["attempts"]
        assert [attempt["sequence_no"] for attempt in sequence] == list(
            range(1, len(sequence) + 1)
        )
        next_edges = [
            transition
            for transition in episode["transitions"]
            if transition["relation_type"] == "NEXT"
        ]
        assert len(next_edges) == max(0, len(sequence) - 1)
        expected_repair_edges += sum(
            current["execution_status"] == "sql_error"
            and following["execution_status"] == "executed"
            for current, following in pairwise(sequence)
        )
    repair_edges = sum(
        transition["relation_type"] == "REPAIRED_BY"
        for episode in knowledge["episodes"]
        for transition in episode["transitions"]
    )
    assert repair_edges == expected_repair_edges == 79


def test_all_graph_links_target_existing_semantic_nodes() -> None:
    knowledge = load_snapshot()
    semantic = json.loads(SEMANTIC_PATH.read_text(encoding="utf-8"))
    table_ids = {table["table_id"] for table in semantic["tables"]}
    field_ids = {
        field["field_id"] for table in semantic["tables"] for field in table["fields"]
    }
    join_key_ids = {join_key["join_key_id"] for join_key in semantic["join_keys"]}
    attempts = nested_attempts(knowledge)

    assert knowledge["semantic_catalog"] == {
        "catalog_id": semantic["catalog"]["catalog_id"],
        "schema_hash": semantic["catalog"]["schema_hash"],
    }
    assert all(
        reference["table_id"] in table_ids
        for attempt in attempts
        for reference in attempt["table_refs"]
    )
    assert all(
        reference["field_id"] in field_ids
        for attempt in attempts
        for reference in attempt["field_refs"]
    )
    assert all(
        join_key_id in join_key_ids
        for attempt in attempts
        for join_key_id in attempt["join_key_ids"]
    )
    assert sum(len(attempt["table_refs"]) for attempt in attempts) == 1387
    assert sum(len(attempt["field_refs"]) for attempt in attempts) == 2047
    assert sum(len(attempt["join_key_ids"]) for attempt in attempts) == 1
    assert any(
        "SecurityAlert.Title" in attempt["unresolved_field_names"]
        for attempt in attempts
    )
    assert "excytin-schema-v1:SecurityAlert:Title" not in field_ids


def test_committed_snapshot_excludes_raw_and_sensitive_trajectory_content() -> None:
    knowledge = load_snapshot()
    policy = knowledge["generation_policy"]
    assert policy == {
        "source_split": "train",
        "retrieval_eligible_only": True,
        "sanitized": True,
        "contains_raw_observations": False,
        "contains_agent_thoughts": False,
        "contains_answers": False,
        "contains_test_data": False,
        "contains_vector_embeddings": False,
    }

    forbidden_keys = {
        "answer",
        "solution",
        "messages",
        "observation",
        "thought",
        "submitted_answer",
        "check_ans_response",
        "check_ans_reflection",
        "usage_summary",
        "info",
    }
    for episode in knowledge["episodes"]:
        assert not forbidden_keys.intersection(episode)
        for attempt in episode["attempts"]:
            assert not forbidden_keys.intersection(attempt)
            for parameter_type in attempt["parameter_types"]:
                assert f"<{parameter_type}_" in attempt["sql_template"]

    text = json.dumps(knowledge["episodes"], ensure_ascii=False)
    representative_secrets = (
        "170.54.121.63",
        "raphaelt@vnevado.alpineskihouse.co",
        "S-1-5-21-1874151667-3554330288-105586563-1715",
        "https://ms175052280.orangecliff-f53f26fd.eastus.azurecontainerapps.io",
    )
    assert all(secret.lower() not in text.lower() for secret in representative_secrets)
    assert not re.search(r"https?://", text, re.IGNORECASE)
    assert not re.search(r"\bS-\d(?:-\d+){2,}\b", text, re.IGNORECASE)
    assert not re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.IGNORECASE)


def test_graph_rows_have_the_expected_import_cardinality() -> None:
    rows = graph_rows(load_snapshot())
    assert len(rows["episodes"]) == 237
    assert len(rows["attempts"]) == 1605
    assert len(rows["episode_attempts"]) == 1605
    assert len(rows["table_refs"]) == 1387
    assert len(rows["field_refs"]) == 2047
    assert len(rows["join_key_refs"]) == 1
    assert len(rows["error_refs"]) == 81
    assert sum(row["relation_type"] == "NEXT" for row in rows["transitions"]) == 1369
    assert sum(row["relation_type"] == "REPAIRED_BY" for row in rows["transitions"]) == 79


@pytest.mark.skipif(
    not (DEFAULT_TRAJECTORIES.exists() and DEFAULT_QUESTIONS_DIR.exists()),
    reason="The ignored ExCyTIn-Bench source data is not installed",
)
def test_rebuild_is_deterministic_except_for_generation_time() -> None:
    rebuilt = build_knowledge(
        trajectories_path=DEFAULT_TRAJECTORIES,
        questions_dir=DEFAULT_QUESTIONS_DIR,
        semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE,
    )
    committed = deepcopy(load_snapshot())
    rebuilt.pop("generated_at")
    committed.pop("generated_at")
    assert rebuilt == committed
