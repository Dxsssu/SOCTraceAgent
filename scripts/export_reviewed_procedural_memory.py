"""Export a completed, audited training-only snapshot into the package."""

from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.review_procedural_trajectories import OUTPUT, PACKAGE, save, sha
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import (
    TrajectorySnippetStore,
)


def main():
    catalog = TrajectorySnippetStore.load(OUTPUT / "catalog.json").document
    ledger = json.loads((OUTPUT / "reading_ledger.json").read_text())
    coverage = json.loads((OUTPUT / "coverage.json").read_text())
    validation = json.loads((OUTPUT / "all_support_validation.json").read_text())
    published = json.loads((OUTPUT / "published.json").read_text())
    assert not ledger["missing_indices"] and ledger["reviewed_trajectories"] == 237
    assert ledger["reviewed_sql_attempts"] == ledger["expected_sql_attempts"] == 1605
    assert (
        ledger["reviewer_checked_trajectories"] == 237
        and not ledger["missing_reviewer_indices"]
    )
    assert validation["catalog_id"] == catalog["catalog_id"], (
        "validation snapshot is stale"
    )
    assert (
        not validation["missing_indices"]
        and validation["train_only"]
        and not validation["test_used"]
    )
    assert validation["query_supports"] == validation["query_passed"], (
        "some training query replays failed"
    )
    assert validation["chain_supports"] == validation["chain_passed"], (
        "some retained chain supports failed"
    )
    assert coverage["reviewed"] == 237 and coverage["pending"] == 0, (
        "candidate validation queue is incomplete"
    )
    assert (
        coverage["unique_proposals"]
        == coverage["validated_snippets"] + coverage["rejected"]
    )
    assert all(s["validation"]["chain_binding"] for s in catalog["snippets"])
    destination = PACKAGE / "excytin"
    destination.mkdir(exist_ok=True)
    for name in (
        "catalog.json",
        "reading_ledger.json",
        "branch_recipes.json",
        "recovery_rules.json",
        "unresolved_relations.json",
        "unsupported_queries.json",
        "coverage.json",
        "all_support_validation.json",
    ):
        save(destination / name, json.loads((OUTPUT / name).read_text()))
    rejected = {
        ident: published["replays"][ident]
        for ident in published.get("failed_ids", [])
        if ident not in published["ids"]
    }
    save(destination / "rejected_replays.json", {"records": rejected})
    manifests = [
        json.loads(path.read_text()) for path in sorted(OUTPUT.glob("manifest_*.json"))
    ]
    save(
        destination / "build_manifest.json",
        {
            "source_sha256": sorted({m["source_sha256"] for m in manifests}),
            "prompt_sha256": sorted({m["prompt_sha256"] for m in manifests}),
            "providers": sorted({m["provider"] for m in manifests}),
            "models": sorted({m["model"] for m in manifests}),
            "alignment": "actual first user prompt matched to train question; top-level trajectory key is not trusted",
            "schema_snapshot_sha256": sha((OUTPUT / "target_schemas.json").read_text()),
            "reviewer_note_sources": ledger["reviewer_note_sources"],
            "source_split": "train",
            "test_used": False,
        },
    )
    summary = {
        "trajectories": 237,
        "reviewer_checked_trajectories": ledger["reviewer_checked_trajectories"],
        "sql_attempts": 1605,
        "snippets": len(catalog["snippets"]),
        "atomic": sum(s["kind"] == "atomic" for s in catalog["snippets"]),
        "multi_step": sum(s["kind"] == "evidence_chain" for s in catalog["snippets"]),
        "max_steps": max(len(s["steps"]) for s in catalog["snippets"]),
        "templates": len(catalog["templates"]),
        "intent_variants": sum(
            len(s.get("intent_variants", [])) for s in catalog["snippets"]
        ),
        "branch_recipes": ledger["reference_branch_recipes"],
        "recovery_rules": ledger["reference_recovery_rules"],
        "unresolved_value_relations": ledger["unresolved_value_relations"],
        "unsupported_successful_queries": ledger["unsupported_successful_queries"],
        "rejected_chain_replays": len(rejected),
        "query_replays": validation["query_supports"],
        "query_replays_passed": validation["query_passed"],
        "chain_support_replays": validation["chain_supports"],
        "chain_support_replays_passed": validation["chain_passed"],
        "chain_step_replays": validation["chain_steps"],
        "catalog_id": catalog["catalog_id"],
        "workflow_enabled": False,
        "status": "candidate",
    }
    save(destination / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
