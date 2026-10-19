import json

import pytest
import yaml

from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow

QUERY = {"memory_query": {"known": [{"concept": "邮件ID", "kind": "network_message_id"}],
                            "wanted": [{"concept": "发件人", "kind": "header_from"}]}}


def plan(table="EmailEvents", probe=False):
    task = {"op": "add_task", "entity_id": "$mail", "objective": "Find sender for NetworkMessageId mail-1"}
    if probe:
        task["schema_probe"] = True
    else:
        task["table"] = table
    return {"operations": [
        {"op": "add_goal", "ref": "goal", "objective": "Find sender"},
        {"op": "add_entity", "ref": "mail", "goal_id": "$goal", "objective": "mail-1"}, task]}


def test_default_workflow_retrieves_selects_passes_context_and_finishes():
    calls = []
    replies = iter([QUERY, plan(), {"sql": "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = 'mail-1'"},
                    {"summary": "Found sender", "facts": [{"sender": "a@example.test"}], "limitations": []},
                    {"can_answer": "yes", "answer": "a@example.test"}])
    def llm(system, user):
        calls.append((system, yaml.safe_load(user)))
        return yaml.safe_dump(next(replies))
    workflow = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=llm)
    workflow.set_semantic_schema({"EmailEvents": {"NetworkMessageId": "text", "SenderFromAddress": "text", "TimeGenerated": "text", "SecretUnused": "text"}})
    workflow.start({"context": "Known mail-1", "question": "Find sender", "answer": "FORBIDDEN_REFERENCE"})
    assert "memory_query" not in json.dumps(calls[0][1].get("semantic_results"))
    assert "EmailEvents" not in json.dumps(calls[0][1])
    assert "EmailEvents" in json.dumps(calls[1][1]["semantic_results"])
    action = workflow.propose_next_action()
    context = calls[2][1]["semantic_context"]
    assert context["table"] == "EmailEvents" and context["schema_scope"] == "target_snapshot"
    assert {r["field"] for r in context["fields"]} == {"NetworkMessageId", "SenderFromAddress"}
    assert "FORBIDDEN_REFERENCE" not in json.dumps(calls)
    assert "SecretUnused" not in json.dumps(calls)
    workflow.accept_observation("[('a@example.test',)]", query_success=True)
    assert workflow.propose_next_action().submit
    log = workflow.get_logging()
    assert log["semantic_memory_v2_enabled"] and not log["planner_pm_enabled"]
    assert len(log["memory_retrievals"]) == 1 and log["actions_issued"] == 2
    assert "SenderFromAddress" in action.content


def test_bad_table_gets_validation_feedback_before_execution():
    replies = iter([QUERY, plan("InventedTable"), plan()])
    inputs = []
    def llm(system, user):
        inputs.append(user)
        return yaml.safe_dump(next(replies))
    workflow = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=llm)
    workflow.start("Find sender")
    assert "not an eligible retrieved table" in inputs[-1]
    assert workflow.get_logging()["task_semantic_contexts"]["T1"]["table"] == "EmailEvents"
    assert workflow.state.actions_issued == 0


def test_repair_keeps_same_semantic_context():
    replies = iter([QUERY, plan(), {"sql": "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = 'mail-1'"},
                    {"sql": "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = 'mail-1' LIMIT 1"}])
    inputs = []
    def llm(system, user):
        inputs.append(json.loads(user))
        return yaml.safe_dump(next(replies))
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=llm)
    w.start("Find sender")
    w.propose_next_action()
    w.accept_observation("SQL failure", query_success=False)
    w.propose_next_action()
    assert inputs[-1]["semantic_context"] == inputs[-2]["semantic_context"]
    assert "error" in inputs[-1] and len(w.get_logging()["memory_retrievals"]) == 1


