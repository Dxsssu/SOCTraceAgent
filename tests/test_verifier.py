import json
import pytest
import yaml
from src.agent.verifier import validate_verification
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.workflow.context import MemoryContextService
from test_excytin_benchmark_workflow import INITIAL_TTT


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
                return INITIAL_TTT
            payload = yaml.safe_load(user)
            assert "Verifier" in payload["review"]["summary"]
            assert set(payload["review"]) == {"summary"}
            assert "# 输出格式模板" in system
            assert "\n- id: G1" in system
            assert user.startswith("context:")
            tree = payload["itt"]
            tree[0]["children"][0]["children"].append({
                "id": "T2", "objective": "Check target relation", "status": "todo",
                "execution_code": "", "execution_result": ""})
            return yaml.safe_dump(tree)
        if "SQL 查询生成器" in system:
            calls.append("executor")
            return "sql: SELECT FileName FROM AlertEvidence"
        if "你是 Reviewer" in system:
            calls.append("reviewer")
            return json.dumps({"summary": "Result reviewed"})
        if "你是 Verifier" in system:
            calls.append("verifier")
            p = json.loads(user)
            assert set(p) == {"context", "question", "itt", "reviewer"}
            assert set(p["reviewer"]) == {"summary"}
            assert p["context"] == "Investigation context"
            tasks = p["itt"][0]["children"][0]["children"]
            assert tasks[0]["execution_code"]
            assert tasks[0]["execution_result"] == "Result reviewed"
            confirmed = len(tasks) == 2
            if confirmed:
                assert tasks[1]["execution_result"] == "Result reviewed"
            return json.dumps({"can_answer": "yes", "answer": "browsercore.exe"} if confirmed else {"can_answer": "no"})
        raise AssertionError("Unexpected role: " + system)
    workflow = ExcytinBenchWorkflow(llm=model, memory_contexts=MemoryContextService(enabled=False))
    workflow.act({"context": "Investigation context", "question": "Which process?"})
    second = workflow.act("Only a lead")
    assert not second.submit and second.node_id == "T2"
    final = workflow.act("Target: browsercore.exe")
    assert final.submit and final.content == "browsercore.exe"
    assert calls == ["planner", "executor", "reviewer", "verifier", "planner", "executor", "reviewer", "verifier"]


def test_budget_exit_does_not_invent_answer():
    def model(system, user):
        if "系统中的 Planner" in system: return INITIAL_TTT
        if "SQL 查询生成器" in system: return "sql: SELECT 1"
        if "你是 Reviewer" in system: return "summary: Candidate found"
        if "你是 Verifier" in system:
            return 'can_answer: "no"'
        raise AssertionError(system)
    workflow = ExcytinBenchWorkflow(llm=model, max_steps=2,
        memory_contexts=MemoryContextService(enabled=False))
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
                return INITIAL_TTT
            # Retry appends feedback after the JSON document.
            payload = yaml.safe_load(user.split("\n上一次输出未通过校验。", 1)[0])
            tree = payload['itt']
            replans.append(user)
            if len(replans) == 2:
                assert 'add a new todo T' in user
                if correct_on_retry:
                    tree[0]['children'][0]['children'].append({
                        'id': 'T2', 'objective': 'Discover actual table schema', 'status': 'todo',
                        'execution_code': '', 'execution_result': ''})
            return yaml.safe_dump(tree)
        if 'SQL 查询生成器' in system:
            return 'sql: SHOW TABLES'
        if '你是 Reviewer' in system:
            return 'summary: Evidence insufficient'
        if '你是 Verifier' in system:
            return 'can_answer: "no"'
        raise AssertionError(system)
    workflow = ExcytinBenchWorkflow(llm=model, max_error_repairs=0,
        memory_contexts=MemoryContextService(enabled=False))
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
