"""Summarize real PM workflow diagnostics without feeding answers to agents."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import exp

from src.memory.longterm_memory.procedural_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.legacy_retrieval import (
    parse_response,
    validate_application,
)


def summarize(root: Path):
    snapshot = root / "procedural_memory_snapshot.json"
    memory = json.loads(
        (snapshot if snapshot.exists() else DEFAULT_KNOWLEDGE_PATH).read_text()
    )
    entries = {p.get("id", p["name"]): p for p in memory["procedures"]}
    rows = []
    for path in sorted(root.glob("*/cases/*.json")):
        result = json.loads(path.read_text())
        if "evaluation" not in result:
            continue
        mode = path.parent.parent.name
        calls = json.loads((root / mode / "calls" / path.name).read_text())
        attempts = []
        for call in calls["calls"]:
            if call["kind"] != "selection":
                continue
            payload = json.loads(call["prompt"])
            attempt = {"role": payload.get("role", "planner"), "selected_names": [], "check": None}
            if memory.get("format_version") == "5.0":
                try:
                    choices = parse_response(call.get("response", ""))
                    ids = [x.get("id") if isinstance(x, dict) else x for x in choices]
                    valid = isinstance(choices, list) and len(ids) <= 1 and all(isinstance(x, str) and x in entries for x in ids)
                    attempt.update(selected_ids=ids, valid_selection=valid, selected_names=[entries[x]["name"] for x in ids] if valid else [])
                except (ValueError, TypeError, AttributeError):
                    attempt["parse_error"] = True
                attempts.append(attempt)
                continue
            try:
                attempt["selected_names"] = parse_response(call.get("response", ""))
                # A bounded format repair can repeat a stage. Inspect its last response.
                stages = []
                for candidate_call in calls["calls"][call["index"]:]:
                    if candidate_call["kind"] in {"selection", "role"}:
                        break
                    stages.append(candidate_call)
                prerequisites = [c for c in stages if c["kind"] == "prerequisites"]
                following = prerequisites[-1] if prerequisites else None
                if following and following["kind"] == "prerequisites":
                    check = parse_response(following.get("response", ""))
                    context = json.loads(following["prompt"])
                    attempt["check"] = check
                    if "candidates" in context:
                        candidates = {p["name"]: p for p in context["candidates"]}
                        assessments = check.get("assessments", [])
                        names = [a.get("name") for a in assessments if isinstance(a, dict)]
                        valid_names = (len(names) == len(assessments)
                            and all(isinstance(n, str) for n in names)
                            and len(set(names)) == len(names) and set(names) == set(candidates))
                        accepted = [a for a in assessments if valid_names and validate_application(
                            a, candidates[a["name"]], context["task"], context["sources"]) is None]
                        attempt["suitability_passed"] = bool(accepted)
                        guards = [c for c in stages if c["kind"] == "guard"]
                        guard_call = guards[-1] if guards else None
                        if guard_call and guard_call["kind"] == "guard":
                            guard = parse_response(guard_call.get("response", ""))
                            attempt["guard"] = guard
                            selected = guard.get("selected") if isinstance(guard, dict) else None
                            valid_guard = (isinstance(guard, dict) and set(guard) == {"selected", "reason"}
                                and isinstance(guard["reason"], str) and bool(guard["reason"].strip()))
                            accepted = [a for a in accepted if valid_guard and a["name"] == selected]
                        attempt["gate_passed"] = bool(accepted)
                        attempt["accepted_names"] = [a["name"] for a in accepted[:1]]
                        attempt["task_context"] = context["task"]
                        attempt["quoted_evidence_exists"] = bool(accepted)
                    else:  # Historical diagnostics keep the original protocol.
                        bindings = check.get("bindings", {})
                        attempt["gate_passed"] = (
                            check.get("missing") == []
                            and isinstance(bindings, dict)
                            and set(bindings) == set(context["procedure"]["input_slots"])
                            and all(
                                isinstance(v, str)
                                and bool(v.strip())
                                and v in context["task"]
                                for v in bindings.values()
                            )
                        )
                        attempt["task_context"] = context["task"]
                        attempt["quoted_evidence_exists"] = all(
                            isinstance(v, str) and bool(v.strip()) and v in context["task"]
                            for v in check.get("bindings", {}).values()
                        )
            except (ValueError, TypeError, AttributeError):
                attempt["parse_error"] = True
            attempts.append(attempt)
        retrievals = result.get("workflow", {}).get("memory_retrievals", [])
        executor_retrievals = [r for r in retrievals if r["role"] == "executor"]
        queries = [a for a in result.get("process", []) if a["action_type"] == "query"]
        sql_evidence = []
        for index, query in enumerate(queries):
            names = (
                executor_retrievals[index]["procedure_names"]
                if index < len(executor_retrievals)
                else []
            )
            tables = set()
            try:
                tree = sqlglot.parse_one(query["action"], read="mysql")
                tables = {t.name for t in tree.find_all(exp.Table)}
            except sqlglot.errors.ParseError:
                pass
            refs = {r["table"] for n in names for r in entries[n]["semantic_refs"]}
            sql_evidence.append(
                {
                    "step_no": query["step_no"],
                    "procedures": names,
                    "operations": [entries[n].get("sql_template", entries[n].get("operations")) for n in names],
                    "sql": query["action"],
                    "query_success": query.get("query_success"),
                    "row_count": query.get("row_count"),
                    "referenced_table_overlap": sorted(tables & refs),
                }
            )
        usage = calls["usage"]
        rows.append(
            {
                "case_id": result["case_id"],
                "mode": mode,
                "status": result["status"],
                "error": result["error"],
                "seconds": result["latency_seconds"],
                "queries": len(queries),
                "sql_errors": sum(q.get("query_success") is False for q in queries),
                "empty_results": sum(
                    q.get("query_success") is True and q.get("row_count") == 0
                    for q in queries
                ),
                "evaluation": result["evaluation"],
                "usage": usage,
                "estimated_cost_cny": (
                    usage.get("input_tokens", 0) * 2 + usage.get("output_tokens", 0) * 8
                )
                / 1e6,
                "pm_selections": attempts,
                "pm_injections_by_role": dict(
                    Counter(r["role"] for r in retrievals if r.get("procedure_names"))
                ),
                "retrievals_by_role": dict(Counter(r["role"] for r in retrievals)),
                "sql_evidence": sql_evidence,
                "executor_log_alignment": len(executor_retrievals) == len(queries),
            }
        )
    report = {
        "design": "Real ExcytinBenchWorkflow Planner/Executor/Reviewer with real MySQL, Neo4j SM/EM/PM, same model and budget; baseline disables PM selection and loading.",
        "limitations": [
            "Three purposefully selected diagnostic questions, one run per mode; not a statistical estimate of accuracy improvement.",
            "Memory injection and SQL table overlap show exposure/consistency, not causal reliance.",
            "Deterministic answer matching only, no paper step reward.",
            "No test outcomes incorporated into procedural memory.",
        ],
        "rows": rows,
    }
    totals = {}
    for mode in ["pm", "baseline"]:
        subset = [r for r in rows if r["mode"] == mode]
        totals[mode] = {
            key: sum(r[key] for r in subset)
            for key in [
                "queries",
                "sql_errors",
                "empty_results",
                "seconds",
                "estimated_cost_cny",
            ]
        }
        totals[mode].update(
            cases=len(subset),
            completed=sum(r["status"] == "completed" for r in subset),
            exact=sum(r["evaluation"]["exact_match"] for r in subset),
            containment=sum(r["evaluation"]["containment_match"] for r in subset),
            calls=sum(r["usage"].get("calls", 0) for r in subset),
            selection_requests=sum(len(r["pm_selections"]) for r in subset),
            prerequisite_checks=sum(
                a["check"] is not None for r in subset for a in r["pm_selections"]
            ),
            prerequisite_passed=sum(
                a.get("gate_passed", False) for r in subset for a in r["pm_selections"]
            ),
            input_tokens=sum(r["usage"].get("input_tokens", 0) for r in subset),
            output_tokens=sum(r["usage"].get("output_tokens", 0) for r in subset),
        )
    report["totals"] = totals
    (root / "analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.output)["totals"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