def test_replan_preserves_old_bindings():
    replanned = {"operations": [{"op": "add_task", "entity_id": "E1", "objective": "Find sender again",
        "semantic_refs": [{"table": "EmailEvents", "fields": ["NetworkMessageId", "SenderFromAddress"]}]}]}
    replies = iter([QUERY, plan(), {"sql": "SELECT SenderFromAddress FROM EmailEvents"},
                    {"summary": "No result", "facts": [], "limitations": ["empty"]},
                    {"can_answer": "no"}, replanned])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start("Find sender")
    w.propose_next_action()
    w.accept_observation("[]", query_success=True)
    bindings = w.get_logging()["task_semantic_contexts"]
    assert bindings["T1"] == bindings["T2"]
    assert len(w.get_logging()["memory_retrievals"]) == 1
    w.reset()
    assert w.get_logging()["memory_retrievals"] == []


def test_schema_probe_cannot_query_business_data():
    replies = iter([QUERY, plan(probe=True), {"sql": "SELECT * FROM EmailEvents"},
                    {"sql": "SHOW COLUMNS FROM EmailEvents"}])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start("Inspect schema")
    assert w.propose_next_action().content == "SHOW COLUMNS FROM EmailEvents;"
    assert w.state.actions_issued == 1
    assert any(t["event_type"] == "executor_sql_invalid" for t in w.state.trace)


def test_retrieval_loop_has_a_budget():
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(QUERY))
    with pytest.raises(ValueError, match="exceeded 5"):
        w.start("Find sender")
    assert len(w.get_logging()["memory_retrievals"]) == 5


def test_context_ids_follow_creation_order_not_tree_order():
    initial = plan()
    initial["operations"].insert(2, {"op": "add_entity", "ref": "mail2", "goal_id": "$goal", "objective": "mail-2"})
    initial["operations"][3]["entity_id"] = "$mail2"
    second = {"op": "add_task", "entity_id": "$mail", "objective": "Other mail sender",
              "semantic_refs": [{"table": "EmailAttachmentInfo", "fields": ["NetworkMessageId", "SenderFromAddress"]}]}
    initial["operations"].append(second)
    replies = iter([QUERY, initial])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start("Find sender")
    assert w.get_logging()["task_semantic_contexts"]["T1"]["table"] == "EmailEvents"
    assert w.get_logging()["task_semantic_contexts"]["T2"]["table"] == "EmailAttachmentInfo"


def test_question_intent_supplies_fallback_and_table_only_selection():
    q = {"memory_query": {"known": ["Source_IP"], "wanted": ["Login_Time"]}}
    selected = plan("SigninLogs")
    selected["operations"][-1]["objective"] = "查询已知 IP 的认证时间"
    replies = iter([q, selected, {"sql": "SELECT CreatedDateTime FROM SigninLogs WHERE IPAddress = '192.0.2.1'"}])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start({"question": "When did the SAP authentication occur?", "context": "IP 192.0.2.1"})
    assert "SigninLogs" in w.propose_next_action().content
    result = w.get_logging()["memory_retrievals"][0]["result"]
    assert result["candidates"][0]["table"] == "SigninLogs"


def test_partial_selection_reaches_executor_without_full_coverage_gate():
    query = {"memory_query": {"known": [{"concept": "URL", "kind": "clicked_url"}],
        "wanted": ["Email_Sender", {"concept": "Email_ID", "kind": "network_message_id"}]}}
    replies = iter([query, plan("UrlClickEvents"), {"sql": "SELECT NetworkMessageId FROM UrlClickEvents WHERE Url = 'https://example.test'"}])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start("Find mail sender via clicked URL")
    assert "UrlClickEvents" in w.propose_next_action().content
    assert w.get_logging()["task_semantic_contexts"]["T1"]["capability"] == "partial"


