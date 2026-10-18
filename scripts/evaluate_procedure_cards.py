"""Paired real-MySQL development evaluation of Planner-only procedure cards.

No test answers are sent to investigation roles. Development runs are explicitly
labelled and retained; --run-id prevents overwriting an earlier iteration.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from src.benchmarks.excytin_bench.runner import ExcytinBenchmarkRunner, load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import DEFAULT_CATALOG
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--cases", nargs="+")
    selection.add_argument("--case-manifest", type=Path)
    parser.add_argument("--purpose", choices=["development", "confirmation"], default="development")
    parser.add_argument("--modes", nargs="+", choices=["pm", "baseline"], default=["pm", "baseline"])
    parser.add_argument("--split", choices=["train", "test"], default="train")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--executor-format-attempts", type=int, choices=[1, 2], default=2)
    parser.add_argument("--compact-evidence", action="store_true")
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    if "pm" in args.modes:
        parser.error("LTM is detached from workflow during reconstruction; use --modes baseline or checkpoint dfb8646 for PM experiments")
    load_dotenv()
    load_dotenv("docker/excytin-mysql/.env")
    cases = {c.case_id: c for c in load_test_cases(Path("data/excytin-bench/huggingface/questions") / args.split)}
    selection_manifest = None
    if args.case_manifest:
        selection_manifest = json.loads(args.case_manifest.read_text())
        args.cases = [item["case_id"] for item in selection_manifest["cases"]]
        for item in selection_manifest["cases"]:
            case = cases.get(item["case_id"])
            if case is None or hashlib.sha256(json.dumps(case.question, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != item["question_sha256"]:
                parser.error(f"Frozen question missing or changed: {item['case_id']}")
    args.cases = args.cases or ["incident_322-q0013"]
    if set(args.cases) - cases.keys():
        parser.error("Unknown cases")
    if len(set(args.cases)) != len(args.cases):
        parser.error("Duplicate cases")
    if args.purpose == "confirmation" and (args.split != "test" or selection_manifest is None
            or len(args.cases) < 40 or set(args.modes) != {"pm", "baseline"}):
        parser.error("Confirmation requires test split, frozen manifest, >=40 cases and both modes")
    out = Path("runtime/procedure_cards") / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    if selection_manifest is not None:
        write(out / "selection_manifest.json", selection_manifest)
    catalog_snapshot = out / "catalog.json"
    catalog_snapshot.write_bytes(args.catalog.read_bytes())
    implementation_files = [Path("src/benchmarks/excytin_bench/workflow.py"), Path("src/schema/itt.py"), Path("src/agent/executor.py"), Path("src/agent/planner.py"),
                            Path("src/agent/reviewer.py"), Path("src/schema/reviewer_output.py"),
                            Path("src/agent/verifier.py"),
                            Path("src/schema/planner_operations.py"),
                            Path("src/schema/evidence_view.py"),
                            Path("src/memory/longterm_memory/procedural_memory/excytin_bench/cards/recovery.py"),
                            Path("src/memory/longterm_memory/procedural_memory/excytin_bench/cards/retrieval.py"), Path(__file__)]
    for source in implementation_files:
        relative = source.relative_to(Path.cwd()) if source.is_absolute() else source
        target = out / "implementation" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    write(out / "manifest.json", {"split": args.split, "purpose": args.purpose, "cases": args.cases,
                                 "selection_manifest_sha256": hashlib.sha256(args.case_manifest.read_bytes()).hexdigest() if args.case_manifest else None,
                                 "modes": args.modes, "max_steps": args.max_steps,
                                 "catalog_sha256": hashlib.sha256(catalog_snapshot.read_bytes()).hexdigest(),
                                 "catalog_source": str(args.catalog),
                                 "catalog": json.loads(catalog_snapshot.read_text()),
                                 "other_ltm_enabled": False, "live_mysql": True,
                                 "executor_format_attempts": args.executor_format_attempts,
                                 "compact_evidence": args.compact_evidence,
                                 "provider": os.getenv("SOCAGENT_LLM_PROVIDER", "paratera"),
                                 "implementation_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in implementation_files}})
    results = []
    for case_id in args.cases:
        for mode in args.modes:
            llm = TrackedLLM(None)
            calls = []
            call_path = out / mode / f"{case_id}-calls.json"

            def invoke(system, prompt):
                record = {"index": len(calls)+1, "system": system, "prompt": prompt}
                calls.append(record)
                write(call_path, {"model": llm.client.config.model, "sdk_max_retries": llm.client.config.max_retries, "usage": dict(llm.usage), "calls": calls})
                started = time.monotonic()
                before = dict(llm.usage)
                try:
                    response = llm(system, prompt)
                    record["response"] = response
                    return response
                except Exception as error:
                    record["error"] = f"{type(error).__name__}: {error}"
                    raise
                finally:
                    record["seconds"] = round(time.monotonic()-started, 3)
                    record["usage_delta"] = {k: v-before.get(k, 0) for k, v in llm.usage.items()}
                    write(call_path, {"model": llm.client.config.model, "sdk_max_retries": llm.client.config.max_retries, "usage": dict(llm.usage), "calls": calls})
                    print(json.dumps({"case": case_id, "mode": mode, "call": len(calls), "seconds": record["seconds"], "error": record.get("error")}), flush=True)

            runner = ExcytinBenchmarkRunner(max_steps=args.max_steps, workflow_factory=lambda limit: ExcytinBenchWorkflow(
                max_steps=limit, llm=invoke,
                executor_format_attempts=args.executor_format_attempts, compact_evidence=args.compact_evidence))
            result = runner.run_case(cases[case_id], case_dir=out / mode, split=args.split)
            result.update(mode=mode, usage=dict(llm.usage), model=llm.client.config.model)
            write(out / mode / f"{case_id}.json", result)
            results.append(result)
            write(out / "results.json", results)
            print(json.dumps({k: result[k] for k in ("case_id", "mode", "status", "error", "query_count", "evaluation", "usage")}), flush=True)


if __name__ == "__main__":
    main()
