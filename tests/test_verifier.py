from pm_fakes import task_rows
import json
import pytest
import yaml
from src.agent.verifier import validate_verification
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.workflow.context import MemoryContextService
from test_excytin_benchmark_workflow import INITIAL_OPERATIONS


@pytest.mark.parametrize("value", [
    {"can_answer": "yes", "answer": ""},
    {"can_answer": "no", "answer": "invented"},
    {"can_answer": "no", "answer": ""},
    {"can_answer": "maybe"},
    {"can_answer": "yes"},
    {"can_answer": "yes", "answer": "answer", "extra": 1},
])
def test_invalid_verifier_contract(value):
    with pytest.raises(ValueError):
        validate_verification(value)


def test_verifier_controls_loop_and_supplies_final_answer():
    calls = []
    workflow = None
    def model(system, user):
        if "系统中的 Planner" in system:
            calls.append("planner")
            if workflow.state is None:
                return INITIAL_OPERATIONS
            payload = yaml.safe_load(user)
            assert payload["review"] == {"can_answer": "no"}
            assert "# 输出格式模板" in system
            assert "\noperations:" in system
            assert user.startswith("context:")
            return yaml.safe_dump({"operations": [{"op": "add_task", "entity_id": "E1",
                                                   "objective": "Check target relation"}]})
        if "你是 Executor" in system:
            calls.append("executor")
            return "sql: SELECT FileName FROM AlertEvidence"
        if "你是 Reviewer" in system:
            calls.append("reviewer")
            return json.dumps({"summary": "Result reviewed", "facts": [], "limitations": []})
        if "你是 Verifier" in system:
            calls.append("verifier")
            p = yaml.safe_load(user)
            assert set(p) == {"context", "question", "itt"}
            assert p["context"] == "Investigation context"
            tasks = task_rows(p["itt"])
            assert tasks[0]["execution_code"]
            assert tasks[0]["execution_result"]["summary"] == "Result reviewed"
            confirmed = len(tasks) == 2
            if confirmed:
                assert tasks[1]["execution_result"]["summary"] == "Result reviewed"
            return json.dumps({"can_answer": "yes", "answer": "browsercore.exe"} if confirmed else {"can_answer": "no"})
        raise AssertionError("Unexpected role: " + system)
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    workflow.act({"context": "Investigation context", "question": "Which process?"})
    second = workflow.act("Only a lead")
    assert not second.submit and second.node_id == "T2"
    final = workflow.act("Target: browsercore.exe")
    assert final.submit and final.content == "browsercore.exe"
    assert calls == ["planner", "executor", "reviewer", "verifier", "planner", "executor", "reviewer", "verifier"]


def test_budget_exit_does_not_invent_answer():
    def model(system, user):
        if "系统中的 Planner" in system: return INITIAL_OPERATIONS
        if "你是 Executor" in system: return "sql: SELECT 1"
        if "你是 Reviewer" in system: return 'summary: Candidate found\nfacts: []\nlimitations: []'
        if "你是 Verifier" in system:
            return 'can_answer: "no"'
        raise AssertionError(system)
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model, max_steps=2)
    workflow.act("Find target")
    final = workflow.act("candidate")
    assert final.submit and final.content == "无法确认答案：调查结束，尚无充分证据。"
    assert not workflow.state.ready_to_submit


@pytest.mark.parametrize("decision", ["yes", "no"])
def test_verifier_accepts_yaml_yes_no(decision):
    text = f"can_answer: {decision}" + ('\nanswer: result' if decision == 'yes' else '')
    result = validate_verification(yaml.safe_load(text))
    assert result["can_answer"] == decision
    assert ("answer" in result) == (decision == "yes")


@pytest.mark.parametrize('execution_success', [True, False])
@pytest.mark.parametrize('correct_on_retry', [True, False])
def test_terminal_tree_cannot_bypass_verifier(execution_success, correct_on_retry):
    workflow = None
    replans = []
    def model(system, user):
        if '系统中的 Planner' in system:
            if workflow.state is None:
                return INITIAL_OPERATIONS
            # Retry appends feedback after the JSON document.
            payload = yaml.safe_load(user.split("\n上一次输出未通过校验。", 1)[0])
            operations = []
            replans.append(user)
            if len(replans) == 2:
                assert 'add a new todo T' in user
                if correct_on_retry:
                    operations.append({'op': 'add_task', 'entity_id': 'E1',
                                       'objective': 'Discover actual table schema'})
            return yaml.safe_dump({'operations': operations})
        if '你是 Executor' in system:
            return 'sql: SHOW TABLES'
        if '你是 Reviewer' in system:
            return 'summary: Evidence insufficient\nfacts: []\nlimitations: []'
        if '你是 Verifier' in system:
            return 'can_answer: "no"'
        raise AssertionError(system)
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model, max_error_repairs=0)
    workflow.act('Find target')
    observation = '[]' if execution_success else 'ProgrammingError: missing table'
    if correct_on_retry:
        action = workflow.act(observation)
        assert not action.submit and action.node_id == 'T2'
    else:
        with pytest.raises(ValueError, match='add a new todo T'):
            workflow.act(observation)
    assert len(replans) == 2
    assert not workflow.state.done
    assert not workflow.state.final_answer
    assert all(a['action_type'] != 'submit' for a in workflow.state.action_history)
