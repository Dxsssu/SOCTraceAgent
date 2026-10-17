"""Replay identical investigation states against old/new protocols, without executing SQL.

Run with `uv run python -m scripts.check_planner_operations --case ... --requests ...`.
Inputs are a saved benchmark case and its LLM JSONL log. Only context/question,
the pre-planning ITT and review are sent to the model; evaluation labels are not.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time

import yaml

from src.agent.llm import LLMClient, LLMConfig
from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.schema.itt import itt_payload, compact_json, parse_itt, parse_itt_response
from src.schema.planner_operations import apply_planner_operations, parse_planner_response
from src.schema.ttt import TracebackTaskTree
from src.schema.ttt_updates import next_task, walk


def baseline_prompt(revision):
    source = subprocess.check_output(["git", "show", f"{revision}:src/agent/planner.py"], text=True)
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLANNER_SYSTEM_PROMPT" for t in statement.targets):
            value = statement.value
            return ast.literal_eval(value.func.value).strip() if isinstance(value, ast.Call) else ast.literal_eval(value)
    raise ValueError("Baseline Planner prompt not found")


def scenarios(case_path, requests_path):
    case = json.loads(case_path.read_text())
    records = [json.loads(line) for line in requests_path.read_text().splitlines() if line.strip()]
    planners = [r for r in records if r.get("role") == "planner" and "上一次输出未通过校验" not in r["user"]]
    initial = json.loads(planners[0]["user"].split("\n", 1)[1])
    initial = {key: initial[key] for key in ("context", "question")}
    yield "initial", initial, None, []
    last = next(r for r in reversed(planners) if r["user"].startswith("context:"))
    payload = yaml.safe_load(last["user"])
    payload = {key: payload[key] for key in ("context", "question", "itt", "review")}
    for snapshot in reversed(case["workflow"]["ttt_history"]):
        tree = TracebackTaskTree.from_dict(snapshot)
        if itt_payload(tree, planner_view=True) == payload["itt"]:
            evidence = {ref for node, depth in walk(tree) if depth == 3 for ref in node.evidence_refs}
            executions = [e for e in case["workflow"]["executions"] if e["execution_id"] in evidence]
            yield "replan", payload, tree, executions
            return
    raise ValueError("No saved snapshot matches the pre-replanning model input")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--baseline", default="47f0d29")
    parser.add_argument("--output", type=Path, default=Path("runtime/planner_operations_check"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    inputs = list(scenarios(args.case, args.requests))
    args.output.mkdir(parents=True, exist_ok=True)
    if args.live:
        (args.output / "requests.jsonl").write_text("")
    report = {"baseline": args.baseline, "source_case": str(args.case),
              "scope": "paired Planner protocol smoke test; no SQL execution or answer-quality evaluation",
              "results": []}
    # Compare serializations for exactly the same deterministic state update.
    # These are character counts, not estimated API tokens or model outcomes.
    _, _, prior, records = inputs[-1]
    operation = {"operations": [{"op": "add_task", "entity_id": prior.root_nodes[0].children[0].node_id,
                                  "objective": "检查当前数据库的实际表结构，选择下一步证据来源"}]}
    updated = apply_planner_operations(operation, prior.event_id, prior.round_id + 1,
                                       previous=prior, executions=records)
    full_output = itt_payload(updated, records, planner_view=True)
    equivalent = parse_itt(full_output, prior.event_id, prior.round_id + 1,
                           previous=prior, executions=records)
    assert equivalent.root_nodes == updated.root_nodes and equivalent.next_task_id == updated.next_task_id
    full_chars = len(yaml.safe_dump(full_output, allow_unicode=True, sort_keys=False))
    operation_chars = len(yaml.safe_dump(operation, allow_unicode=True, sort_keys=False))
    report["equivalent_update"] = {
        "identical_tree_and_next_task": True, "existing_tasks": sum(d == 3 for _, d in walk(prior)),
        "full_tree_output_characters": full_chars, "operations_output_characters": operation_chars,
        "character_reduction_fraction": 1 - operation_chars / full_chars,
    }
    old_prompt = baseline_prompt(args.baseline)
    if args.live:
        config = replace(LLMConfig.from_env(), request_timeout_seconds=args.timeout, max_retries=0)
        client = LLMClient(config)
        report["model"] = config.model
    for stage, payload, previous, executions in inputs:
        for protocol, system in (("full_tree", old_prompt), ("operations", PLANNER_SYSTEM_PROMPT)):
            if previous is None:
                instruction = "下面是此次事件的背景和调查问题，请你根据要求初始化并输出ITT。" if protocol == "full_tree" else "下面是此次事件的背景和调查问题，请输出初始化调查所需的 operations。"
                user = instruction + "\n" + (compact_json(payload) if protocol == "operations" else json.dumps(payload, ensure_ascii=False, indent=2))
            elif protocol == "operations":
                user = yaml.safe_dump({"context": payload["context"], "question": payload["question"],
                                     "itt": itt_payload(previous, executions, planner_view=True), "review": {"can_answer": "no"}},
                                      allow_unicode=True, sort_keys=False)
            else:
                user = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
            entry = {"stage": stage, "protocol": protocol, "input_characters": len(system) + len(user), "attempts": []}
            report["results"].append(entry)
            if not args.live:
                continue
            prompt = user
            for attempt in (1, 2):
                print(f"{stage}/{protocol} attempt {attempt}", flush=True)
                started = time.monotonic()
                record = {"stage": stage, "protocol": protocol, "attempt": attempt,
                          "system": system, "user": prompt}
                try:
                    response = client.client.chat.completions.create(
                        model=config.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        reasoning_effort=config.reasoning_effort,
                        extra_body={"thinking": {"type": "enabled" if config.thinking_enabled else "disabled"}},
                        max_tokens=4096,
                    )
                    content = response.choices[0].message.content or ""
                    record.update(response=content, output_characters=len(content),
                                  usage=response.usage.model_dump() if response.usage else None)
                    validate = apply_planner_operations if protocol == "operations" else parse_itt
                    parse = parse_planner_response if protocol == "operations" else parse_itt_response
                    tree = validate(parse(content), previous.event_id if previous else "protocol-check",
                                    previous.round_id + 1 if previous else 1, previous=previous, executions=executions)
                    if next_task(tree) is None:
                        raise ValueError("Planner must leave an actionable task")
                    record["valid"] = True
                except Exception as exc:
                    record.update(valid=False, error=f"{type(exc).__name__}: {exc}")
                record["latency_seconds"] = round(time.monotonic() - started, 3)
                with (args.output / "requests.jsonl").open("a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                entry["attempts"].append({k: v for k, v in record.items() if k not in {"system", "user", "response"}})
                (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps(entry["attempts"][-1], ensure_ascii=False), flush=True)
                if record["valid"] or "response" not in record:
                    break
                prompt = user + "\n上一次输出未通过校验。请根据错误修正，不要使用额外说明。\n解析错误：" + record["error"]
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
