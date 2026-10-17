"""Planner's G/E/T YAML contract, adapted to the persisted task-tree model."""
from copy import deepcopy
from dataclasses import replace
import json
from typing import Any

import yaml

from .ttt import TracebackTaskTree, TTTNode, TTTNodeStatus as S
from .ttt_updates import next_task, walk


def parse_itt_response(text: str) -> list[dict[str, Any]]:
    value = yaml.safe_load(text)
    if not isinstance(value, list) or not value:
        raise ValueError("ITT output must be a nonempty YAML list")
    return value


def execution_fields(executions, node_id):
    records = [e.to_dict() if hasattr(e, "to_dict") else e for e in executions]
    records = [e for e in records if e.get("node_id") == node_id]
    codes, results = [], []
    for e in records:
        codes.append(e.get("sql") or json.dumps({"tool_name": e.get("tool_name"), "tool_input": e.get("tool_input")}, ensure_ascii=False))
        results.append(e.get("observation") if "observation" in e else json.dumps({"result": e.get("result"), "error_message": e.get("error_message", "")}, ensure_ascii=False))
    def pack(values):
        return values[0] if len(values) == 1 else json.dumps(values, ensure_ascii=False) if values else ""
    return pack(codes), pack(results), tuple(e["execution_id"] for e in records)


def task_payload(node: TTTNode) -> dict[str, Any]:
    """The complete, task-only input boundary for Executor."""
    status = "done" if node.status == S.DONE else "failed" if node.status in {S.BLOCKED, S.NOT_APPLICABLE} else "todo"
    return {
        "id": node.node_id,
        "objective": node.title,
        "status": status,
        "semantic_refs": deepcopy(node.metadata.get("semantic_refs", [])),
        "execution_code": node.metadata.get("execution_code", ""),
        "execution_result": node.metadata.get("execution_result", ""),
        **({"query_template": deepcopy(node.metadata["query_template"])} if node.metadata.get("query_template") else {}),
    }


def itt_payload(tree: TracebackTaskTree, executions=(), *, planner_view=False) -> list[dict[str, Any]]:
    def node(n):
        status = "done" if n.status in {S.DONE, S.RESOLVED} else "failed" if n.status in {S.BLOCKED, S.NOT_APPLICABLE} else "todo"
        out = {"id": n.node_id, "objective": n.title, "status": status}
        if n.level < 3:
            out["children"] = [node(c) for c in n.children]
        else:
            code, result, refs = execution_fields(executions, n.node_id)
            out["semantic_refs"] = deepcopy(n.metadata.get("semantic_refs", []))
            if n.metadata.get("query_template"):
                out["template_id"] = n.metadata["query_template"]["id"]
            out.update(execution_code=code if refs else n.metadata.get("execution_code", ""),
                       execution_result=n.metadata.get("execution_result", "") if n.metadata.get("execution_result_source") == "reviewer" else (result if refs else n.metadata.get("execution_result", "")))
        if planner_view:
            if n.level < 3:
                out.pop("status")
            else:
                out["status"] = "error" if status == "failed" else status
                out.pop("semantic_refs", None)
                out.pop("template_id", None)
        return out
    return [node(n) for n in tree.root_nodes]


def review_payload(tree: TracebackTaskTree, execution) -> dict[str, Any]:
    """Provide the task outline and only the current Executor result."""
    def outline(n):
        value = {"id": n.node_id, "objective": n.title,
                 "status": "done" if n.status in {S.DONE, S.RESOLVED} else "failed" if n.status in {S.BLOCKED, S.NOT_APPLICABLE} else "todo"}
        if n.children:
            value["children"] = [outline(c) for c in n.children]
        return value
    fields = {"execution_id", "node_id", "sql", "observation", "query_success",
              "result_status", "observation_truncated", "original_observation_chars",
              "tool_name", "tool_input", "result", "execution_status", "error_message"}
    record = execution.to_dict() if hasattr(execution, "to_dict") else execution
    return {"itt": [outline(n) for n in tree.root_nodes],
            "executor_result": {k: v for k, v in record.items() if k in fields}}


