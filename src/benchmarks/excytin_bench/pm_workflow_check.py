"""Paired PM on/off diagnostics through the production workflow and real MySQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from src.benchmarks.excytin_bench.runner import ExcytinBenchmarkRunner, load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import (
    TrackedLLM,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import (
    SELECTION_PROMPT,
)
from src.workflow.context import MemoryContextService


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class LoggedLLM(TrackedLLM):
    def __init__(self, model, path):
        super().__init__(model)
        self.path = path
        self.calls = []

    def __call__(self, system, prompt):
        kind = (
            "selection"
            if system == SELECTION_PROMPT
            else "role"
        )
        started = time.monotonic()
        record = {
            "index": len(self.calls) + 1,
            "kind": kind,
            "system": system,
            "prompt": prompt,
        }
        self.calls.append(record)
        write(self.path, {"usage": dict(self.usage), "calls": self.calls})
        try:
            response = super().__call__(system, prompt)
            record["response"] = response
            return response
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            write(self.path, {"usage": dict(self.usage), "calls": self.calls})
            print(
                f"{self.path.stem} call={record['index']} {kind} {record['seconds']}s",
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="DeepSeek-V4.1-Flash")
    parser.add_argument(
        "--cases",
        nargs="+",
        default=["incident_39-q0088", "incident_55-q0022", "incident_5-q0001"],
    )
    parser.add_argument(
        "--modes", nargs="+", choices=["pm", "baseline"], default=["pm", "baseline"]
    )
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv(".env")
    load_dotenv("docker/neo4j/.env")
    load_dotenv("docker/excytin-mysql/.env")
    import os

    os.environ["SOCAGENT_LTM_BACKEND"] = "neo4j"
    cases = {c.case_id: c for c in load_test_cases()}
    if set(args.cases) - cases.keys():
        parser.error("Unknown case IDs")
    write(
        args.output / "manifest.json",
        {
            "model": args.model,
            "max_steps": args.max_steps,
            "cases": args.cases,
            "modes": args.modes,
            "pm_sha256": hashlib.sha256(
                DEFAULT_KNOWLEDGE_PATH.read_bytes()
            ).hexdigest(),
            "design": "Same production workflow, model, SM/EM, database and action budget; baseline disables PM selector only. Test cases chosen by question text, not answers. No memory curation during this run.",
        },
    )
    snapshot = json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())
    write(args.output / "procedural_memory_snapshot.json", snapshot)
    results = []
    for case_id in args.cases:
        for mode in args.modes:
            root = args.output / mode
            case_file = root / "cases" / f"{case_id}.json"
            if case_file.exists():
                saved = json.loads(case_file.read_text())
                if saved.get("status") == "completed":
                    results.append({"mode": mode, **saved})
                    continue
            llm = LoggedLLM(args.model, root / "calls" / f"{case_id}.json")
            service = MemoryContextService(
                procedure_selector=llm if mode == "pm" else None
            )
            runner = ExcytinBenchmarkRunner(
                max_steps=args.max_steps,
                workers=1,
                workflow_factory=lambda limit, llm=llm, service=service: (
                    ExcytinBenchWorkflow(
                        max_steps=limit, llm=llm, memory_contexts=service
                    )
                ),
            )
            result = runner.run_case(cases[case_id], case_dir=root / "cases")
            result["usage"] = dict(llm.usage)
            write(case_file, result)
            results.append({"mode": mode, **result})
            write(args.output / "results.json", results)
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "case": case_id,
                        "status": result["status"],
                        "error": result["error"],
                        "queries": result["query_count"],
                        "evaluation": result["evaluation"],
                        "usage": result["usage"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    write(args.output / "results.json", results)


if __name__ == "__main__":
    main()
