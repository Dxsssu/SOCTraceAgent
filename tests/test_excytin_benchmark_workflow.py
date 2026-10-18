from __future__ import annotations
from pm_fakes import verify, task_rows

from pm_fakes import approve

import json

import json

import pytest

from src.benchmarks.excytin_bench import (
    BenchmarkActionType,
    ExcytinBenchAgent,
    ExcytinBenchWorkflow,
    ReadOnlySQLValidator,
)

INITIAL_OPERATIONS = """
operations:
  - op: add_goal
    ref: goal
    objective: Investigate the alert
  - op: add_entity
    ref: entity
    goal_id: $goal
    objective: Validate the network evidence
  - op: add_task
    entity_id: $entity
    objective: Query the alert evidence
"""


def role_llm(system_prompt: str, user_prompt: str) -> str:
    if "你是 Verifier" in system_prompt: return verify(user_prompt)
    if system_prompt.startswith("你是 Procedural Memory 的前提核对助手"):
        payload = json.loads(user_prompt)
        return approve(user_prompt)
    if system_prompt.startswith("你正在为当前 Agent 选择 Procedural Memory"):
        payload = json.loads(user_prompt)
        assert all(set(p) == {"id", "name", "inputs", "returns"} for p in payload["catalog"])
        return json.dumps([p["id"] for p in payload["catalog"]][:1])
    if "你是 Executor" in system_prompt:
        task = json.loads(user_prompt)
        assert set(task) == {"id", "objective"}
        assert "procedural_memory" not in user_prompt
        assert "episodic_memory" not in user_prompt
        assert "reason_summary" not in system_prompt
        return """
response_type: EXECUTE_SQL
sql: SELECT AlertId FROM AlertEvidence LIMIT 5
"""
    if "你是 Reviewer" in system_prompt:
        assert "workflow_memory_context" not in user_prompt
        assert set(json.loads(user_prompt)) == {"task", "executor_result"}
        assert "episodic_memory" not in user_prompt
        return 'summary: The queried alert identifier directly answers the question.\nfacts: []\nlimitations: []'
    if "最终答案整理器" in system_prompt:
        return """
response_type: FINAL_ANSWER
answer: alert-1
"""
    assert "# 适用的 Procedural Memory" not in system_prompt
    assert "episodic_memory" not in user_prompt
    return INITIAL_OPERATIONS


def test_default_workflow_and_agent_limits() -> None:
    workflow = ExcytinBenchWorkflow(llm=role_llm)
    agent = ExcytinBenchAgent(workflow=workflow)

    assert workflow.max_steps == 25
    assert workflow.max_executor_attempts_per_l3 == 2
    assert agent.max_steps == 25


def test_disabled_ltm_never_loads_repositories_or_retrieves_recovery(monkeypatch):
    from src.workflow.context import MemoryContextService
    monkeypatch.setenv("SOCAGENT_LTM_ENABLED", "false")
    def forbidden(*args, **kwargs):
        raise AssertionError("LTM must not be accessed")
    monkeypatch.setattr("src.workflow.context.build_default_repositories", forbidden)
    monkeypatch.setattr(MemoryContextService, "executor_recoveries", forbidden)
    calls = []
    def llm(system, user):
        calls.append((system, user))
        assert "# 适用的 Procedural Memory" not in system
        assert "# Semantic Memory" not in system
        if "你是 Executor" in system:
            payload = json.loads(user)
            assert set(payload) == ({"id", "objective", "error"} if "error" in payload else {"id", "objective"})
            if "error" in payload:
                assert set(json.loads(payload["error"].rsplit("\n", 1)[1])) == {"failed_sql", "error"}
            return "sql: SELECT UnknownField FROM SecurityAlert LIMIT 1"
        return INITIAL_OPERATIONS
    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.act("Investigate the alert")
    workflow.act("ProgrammingError: Unknown column 'UnknownField' in 'field list'")
    assert workflow.get_logging()["ltm_enabled"] is False
    assert workflow.get_logging()["memory_retrievals"] == []
    assert len(calls) == 3


