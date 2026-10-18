"""Atomic Planner operations; the runtime owns IDs and execution history.

The full-tree parser remains the internal snapshot validator. Model responses
must use this operation protocol and cannot supply a replacement tree.
"""
from copy import deepcopy
from dataclasses import replace
import re
from typing import Any

import yaml

from .itt import itt_payload, parse_itt
from .ttt import TracebackTaskTree
from .ttt_updates import next_task


MAX_OPERATIONS = 64
_FIELDS = {
    "add_goal": ({"op", "ref", "objective"}, set()),
    "add_entity": ({"op", "ref", "goal_id", "objective"}, set()),
    "add_task": ({"op", "entity_id", "objective"}, {"ref", "semantic_refs", "template_id"}),
    "select_task": ({"op", "task_id"}, set()),
}


def validate_operations(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {"operations"}:
        raise ValueError("Planner must output a YAML object containing only operations, not a full ITT")
    operations = payload["operations"]
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise ValueError(f"operations must be a list of at most {MAX_OPERATIONS} operations")
    for i, operation in enumerate(operations):
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str) or operation["op"] not in _FIELDS:
            raise ValueError(f"operations[{i}]: unsupported op; use {', '.join(_FIELDS)}")
        required, optional = _FIELDS[operation["op"]]
        if not required <= set(operation) or set(operation) - required - optional:
            missing = sorted(required - set(operation))
            unexpected = sorted(set(operation) - required - optional)
            raise ValueError(f"operations[{i}] {operation['op']}: missing fields {missing}; unexpected fields {unexpected}; "
                             f"required fields {sorted(required)}; optional {sorted(optional)}. "
                             "Use entity_id for add_task, goal_id for add_entity, task_id for select_task; "
                             "the runtime assigns new IDs. Do not output id, parent or execution_code.")
        for key in ("objective", "goal_id", "entity_id", "task_id", "template_id"):
            if key in operation and (not isinstance(operation[key], str) or not operation[key].strip()):
                raise ValueError(f"operations[{i}].{key} must be a nonempty string")
        if "ref" in operation and (not isinstance(operation["ref"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", operation["ref"])):
            raise ValueError(f"operations[{i}].ref must be a lowercase alias such as goal or process")
    return operations


def parse_planner_response(text: str) -> dict[str, Any]:
    # Accept a single whole-message transport fence, without extracting a
    # convenient YAML fragment from surrounding prose or multiple candidates.
    fenced = re.fullmatch(r"\s*```(?:yaml|yml)?[ \t]*\r?\n(.*?)\r?\n```\s*", text,
                          flags=re.IGNORECASE | re.DOTALL)
    if fenced and "```" not in fenced.group(1):
        text = fenced.group(1)
    payload = yaml.safe_load(text)
    validate_operations(payload)
    return payload


def apply_planner_operations(
    payload: dict[str, Any], event_id: str, round_id: int, *,
    previous: TracebackTaskTree | None = None, executions=(), procedures=(),
    require_next_task: bool = False,
) -> TracebackTaskTree:
    """Validate and apply a complete batch without mutating its input snapshot.

    Aliases are scoped to this response and referenced as $alias. Existing nodes
    use their stable IDs. No operation can edit, delete, move, or reopen an old T.
    """
    operations = validate_operations(payload)
    if previous is not None and previous.event_id != event_id:
        raise ValueError("ITT event mismatch")
    roots = deepcopy(itt_payload(previous, executions)) if previous else []
    nodes: dict[str, tuple[dict[str, Any], int]] = {}

    def index(values, level=1):
        for value in values:
            nodes[value["id"]] = (value, level)
            if level < 3:
                # Ancestor state is derived by the runtime from child states.
                value.pop("status", None)
                index(value["children"], level + 1)
    index(roots)
    aliases: dict[str, str] = {}
    selected = None

    def allocate(prefix):
        largest = max((int(ident[len(prefix):]) for ident in nodes
                       if re.fullmatch(prefix + r"[0-9]+", ident)), default=0)
        return f"{prefix}{largest + 1}"

    def resolve(reference, expected_level):
        ident = aliases.get(reference[1:]) if reference.startswith("$") else reference
        if ident not in nodes:
            raise ValueError(f"Unknown node or undefined alias: {reference}")
        value, level = nodes[ident]
        if level != expected_level:
            raise ValueError(f"{reference} must reference ITT level {expected_level}")
        return value

    for operation in operations:
        kind = operation["op"]
        if kind == "select_task":
            if selected is not None:
                raise ValueError("Only one select_task operation is allowed per batch")
            selected = resolve(operation["task_id"], 3)["id"]
            continue
        if kind == "add_goal":
            if previous is not None:
                raise ValueError("add_goal is allowed only in the initial plan; preserve the original goals")
            parent, level, prefix = None, 1, "G"
        elif kind == "add_entity":
            parent, level, prefix = resolve(operation["goal_id"], 1), 2, "E"
        else:
            parent, level, prefix = resolve(operation["entity_id"], 2), 3, "T"
        alias = operation.get("ref")
        if alias is not None and alias in aliases:
            raise ValueError(f"Duplicate operation ref: {alias}")
        value = {"id": allocate(prefix), "objective": operation["objective"].strip()}
        if level < 3:
            value["children"] = []
        else:
            value.update(status="todo", execution_code="", execution_result="",
                         semantic_refs=deepcopy(operation.get("semantic_refs", [])))
            if "template_id" in operation:
                value["template_id"] = operation["template_id"]
        nodes[value["id"]] = (value, level)
        if alias is not None:
            aliases[alias] = value["id"]
        (roots if parent is None else parent["children"]).append(value)

    candidate = parse_itt(roots, event_id, round_id, previous=previous,
                          executions=executions, procedures=procedures)
    if selected is not None:
        candidate = replace(candidate, next_task_id=selected)
        if next_task(candidate) is None:
            raise ValueError("select_task must reference an actionable todo T")
    if (previous is None or require_next_task) and next_task(candidate) is None:
        raise ValueError("Investigation requires an actionable task; add a new todo T or select an existing one")
    return candidate
