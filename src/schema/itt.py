"""G/E/T snapshots and role payloads; Planner mutations use planner_operations."""
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
    return {"id": node.node_id, "objective": node.title}


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def itt_payload(tree: TracebackTaskTree, executions=(), *, planner_view=False) -> list[dict[str, Any]]:
    def node(n):
        status = "done" if n.status in {S.DONE, S.RESOLVED} else "failed" if n.status in {S.BLOCKED, S.NOT_APPLICABLE} else "todo"
        out = {"id": n.node_id, "objective": n.title, "status": status}
        if n.level < 3:
            out["children"] = [node(c) for c in n.children]
            if n.level == 2 and n.metadata.get("entity"):
                out["entity"] = deepcopy(n.metadata["entity"])
        else:
            code, result, refs = execution_fields(executions, n.node_id)
            out["semantic_refs"] = deepcopy(n.metadata.get("semantic_refs", []))
            if n.metadata.get("query_template"):
                out["template_id"] = n.metadata["query_template"]["id"]
            out.update(execution_code=code if refs else n.metadata.get("execution_code", ""),
                       execution_result=n.metadata.get("execution_result", "") if n.metadata.get("execution_result_source") == "reviewer" else (result if refs else n.metadata.get("execution_result", "")))
        if n.level == 3:
            out["execution_result"] = deepcopy(out["execution_result"])
            if n.metadata.get("input_entity_ids"):
                out["input_entity_ids"] = list(n.metadata["input_entity_ids"])
        if planner_view:
            if n.level < 3:
                out.pop("status")
            else:
                out["status"] = "error" if status == "failed" else status
                out.pop("semantic_refs", None)
                out.pop("template_id", None)
                if n.metadata.get("procedure"):
                    out["procedure"] = deepcopy(n.metadata["procedure"])
        return out
    return [node(n) for n in tree.root_nodes]


def review_payload(tree: TracebackTaskTree, execution) -> dict[str, Any]:
    """Only the current task and execution; no full tree or global context."""
    fields = {"execution_id", "node_id", "sql", "observation", "query_success",
              "result_status", "observation_truncated", "original_observation_chars",
              "tool_name", "tool_input", "result", "execution_status", "error_message"}
    record = execution.to_dict() if hasattr(execution, "to_dict") else execution
    task = next((n for n, _ in walk(tree) if n.node_id == record["node_id"] and n.level == 3), None)
    if task is None:
        raise ValueError("Reviewer execution must reference an existing task")
    return {"task": task_payload(task),
            "executor_result": {k: v for k, v in record.items() if k in fields}}