def test_planner_yaml_with_explanatory_wrapper_is_retried() -> None:
    calls = []
    def wrapped_llm(system_prompt: str, user_prompt: str) -> str:
        if "你是 Verifier" in system_prompt: return verify(user_prompt)
        if system_prompt.startswith("你是 Procedural Memory 的前提核对助手"):
            payload = json.loads(user_prompt)
            return approve(user_prompt)
        if system_prompt.startswith("你正在为当前 Agent 选择 Procedural Memory"):
            payload = json.loads(user_prompt)
            assert all(set(p) == {"id", "name", "inputs", "returns"} for p in payload["catalog"])
            return json.dumps([p["id"] for p in payload["catalog"]][:1])
        if "系统中的 Planner" in system_prompt:
            calls.append(user_prompt)
            return INITIAL_OPERATIONS if len(calls) > 1 else f"Here is the requested YAML:\n{INITIAL_OPERATIONS}\nNo other changes."
        return role_llm(system_prompt, user_prompt)

    workflow = ExcytinBenchWorkflow(max_steps=5, llm=wrapped_llm)
    ttt = workflow.start("Investigate the alert")

    assert ttt.root_nodes[0].title == "Investigate the alert"
    assert len(calls) == 2


def test_initial_planner_failure_preserves_raw_responses_in_trace() -> None:
    responses = iter(
        [
            'I should not include "response": root? No.',
            'Still not YAML: because I am explaining.',
        ]
    )

    workflow = ExcytinBenchWorkflow(
        max_steps=5,
        llm=lambda _system, _user: "[]" if _system.startswith("你正在为当前 Agent") or "SM 表选择助手" in _system else next(responses),
        yaml_response_attempts=2,
    )

    with pytest.raises(ValueError):
        workflow.start("Investigate the alert")

    logging = workflow.get_logging()
    assert logging["status"] == "failed"
    invalid = [
        item for item in logging["trace"] if item["event_type"] == "role_yaml_invalid"
    ]
    assert len(invalid) == 2
    assert invalid[0]["payload"]["raw_response"].startswith("I should not include")
    assert logging["trace"][-1]["event_type"] == "workflow_start_failed"


def test_over_nested_planner_branch_is_rejected_without_renumbering() -> None:
    import yaml
    payload = yaml.safe_load(INITIAL_OPERATIONS)
    payload["operations"].append({"op": "add_entity", "ref": "invalid", "goal_id": "T1", "objective": "Invalid nesting"})
    over_nested = yaml.safe_dump(payload)
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=lambda _system, _user: "[]" if _system.startswith("你正在为当前 Agent") else over_nested)

    with pytest.raises(ValueError, match="level"):
        workflow.start("Identify the AadDeviceId")


def test_external_action_workflow_waits_for_environment_observation() -> None:
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=role_llm)

    first = workflow.act("Alert context. Which alert id is involved?")
    assert first.action_type == BenchmarkActionType.QUERY
    assert first.content == "SELECT AlertId FROM AlertEvidence LIMIT 5;"
    assert workflow.state is not None
    assert workflow.state.executions == []
    assert workflow.state.ttt.root_nodes[0].children[0].children[0].metadata == {"semantic_refs": [], "execution_code": first.content, "execution_result": ""}

    second = workflow.act("[('alert-1',)]")
    assert second.action_type == BenchmarkActionType.SUBMIT
    assert second.content == "alert-1"

    logging = workflow.get_logging()
    assert logging["actions_issued"] == 2
    assert logging["executions"][0]["observation"] == "[('alert-1',)]"
    assert "decision" not in logging["reviews"][0]
    assert "suggestion" not in logging["reviews"][0]
    assert workflow.state.ready_to_submit
    assert len(logging["ttt_history"]) == 3
    assert logging["ttt_history"][-1]["root_nodes"][0]["status"] == "resolved"
    assert any(
        item["event_type"] == "investigation_loop_exit_scheduled"
        and item["payload"]["reason"] == "verifier_confirmed"
        for item in logging["trace"]
    )
    assert logging["memory_retrievals"] == []
    assert logging["ltm_enabled"] is False


def test_initial_dict_uses_allowlist_and_excludes_ground_truth() -> None:
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=role_llm)
    workflow.start(
        {
            "context": "Investigate this alert.",
            "question": "Which alert id is involved?",
            "answer": "secret-answer",
            "solution": "secret-solution",
            "ground_truth": ["secret-ioc"],
        }
    )
    serialized = json.dumps(workflow.get_logging())
    assert "secret-answer" not in serialized
    assert "secret-solution" not in serialized
    assert "secret-ioc" not in serialized


