"""Real Fancybag + MySQL integration evaluation; preserve every failed attempt."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from src.agent.llm import LLMClient, LLMConfig
from src.benchmarks.excytin_bench.runner import ExcytinBenchmarkRunner, load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", default=["incident_134-q0003", "incident_39-q0008", "incident_322-q0006"])
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()
    load_dotenv(ROOT / "docker/excytin-mysql/.env")
    os.environ["SOCAGENT_LLM_PROVIDER"] = "fancybag"
    config = replace(LLMConfig.from_env(), max_retries=0)
    output = ROOT / "runtime/semantic_v2_fancybag" / datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    print(f"OUTPUT {output}", flush=True)
    save(output / "manifest.json", {
        "provider": "fancybag", "base_url": config.base_url, "model": config.model,
        "max_steps": args.max_steps, "cases": args.cases, "llm_eval": False,
        "selection": "fixed coverage examples selected from question/context only; not random or representative accuracy estimate",
        "sources_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT / "src/benchmarks/excytin_bench/workflow.py", ROOT / "src/agent/planner.py",
                      *sorted((ROOT / "src/memory/longterm_memory/semantic_memory/v2").glob("*.py")),
                      *sorted((ROOT / "src/memory/longterm_memory/semantic_memory/v2/excytin").glob("*.json"))]},
    })
    cases = {c.case_id: c for c in load_test_cases()}
    summaries = []
    for case_id in args.cases:
        case = cases[case_id]
        case_dir = output / case_id
        case_dir.mkdir()
        calls = []
        client = LLMClient(config)
        create = client.client.chat.completions.create
        def tracked_create(**kwargs):
            response = create(**kwargs)
            if response.usage:
                calls[-1]["usage"] = response.usage.model_dump()
            return response
        client.client.chat.completions.create = tracked_create
        def invoke(system, user):
            role = next((r for r, marker in [("planner", "系统中的 Planner"), ("executor", "你是 Executor"),
                         ("reviewer", "你是 Reviewer"), ("verifier", "你是 Verifier")] if marker in system), "unknown")
            call = {"index": len(calls) + 1, "role": role, "system": system, "input": user}
            calls.append(call)
            save(case_dir / "calls.json", calls)
            print(f"{case_id} CALL {call['index']} {role} started", flush=True)
            start = time.monotonic()
            try:
                call["output"] = client.chat(system_prompt=system, user_prompt=user)
                return call["output"]
            except Exception as exc:
                call["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                call["seconds"] = round(time.monotonic() - start, 3)
                save(case_dir / "calls.json", calls)
                print(f"{case_id} CALL {call['index']} {role} finished {call['seconds']}s", flush=True)
        try:
            runner = ExcytinBenchmarkRunner(max_steps=args.max_steps, workers=1, llm_eval=False,
                workflow_factory=lambda limit: ExcytinBenchWorkflow(max_steps=limit, llm=invoke))
            result = runner.run_case(case, case_dir=case_dir)
        finally:
            client.client.close()
        log = result["workflow"]
        summaries.append({**{k: result[k] for k in ("case_id", "question", "status", "error", "latency_seconds", "query_count", "submitted_answer", "evaluation")},
            "reference_answer": case.question.get("answer"),
            "llm_calls": len(calls), "usage": {key: sum(c.get("usage", {}).get(key, 0) or 0 for c in calls) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "retrieval_count": len(log.get("memory_retrievals", [])),
            "selections": log.get("task_semantic_contexts", {}),
            "executions": log.get("executions", []),
            "retry_events": [e for e in log.get("trace", []) if "invalid" in e["event_type"] or "retry" in e["event_type"]],
        })
        save(output / "summary.json", summaries)
        print(json.dumps({k: summaries[-1][k] for k in ("case_id", "status", "error", "query_count", "llm_calls", "retrieval_count", "submitted_answer")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
