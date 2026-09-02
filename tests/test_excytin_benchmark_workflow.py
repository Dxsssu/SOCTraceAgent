from __future__ import annotations

import json

import pytest

from src.benchmarks.excytin_bench import (
    BenchmarkActionType,
    ExcytinBenchAgent,
    ExcytinBenchWorkflow,
    ReadOnlySQLValidator,
)

INITIAL_TTT = """
response_type: TTT_PLAN
ttt:
  root_nodes:
    - title: Investigate the alert
      status: todo
      children:
        - title: Validate the network evidence
          status: todo
          children:
            - title: Query the alert evidence
              status: todo
              metadata:
                candidate_tables: [AlertEvidence]
                semantic_memory_refs: [table:AlertEvidence]
              children: []
"""


UPDATED_TTT = """
response_type: TTT_UPDATE
ttt:
  root_nodes:
    - title: Investigate the alert
      status: done
      children:
        - title: Validate the network evidence
          status: done
          children:
            - title: Query the alert evidence
              status: done
              metadata:
                candidate_tables: [AlertEvidence]
                semantic_memory_refs: [table:AlertEvidence]
              children: []
"""


def role_llm(system_prompt: str, user_prompt: str) -> str:
    if system_prompt.startswith("你是 ExCyTIn-Bench 调查流程中的 Executor"):
        assert '"allowed_memory_types": [\n      "semantic"' in user_prompt
        assert "procedural_memory" not in user_prompt
        assert "episodic_memory" not in user_prompt
        assert "reason_summary" not in system_prompt
        return """
response_type: EXECUTE_SQL
sql: SELECT AlertId FROM AlertEvidence LIMIT 5
"""
    if system_prompt.startswith("你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer"):
        assert '"allowed_memory_types": [\n      "semantic"' in user_prompt
        assert "episodic_memory" not in user_prompt
        return """
response_type: ROUND_REVIEW
decision: ready_to_submit
findings: [The queried alert identifier is present.]
gaps: []
recommendations: [Submit the observed identifier.]
answer_facts: [alert-1]
"""
    if "最终答案整理器" in system_prompt:
        return """
response_type: FINAL_ANSWER
answer: alert-1
"""
    if "上一轮" in user_prompt or "Review" in user_prompt:
        assert "procedural_memory" in user_prompt
        assert "episodic_memory" not in user_prompt
        return UPDATED_TTT
    assert "procedural_memory" in user_prompt
    assert "episodic_memory" not in user_prompt
    return INITIAL_TTT


def test_default_workflow_and_agent_limits() -> None:
    workflow = ExcytinBenchWorkflow(llm=role_llm)
    agent = ExcytinBenchAgent(workflow=workflow)

    assert workflow.max_steps == 25
    assert workflow.max_executor_attempts_per_l3 == 3
    assert agent.max_steps == 25