def test_agent_can_preload_question_dict_without_consuming_it_as_sql_result() -> None:
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=role_llm)
    agent = ExcytinBenchAgent(max_steps=5, workflow=workflow)

    agent.reset(
        question_dict={
            "context": "Alert context.",
            "question": "Which alert id is involved?",
            "answer": "must-not-be-used",
        }
    )
    action, submit = agent.act("Alert context. Which alert id is involved?")
    assert action.startswith("SELECT AlertId")
    assert submit is False
    assert agent.step_count == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM AlertEvidence",
        "SELECT * FROM AlertEvidence; DROP TABLE AlertEvidence",
        "SELECT * FROM AlertEvidence INTO OUTFILE '/tmp/x'",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT SLEEP(10)",
        "SET @x = 1",
    ],
)
def test_sql_validator_rejects_unsafe_or_multiple_statements(sql: str) -> None:
    with pytest.raises(ValueError):
        ReadOnlySQLValidator.validate(sql)


def test_sql_error_is_consumed_before_retry() -> None:
    calls = {"executor": 0, "reviewer": 0}

    def retry_llm(system_prompt: str, user_prompt: str) -> str:
        if "你是 Verifier" in system_prompt: return verify(user_prompt)
        if system_prompt.startswith("你是 Procedural Memory 的前提核对助手"):
            payload = json.loads(user_prompt)
            return approve(user_prompt)
        if system_prompt.startswith("你正在为当前 Agent 选择 Procedural Memory"):
            payload = json.loads(user_prompt)
            assert all(set(p) == {"id", "name", "inputs", "returns"} for p in payload["catalog"])
            return json.dumps([p["id"] for p in payload["catalog"]][:1])
        if "你是 Executor" in system_prompt:
            calls["executor"] += 1
            if calls["executor"] == 1:
                assert "episodic_memory" not in user_prompt
            else:
                assert "episodic_memory" not in user_prompt
                assert set(json.loads(json.loads(user_prompt)["error"].rsplit("\n", 1)[1])) == {"failed_sql", "error"}
                assert "Unknown column" in user_prompt
            return """
response_type: EXECUTE_SQL
sql: SELECT AlertId FROM AlertEvidence LIMIT 1
"""
        if "你是 Reviewer" in system_prompt:
            calls["reviewer"] += 1
            return 'summary: Unknown column must be repaired.\nfacts: []\nlimitations: []'
        if "最终答案整理器" in system_prompt:
            return "response_type: FINAL_ANSWER\nanswer: unknown"
        if "根据真实 SQL 结果的 Review 输出带 base_version" in user_prompt:
            return INITIAL_OPERATIONS
        return INITIAL_OPERATIONS

    workflow = ExcytinBenchWorkflow(max_steps=4, llm=retry_llm)
    first = workflow.act("Investigate the alert")
    assert calls["executor"] == 1
    second = workflow.act("ProgrammingError: Unknown column 'AlertId'")
    assert first.action_type == second.action_type == BenchmarkActionType.QUERY
    assert calls["executor"] == 2
    assert calls["reviewer"] == 0
    assert workflow.get_logging()["executions"][0]["result_status"] == "error"


def test_empty_result_goes_directly_to_reviewer_without_memory(monkeypatch) -> None:
    from src.workflow.context import MemoryContextService
    calls = {"executor": 0, "reviewer": 0}

    def forbidden(*args, **kwargs):
        raise AssertionError("No memory retrieval for Reviewer or empty results")

    monkeypatch.setattr(MemoryContextService, "reviewer_view", forbidden)
    monkeypatch.setattr(MemoryContextService, "_build_reviewer", forbidden)
    monkeypatch.setattr(MemoryContextService, "executor_recoveries", forbidden)

    def llm(system, user):
        if "你是 Verifier" in system: return verify(user)
        if "你是 Executor" in system:
            calls["executor"] += 1
        if "你是 Reviewer" in system:
            calls["reviewer"] += 1
            payload = json.loads(user)
            assert set(payload) == {"task", "executor_result"}
            assert payload["executor_result"]["result_status"] == "empty_result"
            assert payload["executor_result"]["observation"] == "[]"
            assert set(payload["task"]) == {"id", "objective"}
            assert "workflow_memory_context" not in user
            assert "execution_result" not in user  # evidence occurs once, in executor_result
            return 'summary: No matching rows and no further leads.\nfacts: []\nlimitations: []'
        return role_llm(system, user)

    workflow = ExcytinBenchWorkflow(llm=llm)
    first = workflow.act("Investigate the alert")
    final = workflow.act("[]")
    assert first.action_type == BenchmarkActionType.QUERY
    assert final.action_type == BenchmarkActionType.SUBMIT
    assert calls == {"executor": 1, "reviewer": 1}
    assert not any(e["event_type"] == "executor_empty_repair_scheduled"
                   for e in workflow.state.trace)


