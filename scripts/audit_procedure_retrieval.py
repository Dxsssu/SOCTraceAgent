"""Inspect initial retrieval across train questions without exposing answers."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from src.benchmarks.excytin_bench.runner import load_test_cases
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import ProcedureCardMemory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    memory = ProcedureCardMemory()
    cases = []
    for case in load_test_cases(Path("data/excytin-bench/huggingface/questions/train")):
        task = {key: case.question.get(key, "") for key in ("context", "question")}
        retrieved = memory.retrieve(task)
        cases.append({"case_id": case.case_id, "question": task["question"],
                      "retrieval": retrieved["card_retrieval"],
                      "purpose": retrieved["procedure_cards"][0]["purpose"] if retrieved["procedure_cards"] else "none"})
    summary = {"tasks": len(cases), "purpose_counts": dict(Counter(c["purpose"] for c in cases)),
               "status_counts": dict(Counter(c["retrieval"]["status"] for c in cases)),
               "target_counts": dict(Counter(",".join(c["retrieval"]["target_types"]) for c in cases)),
               "max_input_bytes": max(c["retrieval"]["input_bytes"] for c in cases),
               "mean_input_bytes": sum(c["retrieval"]["input_bytes"] for c in cases) / len(cases)}
    source = Path("src/memory/longterm_memory/procedural_memory/excytin_bench/cards/retrieval.py")
    report = {"scope": "All train questions, initial retrieval only, no answer labels or observations. Route existence is not relevance or correctness.",
              "catalog_sha256": memory.fingerprint, "retrieval_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "summary": summary, "cases": cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