def test_external_action_workflow_waits_for_environment_observation() -> None:
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=role_llm)

    first = workflow.act("Alert context. Which alert id is involved?")
    assert first.action_type == BenchmarkActionType.QUERY
    assert first.content == "SELECT AlertId FROM AlertEvidence LIMIT 5;"
    assert workflow.state is not None
    assert workflow.state.executions == []
    assert workflow.state.ttt.root_nodes[0].children[0].children[0].metadata == {}

    second = workflow.act("[('alert-1',)]")
    assert second.action_type == BenchmarkActionType.SUBMIT
    assert second.content == "alert-1"

    logging = workflow.get_logging()
    assert logging["actions_issued"] == 2
    assert logging["executions"][0]["observation"] == "[('alert-1',)]"
    assert logging["reviews"][0]["decision"] == "ready_to_submit"
    assert len(logging["ttt_history"]) == 1
    assert any(
        item["event_type"] == "investigation_loop_exit_scheduled"
        and item["payload"]["reason"] == "ready_to_submit"
        for item in logging["trace"]
    )
    assert logging["memory_retrievals"][0]["allowed_memory_types"] == [
        "procedural",
        "semantic",
    ]
    assert all(
        item["allowed_memory_types"] != ["procedural", "semantic", "episodic"]
        for item in logging["memory_retrievals"]
    )


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
        if system_prompt.startswith("你是 ExCyTIn-Bench 调查流程中的 Executor"):
            calls["executor"] += 1
            if calls["executor"] == 1:
                assert "episodic_memory" not in user_prompt
            else:
                assert "episodic_memory" in user_prompt
                assert "Unknown column" in user_prompt
            return """
response_type: EXECUTE_SQL
sql: SELECT AlertId FROM AlertEvidence LIMIT 1
"""
        if system_prompt.startswith("你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer"):
            calls["reviewer"] += 1
            return """
response_type: ROUND_REVIEW
decision: continue
findings: []
gaps: [Unknown column must be repaired.]
recommendations: [Retry the current evidence goal.]
answer_facts: []
"""
        if "最终答案整理器" in system_prompt:
            return "response_type: FINAL_ANSWER\nanswer: unknown"
        if "上一轮" in user_prompt or "Review" in user_prompt:
            return INITIAL_TTT
        return INITIAL_TTT

    workflow = ExcytinBenchWorkflow(max_steps=4, llm=retry_llm)
    first = workflow.act("Investigate the alert")
    assert calls["executor"] == 1
    second = workflow.act("ProgrammingError: Unknown column 'AlertId'")
    assert first.action_type == second.action_type == BenchmarkActionType.QUERY
    assert calls["executor"] == 2
    assert calls["reviewer"] == 0
    assert workflow.get_logging()["executions"][0]["result_status"] == "error"


def test_empty_result_retries_same_l3_with_episodic_memory() -> None:
    calls = {"executor": 0, "reviewer": 0}

    def retry_llm(system_prompt: str, user_prompt: str) -> str:
        if system_prompt.startswith("你是 ExCyTIn-Bench 调查流程中的 Executor"):
            calls["executor"] += 1
            if calls["executor"] == 1:
                assert "episodic_memory" not in user_prompt
                sql = "SELECT AccountUpn FROM AlertEvidence WHERE AccountUpn = 'u141@example.com'"
            else:
                assert "episodic_memory" in user_prompt
                assert '"result_status": "empty_result"' in user_prompt
                assert "不要只删除次要过滤条件" in user_prompt
                sql = "SELECT CompromisedEntity FROM SecurityAlert WHERE ExtendedProperties LIKE '%u141%'"
            return f'response_type: EXECUTE_SQL\nsql: "{sql}"'
        if system_prompt.startswith("你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer"):
            calls["reviewer"] += 1
            return """
response_type: ROUND_REVIEW
decision: ready_to_submit
findings: [The host is supported by SecurityAlert evidence.]
gaps: []
recommendations: [Submit the observed host.]
answer_facts: [host-1]
"""
        if "最终答案整理器" in system_prompt:
            return "response_type: FINAL_ANSWER\nanswer: host-1"
        if "上一轮" in user_prompt or "Review" in user_prompt:
            return UPDATED_TTT
        return INITIAL_TTT

    workflow = ExcytinBenchWorkflow(max_steps=5, llm=retry_llm)
    first = workflow.act("Find the host for the automated investigation")
    second = workflow.act("[]")

    assert first.action_type == second.action_type == BenchmarkActionType.QUERY
    assert "AlertEvidence" in first.content
    assert "SecurityAlert" in second.content
    assert calls == {"executor": 2, "reviewer": 0}
    logging = workflow.get_logging()
    assert logging["executions"][0]["query_success"] is True
    assert logging["executions"][0]["result_status"] == "empty_result"
    assert any(
        item["event_type"] == "executor_empty_repair_scheduled"
        for item in logging["trace"]
    )

    final = workflow.act("[('host-1',)]")
    assert final.action_type == BenchmarkActionType.SUBMIT
    assert calls["reviewer"] == 1