def test_executor_errors_allow_only_one_em_repair() -> None:
    calls = {"executor": 0, "reviewer": 0, "planner": 0}

    def capped_llm(system_prompt: str, user_prompt: str) -> str:
        if "你是 Verifier" in system_prompt: return verify(user_prompt)
        if system_prompt.startswith("你是 Procedural Memory 的前提核对助手"):
            payload = json.loads(user_prompt)
            return approve(user_prompt)
        if system_prompt.startswith("你正在为当前 Agent 选择 Procedural Memory"):
            payload = json.loads(user_prompt)
            assert all(set(p) == {"id", "name", "inputs", "returns"} for p in payload["catalog"])
            return json.dumps([p["id"] for p in payload["catalog"]][:1])
        if "你是 Executor" in system_prompt:
            calls["executor"] += 1
            if calls["executor"] == 1:
                assert "episodic_memory" not in user_prompt
            elif calls["executor"] == 2:
                assert "episodic_memory" not in user_prompt
            else:
                assert "episodic_memory" not in user_prompt
            return "response_type: EXECUTE_SQL\nsql: SELECT AlertId FROM AlertEvidence"
        if "你是 Reviewer" in system_prompt:
            calls["reviewer"] += 1
            return 'summary: The L3 query could not retrieve evidence.\nfacts: []\nlimitations: []'
        if "最终答案整理器" in system_prompt:
            return "response_type: FINAL_ANSWER\nanswer: unknown"
        calls["planner"] += 1
        return INITIAL_OPERATIONS

    workflow = ExcytinBenchWorkflow(
        max_steps=10,
        llm=capped_llm,
        max_error_repairs=10,
        max_executor_attempts_per_l3=3,
    )

    first = workflow.act("Investigate the alert")
    second = workflow.act("ProgrammingError: Unknown column 'AlertId'")
    submit = workflow.act("ProgrammingError: Unknown column 'AlertId'")

    assert first.action_type == second.action_type
    assert first.action_type == BenchmarkActionType.QUERY
    assert submit.action_type == BenchmarkActionType.SUBMIT
    assert calls == {"executor": 2, "reviewer": 1, "planner": 1}
    logging = workflow.get_logging()
    assert logging["max_executor_attempts_per_l3"] == 2
    assert len(logging["executions"]) == 2
    assert sum(e["event_type"] == "executor_error_recovery" for e in logging["trace"]) == 1
    assert any(
        item["event_type"] == "executor_attempt_budget_exhausted"
        and item["payload"]["attempt_count"] == 2
        for item in logging["trace"]
    )


def test_query_budget_exhaustion_skips_replanning_and_submits() -> None:
    calls = {"planner": 0, "executor": 0, "reviewer": 0, "answer": 0}

    def budget_llm(system_prompt: str, user_prompt: str) -> str:
        if "你是 Verifier" in system_prompt: return verify(user_prompt)
        if system_prompt.startswith("你是 Procedural Memory 的前提核对助手"):
            payload = json.loads(user_prompt)
            return approve(user_prompt)
        if system_prompt.startswith("你正在为当前 Agent 选择 Procedural Memory"):
            payload = json.loads(user_prompt)
            assert all(set(p) == {"id", "name", "inputs", "returns"} for p in payload["catalog"])
            return json.dumps([p["id"] for p in payload["catalog"]][:1])
        if "你是 Executor" in system_prompt:
            calls["executor"] += 1
            return "response_type: EXECUTE_SQL\nsql: SELECT CompromisedEntity FROM SecurityAlert"
        if "你是 Reviewer" in system_prompt:
            calls["reviewer"] += 1
            assert json.loads(user_prompt)["task"]["id"] == "T1"
            return 'summary: The requested host is host-1.\nfacts: []\nlimitations: []'
        if "最终答案整理器" in system_prompt:
            calls["answer"] += 1
            return "response_type: FINAL_ANSWER\nanswer: host-1"
        calls["planner"] += 1
        return INITIAL_OPERATIONS

    workflow = ExcytinBenchWorkflow(max_steps=2, llm=budget_llm)
    query = workflow.act("Which host is involved?")
    assert query.action_type == BenchmarkActionType.QUERY

    submit = workflow.act("[('host-1',)]")
    assert submit.action_type == BenchmarkActionType.SUBMIT
    assert submit.content == "无法确认答案：调查结束，尚无充分证据。"
    assert calls == {"planner": 1, "executor": 1, "reviewer": 1, "answer": 0}
    assert any(
        item["event_type"] == "investigation_loop_exit_scheduled"
        and item["payload"]["reason"] == "query_budget_exhausted"
        for item in workflow.get_logging()["trace"]
    )


