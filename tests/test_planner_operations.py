"""Planner operations protect history independently of model compliance."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest
import yaml

from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.working_memory import TTTStore
from src.schema import Event, EventStatus, RoundReview
from src.schema.itt import itt_payload
from src.schema.planner_operations import apply_planner_operations, parse_planner_response


def test_single_transport_fence_keeps_strict_operation_validation():
    assert parse_planner_response('```yaml\noperations: []\n```') == {"operations": []}
    for text in ('Explanation\n```yaml\noperations: []\n```',
                 '```yaml\noperations: []\n```\nTrailing explanation',
                 '```yaml\noperations: []\n```\n```yaml\noperations: []\n```',
                 '```yaml\noperations: []\nanswer: invented\n```'):
        with pytest.raises((ValueError, yaml.YAMLError)):
            parse_planner_response(text)


def test_operation_field_error_identifies_actual_missing_and_unexpected_fields():
    with pytest.raises(ValueError) as error:
        parse_planner_response('operations:\n- {op: add_task, id: T2, parent: E1, objective: query, execution_code: SELECT 1}')
    assert "missing fields ['entity_id']" in str(error.value)
    assert "unexpected fields ['execution_code', 'id', 'parent']" in str(error.value)
from src.schema.ttt import TTTNodeStatus as S
from src.schema.ttt_updates import change_node, next_task, walk
from src.workflow.context import MemoryContextService
from src.workflow.orchestrator import SOCTraceWorkflow


def initial():
    return parse_planner_response(PLANNER_SYSTEM_PROMPT.split("# 输出格式模板\n", 1)[1])


def tree():
    return apply_planner_operations(initial(), "event", 1)


def apply(operations, previous=None, **kwargs):
    return apply_planner_operations({"operations": operations}, "event", 2,
                                    previous=previous, **kwargs)


def test_program_assigns_ids_and_keeps_aliases_out_of_snapshots():
    payload = initial()
    payload["operations"] += [
        {"op": "add_task", "ref": "preferred", "entity_id": "$alert", "objective": "Check process creation"},
        {"op": "select_task", "task_id": "$preferred"},
    ]
    result = apply_planner_operations(payload, "event", 1)
    assert [n.node_id for n, _ in walk(result)] == ["G1", "E1", "T1", "T2"]
    assert next_task(result).node_id == "T2"
    assert "$alert" not in json.dumps(result.to_dict())
    assert all(n.metadata["execution_code"] == "" for n, d in walk(result) if d == 3)


def test_multiple_goals_and_explicit_selection_survive_sqlite(tmp_path):
    batch = initial()["operations"] + [
        {"op": "add_goal", "ref": "second_goal", "objective": "Identify the host"},
        {"op": "add_entity", "ref": "host", "goal_id": "$second_goal", "objective": "Host A"},
        {"op": "add_task", "ref": "second_task", "entity_id": "$host", "objective": "Query host identifier"},
        {"op": "select_task", "task_id": "$second_task"},
    ]
    result = apply(batch)
    store = TTTStore(tmp_path / "state.db")
    saved = store.save_snapshot(result, expected_version=0)
    restored = store.get_latest_ttt("event")
    assert restored.to_dict() == saved.to_dict()
    assert store.claim_next_todo_leaf("event", 2).node_id == "T2"


@pytest.mark.parametrize("status", [S.DONE, S.BLOCKED])
def test_add_task_preserves_terminal_task_and_all_attempts(status):
    old = change_node(tree(), "T1", status=status, evidence_refs=("exec1", "exec2"),
                      metadata={"semantic_refs": [], "execution_result": "已核对：两个候选。",
                                "execution_result_source": "reviewer", "custom": "preserved"})
    records = [dict(node_id="T1", execution_id=f"exec{i}", sql=f"SELECT {i}", observation=str(i)) for i in (1, 2)]
    before = deepcopy(old.to_dict())
    result = apply([{"op": "add_task", "entity_id": "E1", "objective": "Resolve candidates"}], old, executions=records)
    prior = result.root_nodes[0].children[0].children[0]
    assert prior.status == status
    assert prior.title == old.root_nodes[0].children[0].children[0].title
    assert prior.metadata["custom"] == "preserved"
    assert prior.metadata["execution_result"] == "已核对：两个候选。"
    assert json.loads(prior.metadata["execution_code"]) == ["SELECT 1", "SELECT 2"]
    assert result.root_nodes[0].evidence_refs == ("exec1", "exec2")
    assert next_task(result).node_id == "T2"
    assert result.version == old.version + 1
    assert old.to_dict() == before


@pytest.mark.parametrize("bad", [
    {"op": "delete", "task_id": "T1"},
    {"op": "add_task", "entity_id": "E1", "objective": "New", "id": "T1"},
    {"op": "add_task", "entity_id": "E1", "objective": "New", "status": "done"},
    {"op": "add_task", "entity_id": "E1", "objective": "New", "execution_result": "invented"},
    {"op": "add_task", "entity_id": "T1", "objective": "Fourth level"},
    {"op": "add_task", "entity_id": "$undefined", "objective": "New"},
    {"op": "add_entity", "ref": "wrong", "goal_id": "E1", "objective": "Wrong parent"},
    {"op": "add_goal", "ref": "drift", "objective": "Unrequested goal"},
    {"op": "select_task", "task_id": "E1"},
    {"op": "select_task", "task_id": "missing"},
    {"op": "add_task", "entity_id": "E1", "objective": ""},
    {"op": "add_task", "entity_id": "E1", "objective": "New", "semantic_refs": "invalid"},
    {"op": "add_task", "entity_id": "E1", "objective": "New", "template_id": "fabricated"},
])
def test_invalid_batch_is_atomic_even_after_valid_operation(bad):
    old = tree()
    before = deepcopy(old.to_dict())
    with pytest.raises(ValueError):
        apply([{"op": "add_task", "entity_id": "E1", "objective": "Valid first op"}, bad], old)
    assert old.to_dict() == before


def test_aliases_are_unique_and_response_scoped():
    batch = initial()["operations"]
    batch[1]["ref"] = "goal"
    with pytest.raises(ValueError, match="Duplicate"):
        apply(batch)
    with pytest.raises(ValueError, match="undefined alias"):
        apply([{"op": "add_task", "entity_id": "$alert", "objective": "New task"}], tree())


def test_selection_cannot_reopen_terminal_task():
    old = change_node(tree(), "T1", status=S.DONE, evidence_refs=("exec1",))
    with pytest.raises(ValueError, match="actionable"):
        apply([{"op": "select_task", "task_id": "T1"}], old)
    with pytest.raises(ValueError, match="Only one"):
        apply([{"op": "select_task", "task_id": "T1"}] * 2, tree())


def test_noop_and_empty_frontier_rules():
    assert next_task(apply([], tree())).node_id == "T1"
    done = change_node(tree(), "T1", status=S.DONE, evidence_refs=("exec1",))
    assert next_task(apply([], done)) is None  # local workflow retains its derived completion rule
    with pytest.raises(ValueError, match="add a new todo T"):
        apply([], done, require_next_task=True)
    with pytest.raises(ValueError, match="child"):
        apply(initial()["operations"][:-1])


@pytest.mark.parametrize("payload", [[], {"operations": None}, {"operations": [], "answer": "invented"}, {"operations": [{}]}, {"operations": [None]}, {"operations": [{"op": []}]}])
def test_response_envelope_is_strict(payload):
    with pytest.raises(ValueError):
        parse_planner_response(yaml.safe_dump(payload))


def test_full_tree_response_is_not_accepted_as_legacy_fallback():
    with pytest.raises(ValueError, match="not a full ITT"):
        parse_planner_response(yaml.safe_dump(itt_payload(tree())))


def test_pm_template_uses_canonical_definition():
    template = {"id": "pm1", "sql_template": "SELECT 1", "inputs": {}, "returns": {},
                "semantic_refs": [{"table": "KnownTable", "fields": ["KnownField"]}]}
    batch = initial()
    batch["operations"][-1]["template_id"] = "pm1"
    result = apply_planner_operations(batch, "event", 1, procedures=[template])
    assert next_task(result).metadata["query_template"] == template
    replanned = apply([], result)
    assert next_task(replanned).metadata["query_template"] == template


def test_background_workflow_initial_and_replan_use_operations(tmp_path):
    workflow = SOCTraceWorkflow(db_path=str(tmp_path / "runtime.db"))
    event = Event(event_id="event", event_name="alert", message="Identify process")
    with patch("src.agent.planner.call_llm", return_value=yaml.safe_dump(initial())):
        event = workflow.planner.process_initial_plan(event)
    assert event.event_status == EventStatus.PLANNED
    first = workflow.ttt_store.get_latest_ttt("event")
    workflow.storage.save_round_review(RoundReview(event_id="event", round_id=1, findings=("Process 42 identified",)))
    batch = {"operations": [
        {"op": "add_task", "ref": "next", "entity_id": "E1", "objective": "Check process 42"},
        {"op": "select_task", "task_id": "$next"},
    ]}
    with patch("src.agent.planner.call_llm", return_value=yaml.safe_dump(batch)):
        updated = workflow.planner.process_replanning(event)
    saved = workflow.ttt_store.get_latest_ttt("event")
    assert updated.event_status == EventStatus.PLANNED
    assert saved.next_task_id == "T2" and saved.version == first.version + 1
    assert saved.root_nodes[0].children[0].children[0] == first.root_nodes[0].children[0].children[0]


def test_sql_verifier_cannot_be_bypassed_by_empty_operations():
    def model(system, user):
        if "系统中的 Planner" in system:
            return yaml.safe_dump(initial()) if user.startswith("下面是此次事件的背景和调查问题") else "operations: []"
        if "你是 Executor" in system:
            return "sql: SELECT 1"
        if "你是 Reviewer" in system:
            return 'summary: Candidate only\nfacts: []\nlimitations: []'
        return 'can_answer: "no"'
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    workflow.act("Locate target")
    with pytest.raises(ValueError, match="add a new todo T"):
        workflow.act("[(1,)]")
    assert not workflow.state.done and not workflow.state.final_answer