def test_executor_attempts_are_capped_at_three_per_l3() -> None:
    calls = {"executor": 0, "reviewer": 0, "planner": 0}

    def capped_llm(system_prompt: str, user_prompt: str) -> str:
        if system_prompt.startswith("你是 ExCyTIn-Bench 调查流程中的 Executor"):
            calls["executor"] += 1
            if calls["executor"] == 1:
                assert "episodic_memory" not in user_prompt
            else:
                assert "episodic_memory" in user_prompt
            return "response_type: EXECUTE_SQL\nsql: SELECT AlertId FROM AlertEvidence"
        if system_prompt.startswith("你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer"):
            calls["reviewer"] += 1
            return """
response_type: ROUND_REVIEW
decision: cannot_continue
findings: []
gaps: [The L3 query could not retrieve evidence.]
recommendations: [Stop this investigation.]
answer_facts: []
"""
        if "最终答案整理器" in system_prompt:
            return "response_type: FINAL_ANSWER\nanswer: unknown"
        calls["planner"] += 1
        return INITIAL_TTT

    workflow = ExcytinBenchWorkflow(
        max_steps=10,
        llm=capped_llm,
        max_error_repairs=10,
        max_empty_repairs=10,
        max_executor_attempts_per_l3=3,
    )

    first = workflow.act("Investigate the alert")
    second = workflow.act("ProgrammingError: Unknown column 'AlertId'")
    third = workflow.act("[]")
    submit = workflow.act("ProgrammingError: Unknown column 'AlertId'")

    assert first.action_type == second.action_type == third.action_type
    assert first.action_type == BenchmarkActionType.QUERY
    assert submit.action_type == BenchmarkActionType.SUBMIT
    assert calls == {"executor": 3, "reviewer": 1, "planner": 1}
    logging = workflow.get_logging()
    assert logging["max_executor_attempts_per_l3"] == 3
    assert len(logging["executions"]) == 3
    assert any(
        item["event_type"] == "executor_attempt_budget_exhausted"
        and item["payload"]["attempt_count"] == 3
        for item in logging["trace"]
    )


def test_query_budget_exhaustion_skips_replanning_and_submits() -> None:
    calls = {"planner": 0, "executor": 0, "reviewer": 0, "answer": 0}

    def budget_llm(system_prompt: str, user_prompt: str) -> str:
        if system_prompt.startswith("你是 ExCyTIn-Bench 调查流程中的 Executor"):
            calls["executor"] += 1
            return "response_type: EXECUTE_SQL\nsql: SELECT CompromisedEntity FROM SecurityAlert"
        if system_prompt.startswith("你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer"):
            calls["reviewer"] += 1
            assert "必须输出 decision: ready_to_submit" in user_prompt
            return """
response_type: ROUND_REVIEW
decision: continue
findings: [The requested host is host-1.]
gaps: []
recommendations: []
answer_facts: [host-1]
"""
        if "最终答案整理器" in system_prompt:
            calls["answer"] += 1
            return "response_type: FINAL_ANSWER\nanswer: host-1"
        calls["planner"] += 1
        return INITIAL_TTT

    workflow = ExcytinBenchWorkflow(max_steps=2, llm=budget_llm)
    query = workflow.act("Which host is involved?")
    assert query.action_type == BenchmarkActionType.QUERY

    submit = workflow.act("[('host-1',)]")
    assert submit.action_type == BenchmarkActionType.SUBMIT
    assert submit.content == "host-1"
    assert calls == {"planner": 1, "executor": 1, "reviewer": 1, "answer": 1}
    assert any(
        item["event_type"] == "investigation_loop_exit_scheduled"
        and item["payload"]["reason"] == "query_budget_exhausted"
        for item in workflow.get_logging()["trace"]
    )
