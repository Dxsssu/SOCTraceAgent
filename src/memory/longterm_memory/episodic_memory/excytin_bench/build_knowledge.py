"""Build a sanitized ExCyTIn-Bench episodic-memory snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import DEFAULT_KNOWLEDGE_PATH
from .sanitize import iter_scalar_strings, normalize_error_detail, sanitize_text
from .sql_parser import (
    SemanticIndex,
    analyze_sql,
    classify_result,
    extract_execute_sql,
    is_submit,
)

PACKAGE_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_TRAJECTORIES = (
    REPOSITORY_ROOT
    / "data/excytin-bench/github/secgym/agents/expel_train/corrects.jsonl"
)
DEFAULT_QUESTIONS_DIR = REPOSITORY_ROOT / "data/excytin-bench/huggingface/questions/train"
DEFAULT_SEMANTIC_KNOWLEDGE = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)

EPISODIC_CATALOG_ID = "excytin-episodes-v1"
EXPECTED_EPISODES = 237
EXPECTED_ATTEMPTS = 1605
EXPECTED_ERRORS = 81


ERROR_TYPE_DESCRIPTIONS = {
    "unknown_column": "The query referenced a column that is absent from the target table schema.",
    "unknown_table": "The query referenced a table that is absent from the incident database.",
    "syntax_error": "MySQL rejected the query syntax.",
    "other_sql_error": "MySQL rejected the query for another database error.",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def relative_source(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not {"key", "value"} <= value.keys():
                raise ValueError(f"Invalid trajectory at {path}:{line_number}")
            rows.append(value)
    return rows


def load_train_questions(path: Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for question_path in sorted(path.glob("*.json")):
        match = re.search(r"incident_(\d+)", question_path.name)
        if not match:
            raise ValueError(f"Cannot derive incident id from {question_path}")
        payload = json.loads(question_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError(f"Expected a question list in {question_path}")
        for index, question in enumerate(payload):
            questions.append(
                {
                    "incident_id": f"incident_{match.group(1)}",
                    "question_index": index,
                    "source_file": question_path.name,
                    "payload": question,
                }
            )
    return questions


def trajectory_task_prompt(trajectory: dict[str, Any]) -> str:
    """Return the real task prompt from a trajectory conversation.

    The released ``corrects.jsonl`` has a known alignment defect: for most
    rows, the top-level ``key`` belongs to a different row than
    ``value.messages``.  The first user message in ``value.messages`` is the
    authoritative task that produced the SQL trajectory; later user messages
    are database observations.
    """

    messages = trajectory.get("value", {}).get("messages", [])
    for message in messages:
        if message.get("role") == "user":
            prompt = str(message.get("content") or "").strip()
            if not prompt:
                break
            return prompt
    raise ValueError("Trajectory is missing its initial user task prompt")


def source_key_matches_task_prompt(trajectory: dict[str, Any]) -> bool:
    key_question = str(trajectory.get("key", {}).get("question") or "").strip()
    return bool(key_question) and key_question in trajectory_task_prompt(trajectory)


def map_trajectories_to_train(
    trajectories: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    mapped: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    used_sources: set[tuple[str, int]] = set()
    for trajectory in trajectories:
        task_prompt = trajectory_task_prompt(trajectory)
        candidates = [
            item
            for item in questions
            if str(item["payload"]["question"]).strip() in task_prompt
        ]
        match_method = "trajectory_user_prompt_question_exact"
        if len(candidates) != 1:
            raise ValueError(
                f"Trajectory must map to exactly one train question; found {len(candidates)} "
                f"for initial user prompt {task_prompt[:300]!r}"
            )
        source = candidates[0]
        source_key = (source["source_file"], source["question_index"])
        if source_key in used_sources:
            raise ValueError(f"Multiple trajectories mapped to {source_key}")
        used_sources.add(source_key)
        mapped.append((trajectory, source, match_method))
    return mapped


def make_episode_id(source: dict[str, Any]) -> str:
    payload = source["payload"]
    digest = stable_hash(
        {
            "split": "train",
            "incident_id": source["incident_id"],
            "question_index": source["question_index"],
            "question": payload["question"],
        }
    )[:20]
    return f"{EPISODIC_CATALOG_ID}:episode:{digest}"


def table_names(table_refs: list[dict[str, Any]]) -> list[str]:
    return [ref["table_id"].split(":", 1)[1] for ref in table_refs]


def field_names(field_refs: list[dict[str, Any]]) -> list[str]:
    return [ref["field_id"].rsplit(":", 1)[1] for ref in field_refs]


def build_attempt(
    *,
    episode_id: str,
    sequence_no: int,
    sql: str,
    observation: str,
    semantic: SemanticIndex,
    protected_values: list[str],
) -> dict[str, Any]:
    parsed = analyze_sql(sql, semantic)
    execution_status, result_status, error_type, error_subject = classify_result(
        parsed["query_kind"], observation
    )
    if error_subject and error_type == "unknown_column":
        unresolved_names = set(parsed["unresolved_field_names"])
        if not any(name.rsplit(".", 1)[-1].lower() == error_subject.lower() for name in unresolved_names):
            unresolved_names.add(error_subject)
        parsed["unresolved_field_names"] = sorted(unresolved_names)

    names = table_names(parsed["table_refs"])
    fields = field_names(parsed["field_refs"])
    retrieval_parts = [
        parsed["query_kind"].replace("_", " "),
        f"tables: {', '.join(names)}" if names else "tables: none",
        f"fields: {', '.join(fields)}" if fields else "fields: none",
        f"result: {result_status.replace('_', ' ')}",
        f"SQL template: {parsed['sql_template']}",
    ]
    if error_type:
        retrieval_parts.insert(3, f"error: {error_type.replace('_', ' ')}")

    attempt: dict[str, Any] = {
        "attempt_id": f"{episode_id}:attempt:{sequence_no:02d}",
        "sequence_no": sequence_no,
        "query_kind": parsed["query_kind"],
        "sql_template": parsed["sql_template"],
        "parameter_types": parsed["parameter_types"],
        "execution_status": execution_status,
        "result_status": result_status,
        "table_refs": parsed["table_refs"],
        "field_refs": parsed["field_refs"],
        "join_key_ids": parsed["join_key_ids"],
        "unresolved_field_names": parsed["unresolved_field_names"],
        "retrieval_text": " | ".join(retrieval_parts),
    }
    if error_type:
        attempt["error_type_id"] = f"{EPISODIC_CATALOG_ID}:error:{error_type}"
        attempt["error_detail"] = sanitize_text(
            normalize_error_detail(observation), protected_values=protected_values
        )
    return attempt


def build_episode(
    trajectory: dict[str, Any],
    source: dict[str, Any],
    match_method: str,
    semantic: SemanticIndex,
) -> dict[str, Any]:
    payload = source["payload"]
    protected_values = list(iter_scalar_strings(payload.get("answer")))
    task_template = sanitize_text(payload["question"], protected_values=protected_values)
    episode_id = make_episode_id(source)

    attempts: list[dict[str, Any]] = []
    submit_count = 0
    messages = trajectory["value"]["messages"]
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        sql = extract_execute_sql(message.get("content", ""))
        if sql is not None:
            if index + 1 >= len(messages) or messages[index + 1].get("role") != "user":
                raise ValueError(f"Missing observation after SQL in {episode_id}")
            attempts.append(
                build_attempt(
                    episode_id=episode_id,
                    sequence_no=len(attempts) + 1,
                    sql=sql,
                    observation=messages[index + 1].get("content", ""),
                    semantic=semantic,
                    protected_values=protected_values,
                )
            )
        elif is_submit(message.get("content", "")):
            submit_count += 1
        else:
            raise ValueError(f"Unrecognized assistant action in {episode_id}")
    if submit_count != 1:
        raise ValueError(
            f"Expected one submit action in {episode_id}; found {submit_count}"
        )

    transitions: list[dict[str, str]] = []
    for current, following in pairwise(attempts):
        transitions.append(
            {
                "from_attempt_id": current["attempt_id"],
                "to_attempt_id": following["attempt_id"],
                "relation_type": "NEXT",
            }
        )
        if current["execution_status"] == "sql_error" and following["execution_status"] == "executed":
            transitions.append(
                {
                    "from_attempt_id": current["attempt_id"],
                    "to_attempt_id": following["attempt_id"],
                    "relation_type": "REPAIRED_BY",
                }
            )

    reward = float(trajectory["value"]["reward"])
    outcome = "success" if reward == 1.0 else "partial"
    used_tables = sorted({name for attempt in attempts for name in table_names(attempt["table_refs"])})
    retrieval_text = " | ".join(
        [
            "ExCyTIn-Bench training investigation",
            task_template,
            f"tables: {', '.join(used_tables)}" if used_tables else "tables: none",
            f"outcome: {outcome}",
        ]
    )
    return {
        "episode_id": episode_id,
        "source_dataset": "ExCyTIn-Bench",
        "source_split": "train",
        "source_incident": source["incident_id"],
        "source_question_file": source["source_file"],
        "source_question_index": source["question_index"],
        "source_match_method": match_method,
        "task_template": task_template,
        "reward": reward,
        "outcome": outcome,
        "retrieval_eligible": True,
        "retrieval_text": retrieval_text,
        "attempts": attempts,
        "transitions": transitions,
    }


def build_knowledge(
    *,
    trajectories_path: Path,
    questions_dir: Path,
    semantic_path: Path,
) -> dict[str, Any]:
    trajectories = load_jsonl(trajectories_path)
    questions = load_train_questions(questions_dir)
    semantic_knowledge = json.loads(semantic_path.read_text(encoding="utf-8"))
    semantic = SemanticIndex.from_knowledge(semantic_knowledge)
    mapped = map_trajectories_to_train(trajectories, questions)
    source_key_match_count = sum(source_key_matches_task_prompt(item) for item in trajectories)
    source_key_mismatch_count = len(trajectories) - source_key_match_count
    episodes = [
        build_episode(trajectory, source, match_method, semantic)
        for trajectory, source, match_method in mapped
    ]
    episodes.sort(key=lambda item: item["episode_id"])

    attempts = [attempt for episode in episodes for attempt in episode["attempts"]]
    error_counts = Counter(
        attempt["error_type_id"].rsplit(":", 1)[-1]
        for attempt in attempts
        if "error_type_id" in attempt
    )
    if len(episodes) != EXPECTED_EPISODES:
        raise ValueError(f"Expected {EXPECTED_EPISODES} episodes, found {len(episodes)}")
    if len(attempts) != EXPECTED_ATTEMPTS:
        raise ValueError(f"Expected {EXPECTED_ATTEMPTS} attempts, found {len(attempts)}")
    if sum(error_counts.values()) != EXPECTED_ERRORS:
        raise ValueError(f"Expected {EXPECTED_ERRORS} SQL errors, found {sum(error_counts.values())}")

    question_hashes = [
        {"path": path.name, "sha256": sha256_file(path)}
        for path in sorted(questions_dir.glob("*.json"))
    ]
    error_types = [
        {
            "error_type_id": f"{EPISODIC_CATALOG_ID}:error:{name}",
            "name": name,
            "description": ERROR_TYPE_DESCRIPTIONS[name],
            "attempt_count": error_counts[name],
        }
        for name in sorted(error_counts)
    ]
    outcome_counts = Counter(episode["outcome"] for episode in episodes)
    result_counts = Counter(attempt["result_status"] for attempt in attempts)
    match_counts = Counter(episode["source_match_method"] for episode in episodes)
    return {
        "format_version": "1.0",
        "generated_at": utc_now(),
        "generation_policy": {
            "source_split": "train",
            "retrieval_eligible_only": True,
            "sanitized": True,
            "contains_raw_observations": False,
            "contains_agent_thoughts": False,
            "contains_answers": False,
            "contains_test_data": False,
            "contains_vector_embeddings": False,
        },
        "environment_id": semantic_knowledge["environment"]["environment_id"],
        "semantic_catalog": {
            "catalog_id": semantic.catalog_id,
            "schema_hash": semantic.schema_hash,
        },
        "catalog": {
            "episodic_catalog_id": EPISODIC_CATALOG_ID,
            "name": "ExCyTIn-Bench Sanitized Investigation Episodes",
            "version": "1.0",
            "source": relative_source(trajectories_path),
            "source_sha256": sha256_file(trajectories_path),
            "questions_source": relative_source(questions_dir),
            "questions_manifest_hash": stable_hash(question_hashes),
            "episode_count": len(episodes),
            "attempt_count": len(attempts),
            "error_attempt_count": sum(error_counts.values()),
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "result_counts": dict(sorted(result_counts.items())),
            "source_match_counts": dict(sorted(match_counts.items())),
            "trajectory_alignment": {
                "authoritative_source": "value.messages.first_user_prompt",
                "matched_count": len(mapped),
                "source_key_match_count": source_key_match_count,
                "source_key_mismatch_count": source_key_mismatch_count,
                "fail_on_unmatched_or_ambiguous": True,
            },
        },
        "error_types": error_types,
        "episodes": episodes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ExCyTIn-Bench episodic memory.")
    parser.add_argument("--trajectories", type=Path, default=DEFAULT_TRAJECTORIES)
    parser.add_argument("--questions-dir", type=Path, default=DEFAULT_QUESTIONS_DIR)
    parser.add_argument("--semantic-knowledge", type=Path, default=DEFAULT_SEMANTIC_KNOWLEDGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    knowledge = build_knowledge(
        trajectories_path=args.trajectories.resolve(),
        questions_dir=args.questions_dir.resolve(),
        semantic_path=args.semantic_knowledge.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {args.output.resolve()}: {knowledge['catalog']['episode_count']} episodes, "
        f"{knowledge['catalog']['attempt_count']} attempts, and "
        f"{knowledge['catalog']['error_attempt_count']} SQL errors."
    )


if __name__ == "__main__":
    main()