def parse_itt(payload, event_id: str, round_id: int, *, previous=None, executions=(), procedures=(), allow_pending_entities=False) -> TracebackTaskTree:
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
    levels, parents = {}, {}
    eligible = []
    def read(value, depth, parent=None, active=True):
        if not isinstance(value, dict) or depth > 3:
            raise ValueError("ITT requires exactly three nested G/E/T levels")
        required = {"id", "objective", "children"} if depth < 3 else {"id", "objective", "status", "execution_code", "execution_result"}
        optional = {"template_id", "semantic_refs", "input_entity_ids"} if depth == 3 else ({"status", "entity"} if depth == 2 else {"status"})
        if set(value) - optional != required:
            raise ValueError(f"ITT level {depth} requires fields: {sorted(required)}")
        ident, title, status = value["id"], value["objective"], value.get("status", "todo")
        if status == "error":
            status = "failed"
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("ITT node IDs must be unique and nonempty")
        if not isinstance(title, str) or not title.strip() or status not in ("todo", "done", "failed"):
            raise ValueError("ITT requires a nonempty objective and todo/done/failed status")
        seen.add(ident)
        levels[ident], parents[ident] = depth, parent
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
            if not isinstance(children, list) or not children and not (depth == 2 and allow_pending_entities):
                raise ValueError("Every G/E needs at least one child")
            if depth == 2 and "entity" in value:
                entity = value["entity"]
                if not isinstance(entity, dict) or set(entity) - {"concept", "kind", "value", "source_refs"} or not isinstance(entity.get("concept"), str) or type(entity.get("value")) not in (str, int, float):
                    raise ValueError("Invalid typed E entity contract")
                if prior and prior[0].metadata.get("entity") not in (None, entity):
                    raise ValueError("Existing entity identity cannot change")
                metadata["entity"] = deepcopy(entity)
            children = tuple(read(c, depth + 1, ident, active) for c in children)
            if "status" not in value:
                # Parent states are runtime bookkeeping, absent from Planner output.
                if not children or any(c.status in {S.OPEN, S.TODO, S.IN_PROGRESS} for c in children):
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
            entity_ids = value.get("input_entity_ids", metadata.get("input_entity_ids", []))
            if entity_ids:
                if not isinstance(entity_ids, list) or any(not isinstance(x, str) for x in entity_ids) or len(entity_ids) != len(set(entity_ids)) or parent not in entity_ids:
                    raise ValueError("T input_entity_ids must be distinct E IDs including its parent")
                if prior and prior[0].metadata.get("input_entity_ids", []) != entity_ids:
                    raise ValueError("Existing T input entities cannot change")
                metadata["input_entity_ids"] = list(entity_ids)
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
    for task_id, level in levels.items():
        if level == 3:
            task = next(n for root in roots for entity in root.children for n in entity.children if n.node_id == task_id)
            for entity_id in task.metadata.get("input_entity_ids", []):
                if levels.get(entity_id) != 2 or parents[entity_id] != parents[parents[task_id]]:
                    raise ValueError("T inputs must reference existing E nodes of the same G")
    if set(old) - seen:
        raise ValueError("ITT updates cannot delete existing investigation paths")
    tree = TracebackTaskTree(event_id=event_id, round_id=round_id, root_nodes=roots,
                            version=previous.version + 1 if previous else 1,
                            next_task_id=eligible[0] if eligible else None)
    if previous:
        tree = replace(tree, created_at=previous.created_at)
    if not allow_pending_entities and next_task(tree) is None and any(n.status == S.OPEN for n in roots):
        raise ValueError("Open goals require an actionable task or a terminal status")
    return tree


def planner_prompt(system_prompt: str, memory: dict) -> str:
    if memory.get("enabled") is False:
        return system_prompt
    if "procedure_cards" in memory:
        cards = memory["procedure_cards"]
        if not cards:
            return system_prompt
        return system_prompt + "\n\n# 可参考的调查过程\n" + json.dumps(cards, ensure_ascii=False, separators=(",", ":")) + "\n执行环境是 MySQL，数据表从安全日志导入，不是 KQL/Advanced Hunting。每个当前查询的 objective 应明确写出 MySQL 只读查询。以下过程是方法，不是本案证据。purpose=scope_discovery 表示需要先核对题目对应的事件或告警及其实体；背景有前后多个事件时，不得把前一事件的 IP、账户或主机直接绑定为后一事件的参数，也不能仅凭同名进程回答。purpose=candidate_discovery 仅用于查找候选告警实体，不代表已覆盖问题所需字段；须根据真实字段继续规划，不得把显示名当 UPN。input_type_available 仅表示输入类型已出现，仍须核对实体与目标的对应关系。只为参数确已获得的步骤创建当前查询任务；needs_prior_observation 的步骤须等待真实结果。将当前步骤的精确表字段、必要条件和已有参数写入 T.objective；必须保留 DeviceProcessEvents 等原始表名，不能改写成“进程事件表”等抽象名称，使只收到 id/objective 的 Executor 可以执行。不要复制整个过程，不添加协议之外的字段，不根据记忆填写事实。表字段经过离线核对，当前环境若不支持则安排结构核对。最后只返回 operations YAML，不返回 SQL/KQL 或解释。"
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