def parse_itt(payload, event_id: str, round_id: int, *, previous=None, executions=(), procedures=()) -> TracebackTaskTree:
    if not isinstance(payload, list) or not payload:
        raise ValueError("ITT output must be a nonempty YAML list")
    old = {}
    def index(nodes, parent=None):
        for n in nodes:
            old[n.node_id] = (n, parent)
            index(n.children, n.node_id)
    if previous:
        index(previous.root_nodes)
    templates = {p["id"]: p for p in procedures if p.get("sql_template") and p.get("id")}
    for prior_node, _ in old.values():
        template = prior_node.metadata.get("query_template")
        if template:
            templates.setdefault(template["id"], template)
    seen = set()
    eligible = []
    def read(value, depth, parent=None, active=True):
        if not isinstance(value, dict) or depth > 3:
            raise ValueError("ITT requires exactly three nested G/E/T levels")
        required = {"id", "objective", "children"} if depth < 3 else {"id", "objective", "status", "execution_code", "execution_result"}
        if set(value) - ({"template_id", "semantic_refs"} if depth == 3 else {"status"}) != required:
            raise ValueError(f"ITT level {depth} requires fields: {sorted(required)}")
        ident, title, status = value["id"], value["objective"], value.get("status", "todo")
        if status == "error":
            status = "failed"
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("ITT node IDs must be unique and nonempty")
        if not isinstance(title, str) or not title.strip() or status not in ("todo", "done", "failed"):
            raise ValueError("ITT requires a nonempty objective and todo/done/failed status")
        seen.add(ident)
        prior = old.get(ident)
        if prior and (prior[0].title != title or prior[1] != parent):
            raise ValueError("Existing ITT node identity/objective/parent must be preserved")
        if prior and prior[0].status in {S.DONE, S.RESOLVED, S.BLOCKED, S.NOT_APPLICABLE} and status == "todo" and (depth == 3 or "status" in value):
            raise ValueError("Preserve terminal paths; create a new task to investigate further")
        if not previous and status != "todo":
            raise ValueError("Initial ITT nodes must be todo")
        active = active and status == "todo"
        metadata = dict(prior[0].metadata) if prior else {}
        refs = prior[0].evidence_refs if prior else ()
        if depth < 3:
            children = value["children"]
            if not isinstance(children, list) or not children:
                raise ValueError("Every G/E needs at least one child")
            children = tuple(read(c, depth + 1, ident, active) for c in children)
            if "status" not in value:
                # Parent states are runtime bookkeeping, absent from Planner output.
                if any(c.status in {S.OPEN, S.TODO, S.IN_PROGRESS} for c in children):
                    status = "todo"
                elif all(c.status in {S.DONE, S.RESOLVED} for c in children):
                    status = "done"
                else:
                    status = "failed"
            mapped = {"todo": S.OPEN, "done": S.RESOLVED, "failed": S.BLOCKED}[status]
            refs = tuple(dict.fromkeys(r for c in children for r in c.evidence_refs))
            if status == "done" and not refs:
                raise ValueError("Completed goals/frontiers require executed evidence")
        else:
            children = ()
            template_id = value.get("template_id", (metadata.get("query_template") or {}).get("id"))
            if template_id is not None and (not isinstance(template_id, str) or template_id not in templates):
                raise ValueError("template_id must reference an available query template")
            template = templates.get(template_id)
            if prior and prior[0].status in {S.DONE, S.BLOCKED, S.NOT_APPLICABLE}:
                template = prior[0].metadata.get("query_template")
            if template:
                metadata["query_template"] = {k: deepcopy(template[k]) for k in ("id", "inputs", "sql_template", "returns", "semantic_refs")}
            else:
                metadata.pop("query_template", None)
            semantic_refs = value.get("semantic_refs", metadata.get("semantic_refs", []))
            if not isinstance(semantic_refs, list):
                raise ValueError("semantic_refs must be a list of table/fields objects")
            tables = set()
            for ref in semantic_refs:
                if not isinstance(ref, dict) or set(ref) != {"table", "fields"}:
                    raise ValueError("semantic_refs entries require table and fields")
                table, fields = ref["table"], ref["fields"]
                if not isinstance(table, str) or not table.strip() or table in tables:
                    raise ValueError("semantic_refs table names must be nonempty and unique")
                if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or not f.strip() for f in fields):
                    raise ValueError("semantic_refs fields must be a nonempty list of field names")
                if len(fields) != len(set(fields)):
                    raise ValueError("semantic_refs fields must be unique")
                tables.add(table)
            if prior and prior[0].status in {S.DONE, S.BLOCKED, S.NOT_APPLICABLE} and semantic_refs != prior[0].metadata.get("semantic_refs", []):
                raise ValueError("Preserve semantic_refs on terminal tasks")
            metadata["semantic_refs"] = deepcopy(semantic_refs)
            if template and not (prior and prior[0].status in {S.DONE, S.BLOCKED, S.NOT_APPLICABLE}):
                metadata["semantic_refs"] = deepcopy(template["semantic_refs"])
            code, result, evidence = execution_fields(executions, ident)
            if not evidence:
                code, result = metadata.get("execution_code", ""), metadata.get("execution_result", "")
            if metadata.get("execution_result_source") == "reviewer":
                result = metadata.get("execution_result", "")
            if value["execution_code"] != code or value["execution_result"] != result:
                raise ValueError("Execution code/result must exactly preserve the supplied actual records")
            if status == "done" and not (evidence or refs):
                raise ValueError("Unexecuted tasks cannot be done")
            metadata.update(execution_code=code, execution_result=result)
            refs = tuple(dict.fromkeys((*refs, *evidence)))
            mapped = {"todo": S.TODO, "done": S.DONE, "failed": S.BLOCKED}[status]
            if active:
                eligible.append(ident)
        return TTTNode(node_id=ident, title=title, level=depth, status=mapped, children=children,
                       metadata=metadata, evidence_refs=refs,
                       result_summary=prior[0].result_summary if prior else "")
    roots = tuple(read(v, 1) for v in payload)
    if set(old) - seen:
        raise ValueError("ITT updates cannot delete existing investigation paths")
    tree = TracebackTaskTree(event_id=event_id, round_id=round_id, root_nodes=roots,
                            version=previous.version + 1 if previous else 1,
                            next_task_id=eligible[0] if eligible else None)
    if previous:
        tree = replace(tree, created_at=previous.created_at)
    if next_task(tree) is None and any(n.status == S.OPEN for n in roots):
        raise ValueError("Open goals require an actionable task or a terminal status")
    return tree