@pytest.mark.parametrize('value', ["bcdedit /set recoveryenabled no", "vssblatemp.exe delete shadows /all", "DROP TABLE evidence"])
def test_sql_validator_allows_command_line_evidence_literals(value: str) -> None:
    sql = f"SELECT ProcessId FROM DeviceProcessEvents WHERE ProcessCommandLine = '{value}'"
    assert ReadOnlySQLValidator.validate(sql) == sql + ';'


@pytest.mark.parametrize('sql', [
    "SELECT 'safe', SLEEP(1)",
    "SELECT 'safe' INTO OUTFILE '/tmp/out'",
    "WITH sample AS (SELECT 'safe') DELETE FROM DeviceProcessEvents",
    "SELECT 'safe'; DROP TABLE DeviceProcessEvents",
])
def test_literal_handling_does_not_allow_real_side_effects(sql: str) -> None:
    with pytest.raises(ValueError):
        ReadOnlySQLValidator.validate(sql)


def test_replace_string_function_is_read_only_but_replace_statements_are_not():
    sql = "SELECT REPLACE(UserPrincipalName, ' ', '') FROM SigninLogs"
    assert ReadOnlySQLValidator.validate(sql) == sql + ';'
    for unsafe in ("REPLACE INTO SigninLogs (UserId) VALUES ('x')",
                   "EXPLAIN REPLACE INTO SigninLogs (UserId) VALUES ('x')",
                   "SELECT 1; REPLACE INTO SigninLogs (UserId) VALUES ('x')",
                   "SELECT REPLACE(LOAD_FILE('/tmp/x'), 'a', 'b')"):
        with pytest.raises(ValueError):
            ReadOnlySQLValidator.validate(unsafe)


def test_benchmark_planner_documented_initial_contract_is_executable():
    import yaml
    from src.agent.planner import PLANNER_SYSTEM_PROMPT

    example = "operations:" + PLANNER_SYSTEM_PROMPT.split("\noperations:", 1)[1]
    tree = ExcytinBenchWorkflow._parse_ttt(yaml.safe_load(example), "contract-test", round_id=1)
    assert tree.next_task_id == "T1"
    assert ExcytinBenchWorkflow._next_todo_leaf(tree).node_id == "T1"
    assert "仅输出合法 YAML 对象" in PLANNER_SYSTEM_PROMPT
    assert "base_version" not in PLANNER_SYSTEM_PROMPT