def test_sql_cannot_switch_selected_table_even_when_task_prose_names_other_table():
    selected = plan()
    selected["operations"][-1]["objective"] = "Read EmailAttachmentInfo"
    replies = iter([QUERY, selected,
        {"sql": "SELECT SenderFromAddress FROM EmailAttachmentInfo"},
        {"sql": "SELECT SenderFromAddress FROM EmailEvents"}])
    w = ExcytinBenchWorkflow(procedural_memory_enabled=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.start("Find sender")
    assert w.propose_next_action().content == "SELECT SenderFromAddress FROM EmailEvents;"
    assert any("selected table" in t.get("payload", {}).get("error", "") for t in w.state.trace)


def test_retrieving_additional_concepts_merges_only_selected_table_fields():
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    session = SemanticPlanningSession(SemanticMemoryV2.load())
    session.retrieve(QUERY["memory_query"])
    session.retrieve({"known": [{"concept": "Email_ID", "kind": "network_message_id"}], "wanted": ["Email_Subject"]})
    clean, contexts = session.prepare(plan())
    assert "table" not in clean["operations"][-1]
    assert {f["field"] for f in contexts[0]["fields"]} == {"NetworkMessageId", "SenderFromAddress", "Subject"}
    assert contexts[0]["lookup_ids"] == ["SM1", "SM2"]


@pytest.mark.parametrize("sql", [
    "SELECT MysteryColumn FROM EmailEvents",
    "SELECT * FROM EmailEvents",
    "SELECT e.SenderFromAddress FROM EmailEvents e JOIN EmailAttachmentInfo a ON e.NetworkMessageId=a.NetworkMessageId",
    "SELECT SenderFromAddress FROM other_database.EmailEvents",
])
def test_sql_binding_rejects_unretrieved_fields_tables_and_star(sql):
    from src.memory.longterm_memory.semantic_memory.v2.session import validate_sql_binding, SemanticPlanningSession
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    session = SemanticPlanningSession(SemanticMemoryV2.load())
    session.retrieve(QUERY["memory_query"])
    _, contexts = session.prepare(plan())
    with pytest.raises(ValueError):
        validate_sql_binding(sql, contexts[0])


@pytest.mark.parametrize("sql", [
    "SELECT e.SenderFromAddress AS sender FROM EmailEvents e ORDER BY sender",
    "SELECT COUNT(*) AS n FROM EmailEvents",
    "WITH mail AS (SELECT SenderFromAddress FROM EmailEvents) SELECT SenderFromAddress FROM mail",
    "SELECT SenderFromAddress FROM env_monitor_db.EmailEvents",
])
def test_sql_binding_handles_aliases_aggregation_and_ctes(sql):
    from src.memory.longterm_memory.semantic_memory.v2.session import validate_sql_binding, SemanticPlanningSession
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    session = SemanticPlanningSession(SemanticMemoryV2.load())
    session.retrieve(QUERY["memory_query"])
    _, contexts = session.prepare(plan())
    validate_sql_binding(sql, contexts[0])


def test_task_explicit_fields_are_resolved_from_catalog_without_full_schema():
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession, validate_sql_binding
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    session = SemanticPlanningSession(SemanticMemoryV2.load(schema={"DeviceProcessEvents": {
        "ProcessCommandLine": "text", "ProcessId": "text", "DeviceId": "varchar(64)", "Uncatalogued": "text"}}))
    session.retrieve({"known": ["Process_Command"], "wanted": ["Process_ID"]})
    payload = {"operations": [{"op": "add_task", "entity_id": "E1", "table": "DeviceProcessEvents",
        "objective": "返回 DeviceId、DeviceName、Uncatalogued，将进程按主机关联"}]}
    _, contexts = session.prepare(payload)
    context = contexts[0]
    assert context["task_resolved_fields"] == ["DeviceId"]
    fields = {r["field"]: r for r in context["fields"]}
    assert fields["DeviceId"]["type"] == "varchar(64)" and fields["DeviceId"]["use"] == "task"
    assert "DeviceName" not in fields and "Uncatalogued" not in fields
    validate_sql_binding("SELECT DeviceId, ProcessId FROM DeviceProcessEvents", context)
    with pytest.raises(ValueError):
        validate_sql_binding("SELECT Uncatalogued FROM DeviceProcessEvents", context)


def test_omitted_table_is_inferred_only_from_unique_exact_retrieved_name():
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    session = SemanticPlanningSession(SemanticMemoryV2.load())
    session.retrieve(QUERY["memory_query"])
    def request(objective):
        return {"operations": [{"op": "add_task", "entity_id": "E1", "objective": objective}]}
    _, contexts = session.prepare(request("查询 EmailEvents 的发件人"))
    assert contexts[0]["table"] == "EmailEvents"
    for objective in ["查询 EmailEventsFake", "查询 EmailEvents 和 EmailAttachmentInfo", "查询发件人"]:
        with pytest.raises(ValueError, match="Select one retrieved table"):
            session.prepare(request(objective))
