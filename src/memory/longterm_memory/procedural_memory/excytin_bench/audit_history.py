"""Inventory historical records without using test answers as build input."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .build_knowledge import DEFAULT_QUESTIONS_DIR, DEFAULT_TRAJECTORIES


def question_set(directory):
    return {
        str(q["question"]).strip()
        for p in Path(directory).glob("*.json")
        for q in json.loads(p.read_text())
    }


def inventory(roots, question_dir=DEFAULT_QUESTIONS_DIR):
    train = question_set(question_dir)
    test = question_set(Path(question_dir).parent / "test")
    items = []
    counts = Counter()
    hashes = set()
    for root in map(Path, roots):
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.json")):
            if p.name != "result.json" and p.parent.name != "cases":
                continue
            try:
                raw = p.read_bytes()
                r = json.loads(raw)
            except (ValueError, OSError):
                continue
            if not isinstance(r, dict) or not isinstance(r.get("question"), str):
                continue
            q = r["question"].strip()
            split = (
                "train"
                if q in train and q not in test
                else (
                    "test" if q in test and q not in train else "ambiguous_or_unknown"
                )
            )
            if r.get("split") and r["split"] != split:
                split = "ambiguous_or_unknown"
            sha = hashlib.sha256(raw).hexdigest()
            duplicate = sha in hashes
            hashes.add(sha)
            counts[split] += 1
            items.append(
                {
                    "path": str(p),
                    "sha256": sha,
                    "split": split,
                    "duplicate_content": duplicate,
                    "status": r.get("status"),
                    "disposition": "requires_train_adapter"
                    if split == "train"
                    else "excluded_from_build",
                }
            )
    db = Path("runtime/socagent.db")
    database_counts = {}
    if db.exists():
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            # Inventory only; application events have no verified benchmark split.
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('events','executions')"
            ):
                database_counts[row[0]] = conn.execute(
                    f"SELECT count(*) FROM {row[0]}"
                ).fetchone()[0]
    return {
        "included_training_source": str(DEFAULT_TRAJECTORIES),
        "training_trajectory_count": sum(
            bool(x.strip()) for x in DEFAULT_TRAJECTORIES.read_text().splitlines()
        ),
        "history_file_counts": dict(counts),
        "unique_history_contents": len(hashes),
        "application_database_counts": database_counts,
        "application_database_disposition": "unverified_split_not_used",
        "records": items,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output",
        type=Path,
        default=Path("runtime/procedural_memory/history_inventory.json"),
    )
    a = p.parse_args()
    result = inventory(["results", "runtime/excytin_bench"])
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "records"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