def test_executor_repair_adds_error_without_changing_objective(monkeypatch):
    from src.workflow.context import MemoryContextService
    from src.schema.itt import itt_payload

    def forbidden(*args, **kwargs):
        raise AssertionError("Executor must not retrieve memory")

    monkeypatch.setattr(MemoryContextService, "executor_view", forbidden)
    monkeypatch.setattr(MemoryContextService, "_build_executor", forbidden)
    prompts = []

    def llm(system, user):
        if "你是 Verifier" in system: return verify(user)
        if "你是 Executor" in system:
            task = json.loads(user)
            prompts.append(task)
            assert set(task) == ({"id", "objective"} if len(prompts) == 1 else {"id", "objective", "error"})
            assert "private-context-marker" not in user
            assert "private-question-marker" not in user
            return "response_type: EXECUTE_SQL\nsql: SELECT AlertId FROM AlertEvidence LIMIT 1"
        return role_llm(system, user)

    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.start({"context": "private-context-marker", "question": "private-question-marker"})
    first = workflow.propose_next_action()
    assert set(prompts[0]) == {"id", "objective"}
    assert workflow.state.pending_node.metadata["execution_code"] == first.content
    workflow.accept_observation("Unknown column 'AlertId'", query_success=False)
    task = itt_payload(workflow.state.ttt)[0]["children"][0]["children"][0]
    assert task["execution_code"] == first.content
    assert task["execution_result"] == ""
    assert workflow.state.ttt_history[-1] == workflow.state.ttt.to_dict()
    workflow.propose_next_action()
    assert prompts[1]["id"] == task["id"]
    assert prompts[1]["objective"] == prompts[0]["objective"] == task["objective"]
    assert set(prompts[1]) == {"id", "objective", "error"}
    assert "请参考并重新生成" in prompts[1]["error"]
    assert json.loads(prompts[1]["error"].rsplit("\n", 1)[1])["failed_sql"] == first.content
    workflow.accept_observation("[{\"AlertId\": \"alert-1\"}]", query_success=True)
    task = itt_payload(workflow.state.ttt)[0]["children"][0]["children"][0]
    assert task["status"] == "done"
    assert json.loads(task["execution_code"]) == [first.content, first.content]
    assert task["execution_result"]["summary"] == "The queried alert identifier directly answers the question."