def planner_prompt(system_prompt: str, memory: dict) -> str:
    if memory.get("enabled") is False:
        return system_prompt
    procedures = memory.get("procedural_memory", {}).get("procedures", [])
    section = json.dumps([{k: p[k] for k in ("id", "name", "purpose", "inputs", "sql_template", "returns") if k in p} for p in procedures[:1]], ensure_ascii=False, separators=(",", ":")) if procedures else ""
    prompt = system_prompt + "\n\n# 适用的 Procedural Memory\n" + section
    application = memory.get("procedural_memory", {}).get("application")
    if procedures and application:
        prompt += "\n\n# 本次 PM 的使用边界（规划建议，不是已执行证据）\n" + json.dumps({k: application[k] for k in ("target_question", "next_step", "expected_observation", "limitations") if k in application}, ensure_ascii=False)
    cards = memory.get("semantic_memory", {}).get("table_cards", [])
    tables = memory.get("semantic_memory", {}).get("tables", [])
    if not procedures and cards:
        prompt += "\n\n# Semantic Memory L2（各场景 schema 并集；type 为 MySQL 类型，实际可用性以当前数据库为准；非案件证据）\n" + json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
    elif not procedures and tables:
        prompt += "\n\n# Semantic Memory（真实表字段，非案件证据）\n" + json.dumps(tables, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(line for line in prompt.splitlines() if line.strip())
