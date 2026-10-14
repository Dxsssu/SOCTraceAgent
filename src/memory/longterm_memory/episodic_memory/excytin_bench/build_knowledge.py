"""Build three-field failure-recovery memory from aligned training errors."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from . import DEFAULT_KNOWLEDGE_PATH
from .repair_rules import RULES
from .sql_parser import classify_error
from .training_history import (
    DEFAULT_QUESTIONS_DIR,
    DEFAULT_SEMANTIC_KNOWLEDGE,
    DEFAULT_TRAJECTORIES,
    stable_hash,
    utc_now,
)
from .training_history import (
    build_knowledge as build_training_history,
)

CATALOG_ID = "excytin-failure-recovery-v2"


def entry_id(entry):
    return "recovery:" + stable_hash(entry)[:20]


def build_bundle(
    *,
    trajectories_path=DEFAULT_TRAJECTORIES,
    questions_dir=DEFAULT_QUESTIONS_DIR,
    semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE,
):
    history = build_training_history(
        trajectories_path=Path(trajectories_path),
        questions_dir=Path(questions_dir),
        semantic_path=Path(semantic_path),
    )
    sm = json.loads(Path(semantic_path).read_text())
    fields = {t["name"]: {f["name"] for f in t["field"]} for t in sm["tables"]}
    for (table, _), (target, required, _) in RULES.items():
        if (
            table not in fields
            or target not in fields
            or not set(required) <= fields[target]
        ):
            raise ValueError(f"Invalid recovery schema reference: {table} -> {target}")
    groups, excluded = {}, []
    for episode in history["episodes"]:
        for i, failed in enumerate(episode["attempts"]):
            if failed["result_status"] != "error":
                continue
            error = failed["error_detail"]
            _kind, subject = classify_error(error)
            tables = [r["table_id"].split(":", 1)[1] for r in failed["table_refs"]]
            key = (tables[0], subject) if len(tables) == 1 else None
            source = {
                "episode_id": episode["episode_id"],
                "attempt_id": failed["attempt_id"],
                "incident": episode["source_incident"],
                "error": error,
                "failed_sql_template": failed["sql_template"],
            }
            if key not in RULES:
                excluded.append(
                    {**source, "reason": "no_reviewed_context_specific_repair"}
                )
                continue
            target, required, guidance = RULES[key]
            # Do not mistake DESCRIBE or an unrelated successful query for a repair.
            candidates = [
                a
                for a in episode["attempts"][i + 1 :]
                if a["query_kind"] == "evidence_query"
                and a["execution_status"] == "executed"
                and not a["unresolved_field_names"]
                and any(
                    r["table_id"].split(":", 1)[1] == target for r in a["table_refs"]
                )
                and any(
                    r["field_id"] == f"{sm['catalog']['catalog_id']}:{target}:{f}"
                    for f in required
                    for r in a["field_refs"]
                )
            ]
            if not candidates:
                excluded.append(
                    {**source, "reason": "no_later_schema_valid_evidence_query"}
                )
                continue
            recovery = candidates[0]
            clause = "读取结果字段" if "field list" in error else "构造筛选条件"
            group_key = (key, error)
            if group_key not in groups:
                groups[group_key] = {
                    "entry": {
                        "background": f"在 MySQL 的 {key[0]} 表中{clause}，使用 {subject} 时出现字段错误。适用于该表与字段含义对应的调查；其他表上的同名报错需另行判断。",
                        "error": error,
                        "repair": guidance
                        + " 先确认当前数据库结构，再用当前案件已取得的值构造只读 SQL 并执行验证；修复数据库错误不等于获得有效证据或完成原调查。",
                    },
                    "sources": [],
                }
            groups[group_key]["sources"].append(
                {
                    **source,
                    "recovery_attempt_id": recovery["attempt_id"],
                    "recovery_sql_template": recovery["sql_template"],
                    "recovery_result_status": recovery["result_status"],
                    "basis": "reviewed_schema_guidance_with_later_executed_query",
                }
            )
    ordered = sorted(
        groups.values(), key=lambda g: (g["entry"]["background"], g["entry"]["error"])
    )
    entries = [g["entry"] for g in ordered]
    included = sum(len(g["sources"]) for g in ordered)
    catalog = {
        "episodic_catalog_id": CATALOG_ID,
        "source_sha256": history["catalog"]["source_sha256"],
        "questions_manifest_hash": history["catalog"]["questions_manifest_hash"],
        "entry_count": len(entries),
        "error_attempt_count": history["catalog"]["error_attempt_count"],
        "supported_error_count": included,
        "excluded_error_count": len(excluded),
    }
    knowledge = {
        "format_version": "2.0",
        "generated_at": utc_now(),
        "generation_policy": {
            "source_split": "train",
            "error_only": True,
            "contains_raw_observations": False,
            "contains_answers": False,
            "contains_test_data": False,
        },
        "environment_id": history["environment_id"],
        "semantic_catalog": history["semantic_catalog"],
        "catalog": catalog,
        "entries": entries,
    }
    audit = {
        "catalog": catalog,
        "trajectory_alignment": history["catalog"]["trajectory_alignment"],
        "entries": [
            {"entry_id": entry_id(g["entry"]), "sources": g["sources"]} for g in ordered
        ],
        "excluded": excluded,
        "exclusion_counts": dict(Counter(x["reason"] for x in excluded)),
        "limitations": [
            "Later execution supports SQL recovery, not causal success or answer correctness.",
            "Guidance is reviewed against schema; it is not an automatic semantic proof of equivalence.",
            "Unsupported errors remain excluded; empty results are not errors.",
        ],
    }
    return knowledge, audit


def build_knowledge(**kwargs):
    return build_bundle(**kwargs)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectories", type=Path, default=DEFAULT_TRAJECTORIES)
    parser.add_argument("--questions-dir", type=Path, default=DEFAULT_QUESTIONS_DIR)
    parser.add_argument(
        "--semantic-knowledge", type=Path, default=DEFAULT_SEMANTIC_KNOWLEDGE
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    args = parser.parse_args()
    knowledge, audit = build_bundle(
        trajectories_path=args.trajectories,
        questions_dir=args.questions_dir,
        semantic_path=args.semantic_knowledge,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n")
    args.output.with_name("build_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(knowledge["catalog"], ensure_ascii=False))


if __name__ == "__main__":
    main()
