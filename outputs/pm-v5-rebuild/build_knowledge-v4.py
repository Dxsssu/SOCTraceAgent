"""Build six-field procedures from every aligned training attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    build_knowledge as build_training_history,
    DEFAULT_QUESTIONS_DIR,
    DEFAULT_SEMANTIC_KNOWLEDGE,
    DEFAULT_TRAJECTORIES,
    load_jsonl,
    load_train_questions,
    make_episode_id,
    map_trajectories_to_train,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import (
    retrieval_view,
)

from . import DEFAULT_KNOWLEDGE_PATH
from .fragments import render_fragments
from .mining import mine, records_from_training

DEFAULT_EPISODIC_KNOWLEDGE = (
    Path(__file__).resolve().parents[2]
    / "episodic_memory/excytin_bench/episodic_memory.json"
)
DEFAULT_CURATION_PATH = Path(__file__).with_name("curation.json")
CATALOG_ID = "excytin-procedures-v4"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_bundle(
    *,
    semantic_path=DEFAULT_SEMANTIC_KNOWLEDGE,
    episodic_path=DEFAULT_EPISODIC_KNOWLEDGE,
    trajectories_path=DEFAULT_TRAJECTORIES,
    questions_dir=DEFAULT_QUESTIONS_DIR,
    curation_path=DEFAULT_CURATION_PATH,
):
    semantic = retrieval_view(json.loads(Path(semantic_path).read_text()))
    episodic = json.loads(Path(episodic_path).read_text())
    if episodic["generation_policy"].get("source_split") != "train":
        raise ValueError("Procedures can only be built from training episodes")
    if digest(trajectories_path) != episodic["catalog"]["source_sha256"]:
        raise ValueError("Training trajectory source differs from episodic snapshot")
    mapped = map_trajectories_to_train(
        load_jsonl(Path(trajectories_path)), load_train_questions(Path(questions_dir))
    )
    history = build_training_history(trajectories_path=Path(trajectories_path), questions_dir=Path(questions_dir), semantic_path=Path(semantic_path))
    if {make_episode_id(s) for _, s, _ in mapped} != {
        e["episode_id"] for e in history["episodes"]
    }:
        raise ValueError("Training question alignment differs from episodic snapshot")
    records = records_from_training(history["episodes"], mapped, semantic)
    procedures, audit = mine(records, semantic)
    procedures = render_fragments(procedures, audit, semantic, records)
    # Reviewed wording changes live outside Agent memory and survive rebuilding.
    curation = (
        json.loads(Path(curation_path).read_text())
        if curation_path and Path(curation_path).exists()
        else {}
    )
    by_name = {p["name"]: p for p in procedures}
    for name, patch in curation.items():
        if name not in by_name or not set(patch) <= {
            "goal",
            "operations",
            "input_slots",
            "semantic_refs",
        }:
            raise ValueError("Invalid curation target/fields")
        by_name[name].update(patch)
    k = {
        "format_version": "4.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "generation_policy": {
            "source_split": "train",
            "contains_sql": False,
            "contains_log_values": False,
            "contains_answers": False,
            "contains_test_data": False,
            "method": "systematic_training_operations",
            "min_episodes": 2,
            "min_incidents_active": 2,
            "guards_source": "engineering_constraints_not_mined_outcomes",
        },
        "environment_id": episodic["environment_id"],
        "semantic_catalog": {
            "catalog_id": semantic["catalog"]["catalog_id"],
            "schema_hash": semantic["catalog"]["schema_hash"],
        },
        "episodic_catalog": {
            "episodic_catalog_id": episodic["catalog"]["episodic_catalog_id"],
            "source_sha256": episodic["catalog"]["source_sha256"],
        },
        "catalog": {
            "procedural_catalog_id": CATALOG_ID,
            "procedure_count": len(procedures),
            "active_count": sum(p["status"] == "active" for p in procedures),
            "training_episode_count": len(mapped),
            "training_attempt_count": len(records),
            "semantic_snapshot_sha256": digest(semantic_path),
            "episodic_snapshot_sha256": digest(episodic_path),
        },
        "procedures": procedures,
    }
    audit.update(
        source_split="train",
        source_sha256=digest(trajectories_path),
        curated_names=sorted(curation),
        limitations=[
            "Support frequency is not causal effectiveness.",
            "Value-linked pivots verify literal reuse plus compatible projections, not exact source-column lineage.",
            "Repair traces show temporal recovery, not proof that the proposed adjustment caused success.",
        ],
    )
    return k, audit


def build_knowledge(**kwargs):
    return build_bundle(**kwargs)[0]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    p.add_argument("--audit-output", type=Path)
    p.add_argument("--curation", type=Path, default=DEFAULT_CURATION_PATH)
    args = p.parse_args(argv)
    old = (
        json.loads(args.output.read_text())
        if args.output.exists()
        else {"procedures": []}
    )
    k, audit = build_bundle(curation_path=args.curation)
    from .import_neo4j import validate_knowledge

    validate_knowledge(k)
    before = {p["name"]: p for p in old["procedures"]}
    after = {p["name"]: p for p in k["procedures"]}
    audit["delta"] = {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(
            n for n in after.keys() & before.keys() if after[n] != before[n]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(k, ensure_ascii=False, indent=2) + "\n")
    audit_path = args.audit_output or args.output.with_name("build_audit.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                **k["catalog"],
                "families": audit["family_counts"],
                "active_families": audit["active_family_counts"],
                "supported_attempts": audit["active_supported_attempt_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