def test_error_recovery_uses_actual_error_without_ltm(monkeypatch):
    from src.workflow.context import MemoryContextService
    from src.schema.itt import itt_payload
    calls = []
    retrievals = []
    retrieve = MemoryContextService.executor_recoveries

    def only_em(self, event, **kwargs):
        retrievals.append(kwargs)
        return retrieve(self, event, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("No Executor SM/PM context allowed")

    monkeypatch.setattr(MemoryContextService, "executor_recoveries", only_em)
    monkeypatch.setattr(MemoryContextService, "_build_executor", forbidden)

    def llm(system, user):
        if "你是 Verifier" in system: return verify(user)
        if "你是 Executor" in system:
            payload = json.loads(user)
            calls.append(payload)
            if len(calls) == 1:
                assert "error_recovery" not in payload
                return "response_type: EXECUTE_SQL\nsql: SELECT Title FROM SecurityAlert WHERE SystemAlertId = 'alert-1'"
            recovery = json.loads(payload["error"].rsplit("\n", 1)[1])
            assert "Title" in recovery["error"]
            assert set(recovery) == {"failed_sql", "error"}
            assert "SystemAlertId = 'alert-1'" in recovery["failed_sql"]
            assert "procedural_memory" not in user and "semantic_memory" not in user
            return "response_type: EXECUTE_SQL\nsql: SELECT AlertName FROM SecurityAlert WHERE SystemAlertId = 'alert-1'"
        return role_llm(system, user)

    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.act("Find alert name")
    assert retrievals == []
    action = workflow.act("ProgrammingError: Unknown column 'Title' in 'field list'")
    assert retrievals == []
    assert "AlertName" in action.content
    assert "SystemAlertId = 'alert-1'" in action.content
    workflow.accept_observation("[('suspicious login',)]", query_success=True)
    task = itt_payload(workflow.state.ttt)[0]["children"][0]["children"][0]
    assert task["status"] == "done"
    assert len(json.loads(task["execution_code"])) == 2
    assert "error_recovery" not in task
    assert retrievals == []


def test_reviewer_receives_only_current_executor_result():
    from src.benchmarks.excytin_bench.workflow import BenchmarkExecution
    captured = []
    def llm(system, user):
        if "你是 Reviewer" in system:
            captured.append(json.loads(user))
            return 'summary: Current result reviewed.\nfacts: []\nlimitations: []'
        return role_llm(system, user)
    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.start("Investigate the alert")
    old = BenchmarkExecution(1, "T1", "Old attempt", "SELECT 1", "OLD_RESULT", True, "rows_returned")
    current = BenchmarkExecution(2, "T1", "Current attempt", "SELECT 2", "CURRENT_RESULT", True, "rows_returned")
    workflow.state.executions.extend([old, current])
    workflow._review(current)
    payload = captured[0]
    assert set(payload) == {"task", "executor_result"}
    assert payload["executor_result"]["execution_id"] == "E2"
    assert payload["executor_result"]["sql"] == "SELECT 2"
    assert payload["executor_result"]["observation"] == "CURRENT_RESULT"
    assert "OLD_RESULT" not in json.dumps(payload)


def test_empty_result_continue_replans_new_task():
    import yaml
    workflow = None
    reviews = []
    def llm(system, user):
        if "你是 Verifier" in system: return verify(user)
        if "你是 Reviewer" in system:
            reviews.append(json.loads(user))
            return 'summary: No matching rows.\nfacts: []\nlimitations: []'
        if "系统中的 Planner" in system and workflow.state is not None:
            payload = yaml.safe_load(user)["itt"]
            tasks = task_rows(payload)
            assert tasks[0]["status"] == "done"
            assert "result_summary" not in tasks[0]
            assert tasks[0]["execution_result"]["summary"] == "No matching rows."
            return yaml.safe_dump({"operations": [{"op": "add_task", "entity_id": "E1",
                                                   "objective": "Inspect another source"}]})
        return role_llm(system, user)

    workflow = ExcytinBenchWorkflow(llm=llm)
    first = workflow.act("Investigate the alert")
    second = workflow.act("[]")
    assert first.node_id == "T1" and second.node_id == "T2"
    assert len(reviews) == 1
    assert reviews[0]["executor_result"]["query_success"] is True
    assert reviews[0]["executor_result"]["result_status"] == "empty_result"


def test_reviewer_retries_overlong_summary_before_accepting():
    calls = []
    def llm(system, user):
        if "你是 Verifier" in system: return verify(user)
        if "你是 Reviewer" in system:
            calls.append(user)
            if len(calls) == 1:
                return json.dumps({"summary": "字" * 241, "facts": [], "limitations": []})
            assert "summary exceeds 240 characters" in user
            return 'summary: "E1 确认告警 ID 为 alert-1。"\nfacts: []\nlimitations: []'
        return role_llm(system, user)

    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.act("Find alert ID")
    result = workflow.act("[('alert-1',)]")
    assert result.action_type == BenchmarkActionType.SUBMIT
    assert len(calls) == 2
    assert len(workflow.state.reviews[0]["summary"]) <= 240
    assert "suggestion" not in workflow.state.reviews[0]


@pytest.mark.parametrize('response, expected_calls', [(': broken yaml', 2), ('sql: DELETE FROM SecurityAlert', 1), ('sql: ""', 2)])
def test_executor_format_retries_once_but_unsafe_sql_does_not(response, expected_calls):
    import yaml
    from src.workflow.context import MemoryContextService
    calls = []
    def llm(system, user):
        if '系统中的 Planner' in system:
            return INITIAL_OPERATIONS
        if '你是 Executor' in system:
            calls.append(user)
            return response
        raise AssertionError(system)
    workflow = ExcytinBenchWorkflow(llm=llm, yaml_response_attempts=5,
        )
    with pytest.raises((ValueError, yaml.YAMLError)):
        workflow.act('Investigate the alert')
    assert len(calls) == expected_calls
    assert workflow.state.actions_issued == 0
    assert workflow.state.executions == []


def test_optional_local_sql_syntax_repair_does_not_execute_rejected_query():
    from src.workflow.context import MemoryContextService
    import json
    calls = []
    def llm(system, user):
        if '系统中的 Planner' in system:
            return INITIAL_OPERATIONS
        calls.append(json.loads(user))
        return 'sql: "SELECT (1"' if len(calls) == 1 else 'sql: SELECT 1'
    workflow = ExcytinBenchWorkflow(llm=llm, executor_format_attempts=2,
                                   )
    action = workflow.act('Investigate the alert')
    assert action.content == 'SELECT 1;'
    assert len(calls) == 2
    assert all(set(p) == {'id', 'objective'} for p in calls)
    assert 'SELECT (1' in calls[1]['objective']
    assert workflow.state.actions_issued == 1
    assert workflow.state.executions == []
    assert any(t['event_type'] == 'executor_sql_invalid' for t in workflow.state.trace)


def test_unsafe_sql_is_not_retried_even_with_optional_syntax_repair():
    from src.workflow.context import MemoryContextService
    calls = []
    def llm(system, user):
        if '系统中的 Planner' in system:
            return INITIAL_OPERATIONS
        calls.append(user)
        return 'sql: DELETE FROM SecurityAlert'
    workflow = ExcytinBenchWorkflow(llm=llm, executor_format_attempts=2,
                                   )
    with pytest.raises(ValueError):
        workflow.act('Investigate the alert')
    assert len(calls) == 1
    assert workflow.state.actions_issued == 0
