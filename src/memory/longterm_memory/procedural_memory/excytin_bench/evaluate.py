"""Test header retrieval and partial-step SQL reuse on synthetic, isolated data."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import sqlglot
from sqlglot import exp

from src.agent.llm import LLMClient, LLMConfig, parse_yaml_response
from src.benchmarks.excytin_bench.workflow import _EXECUTOR_PROMPT

from . import DEFAULT_KNOWLEDGE_PATH
from .import_neo4j import load_knowledge
from .retrieval import retrieve

CASES_PATH = Path(__file__).with_name("evaluation_cases.json")


def check_sql(case, sql):
    """Execute only a single SELECT against generated rows, never a real incident DB."""
    statements = sqlglot.parse(sql, read="mysql")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return {"passed": False, "reason": "not_single_select"}
    tree = statements[0]
    if {t.name for t in tree.find_all(exp.Table)} != {case["expected_table"]}:
        return {"passed": False, "reason": "repeated_or_wrong_table"}
    where = tree.args.get("where")
    literals = [str(x.this) for x in where.find_all(exp.Literal)] if where else []
    if not any(case["required_binding"] in x for x in literals):
        return {"passed": False, "reason": "missing_current_binding"}
    with sqlite3.connect(":memory:") as db:
        for t in case["tables"]:
            cols = ", ".join('"' + c + '" TEXT' for c in t["columns"])
            db.execute(f'CREATE TABLE "{t["name"]}" ({cols})')
            db.executemany(
                f'INSERT INTO "{t["name"]}" VALUES ({",".join("?" for _ in t["columns"])})',
                t["rows"],
            )
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(tree.sql(dialect="sqlite")).fetchmany(100)
    # Each fixture has one matching row and an irrelevant distractor row.
    passed = len(rows) == 1 and case["expected_value"] in [str(x) for x in rows[0]]
    return {
        "passed": passed,
        "reason": "matched_expected_row" if passed else "wrong_or_extra_rows",
        "rows": rows,
    }


def evaluate_case(case, knowledge, llm, *, use_memory=True):
    selected = (
        retrieve(knowledge, case["task"], selector=llm, role="executor", limit=1)
        if use_memory
        else []
    )
    names = [p["id"] for p in selected]
    allowed = set(case["allowed_ids"])
    selection_ok = bool(set(names) & allowed) if allowed else not names
    result = {
        "case_id": case["case_id"],
        "selected_names": names,
        "selection_passed": selection_ok,
        "selection_precision": len(set(names) & allowed) / len(names)
        if names
        else (1.0 if not allowed else 0.0),
    }
    if not case.get("tables"):
        result["passed"] = selection_ok
        return result
    from .templates import render_sql
    task = case["task"]
    payload = {"id": "T1", "objective": task["context"] + " " + task["question"],
               "status": "todo", "execution_code": "", "execution_result": "",
               "semantic_refs": [{"table": t["name"], "fields": t["columns"]} for t in case["tables"]]}
    if selected:
        payload["query_template"] = {k:selected[0][k] for k in ('id','inputs','sql_template','returns','semantic_refs')}
    prompt = json.dumps(payload, ensure_ascii=False)
    response = llm(_EXECUTOR_PROMPT, prompt)
    result["response"] = response
    try:
        parsed = parse_yaml_response(response) or {}
        sql = render_sql(selected[0], parsed.get("bindings")) if selected else str(parsed.get("sql", ""))
        result["sql"] = sql
        result["execution"] = check_sql(case, sql)
    except (ValueError, sqlite3.Error, sqlglot.errors.SqlglotError) as exc:
        result["execution"] = {
            "passed": False,
            "reason": type(exc).__name__ + ": " + str(exc),
        }
    result["passed"] = result["execution"]["passed"] and (
        selection_ok or not use_memory
    )
    return result


class TrackedLLM:
    def __init__(self, model):
        config = LLMConfig.from_env()
        self.client = LLMClient(
            replace(config, model=model or config.model, request_timeout_seconds=90)
        )
        self.usage = Counter()
        original = self.client.client.chat.completions.create

        def track(**kwargs):
            response = original(**kwargs)
            self.usage["calls"] += 1
            if response.usage:
                self.usage["input_tokens"] += response.usage.prompt_tokens
                self.usage["output_tokens"] += response.usage.completion_tokens
            return response

        self.client.client.chat.completions.create = track

    def __call__(self, system, prompt):
        return self.client.chat(system_prompt=system, user_prompt=prompt)


def run(knowledge, cases, llm, output, *, baseline=False):
    results = []
    start = time.monotonic()

    def persist():
        execution = [r for r in results if "execution" in r and r["mode"] == "memory"]
        memory = [r for r in results if r["mode"] == "memory"]
        base = [r for r in results if r["mode"] == "baseline"]
        report = {
            "suite": "synthetic_partial_step_reuse",
            "is_benchmark_reward": False,
            "catalog_sha256": hashlib.sha256(
                json.dumps(knowledge, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "model": getattr(getattr(llm, "client", None), "config", None).model
            if hasattr(llm, "client")
            else "injected",
            "summary": {
                "memory_cases": len(memory),
                "passed": sum(r["passed"] for r in memory),
                "selection_passed": sum(
                    r.get("selection_passed", False) for r in memory
                ),
                "execution_cases": len(execution),
                "execution_passed": sum(r["execution"]["passed"] for r in execution),
                "baseline_cases": len(base),
                "baseline_execution_passed": sum(r["passed"] for r in base),
            },
            "usage": dict(getattr(llm, "usage", {})),
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "results": results,
            "review_queue": [
                {
                    "case_id": r["case_id"],
                    "selected_names": r.get("selected_names", []),
                    "failure": r.get("error")
                    or (
                        "selection_mismatch"
                        if not r.get("selection_passed")
                        else r.get("execution", {}).get("reason", "execution_failed")
                    ),
                }
                for r in memory
                if not r["passed"]
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    for mode in ["memory", "baseline"] if baseline else ["memory"]:
        for case in cases:
            if mode == "baseline" and not case.get("tables"):
                continue
            try:
                r = evaluate_case(case, knowledge, llm, use_memory=mode == "memory")
            except Exception as exc:  # noqa: BLE001 -- persist each failed provider call and continue the suite
                r = {
                    "case_id": case["case_id"],
                    "passed": False,
                    "error": type(exc).__name__ + ": " + str(exc),
                }
            r["mode"] = mode
            results.append(r)
            persist()
            print(
                f"{mode} {case['case_id']}: {'PASS' if r['passed'] else 'FAIL'}",
                flush=True,
            )
    return persist()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    p.add_argument("--model")
    p.add_argument(
        "--output", type=Path, default=Path("runtime/procedural_memory/evaluation.json")
    )
    p.add_argument("--cases", nargs="*")
    p.add_argument("--baseline", action="store_true")
    a = p.parse_args()
    cases = json.loads(CASES_PATH.read_text())
    if a.cases:
        unknown = set(a.cases) - {c["case_id"] for c in cases}
        if unknown:
            raise ValueError(f"Unknown cases: {sorted(unknown)}")
        cases = [c for c in cases if c["case_id"] in a.cases]
    if not cases:
        raise ValueError("No evaluation cases selected")
    report = run(
        load_knowledge(a.knowledge),
        cases,
        TrackedLLM(a.model),
        a.output,
        baseline=a.baseline,
    )
    print(
        json.dumps(
            {"summary": report["summary"], "usage": report["usage"]}, ensure_ascii=False
        )
    )
    if any(not r["passed"] for r in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
