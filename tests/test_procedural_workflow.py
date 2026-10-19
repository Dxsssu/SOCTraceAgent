# Historical transport fixtures use the pre-bootstrap adapter; default G/E-first execution is covered by test_staged_memory_planning.py.
import json
from copy import deepcopy

import pytest
import sqlglot
import yaml

from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import (
    ProceduralPlanningSession,
    literal_sql,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
    normalize_sql,
)
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import (
    SemanticPlanningSession,
)

SCHEMA = {
    "EmailEvents": {
        "NetworkMessageId": "text",
        "SenderFromAddress": "text",
        "TimeGenerated": "text",
    }
}
QUERY = {
    "known": [{"concept": "Email_ID", "kind": "network_message_id"}],
    "wanted": ["Email_Sender"],
    "intent": "查询发件人",
}


def session(schema=SCHEMA):
    return ProceduralPlanningSession(
        SemanticPlanningSession(SemanticMemoryV2.load(schema=schema))
    )


def initial(task):
    return {
        "operations": [
            {"op": "add_goal", "ref": "g", "objective": "Find sender"},
            {
                "op": "add_entity",
                "ref": "e",
                "goal_id": "$g",
                "objective": "NetworkMessageId mail-1",
            },
            {
                "op": "add_task",
                "entity_id": "$e",
                "objective": "查邮件 mail-1 的发件人",
                **task,
            },
        ]
    }


def test_default_workflow_uses_retrieved_template_and_repairs_drift():
    calls = []
    sql_attempts = []

    def llm(system, user):
        data = yaml.safe_load(user.split("\n上一次输出")[0])
        calls.append(data)
        if "episodic_context" in data:
            sql_attempts.append(data)
            return yaml.safe_dump(
                {
                    "sql": "SELECT SenderFromAddress FROM EmailEvents"
                    if len(sql_attempts) == 1
                    else data["episodic_context"]["bound_sql"]
                }
            )
        if "episodic_results" in data:
            if not data["episodic_results"]:
                return yaml.safe_dump({"memory_query": QUERY})
            candidate = data["episodic_results"][-1]["candidates"][0]
            return yaml.safe_dump(
                initial(
                    {
                        "episode": {
                            "id": candidate["id"],
                            "step": "s1",
                            "inputs": {"p1": ["mail-1"]},
                        }
                    }
                )
            )
        if "你是 Reviewer" in system:
            return yaml.safe_dump(
                {
                    "summary": "查到发件人",
                    "facts": [{"sender": "a@test"}],
                    "limitations": [],
                }
            )
        return yaml.safe_dump({"can_answer": "yes", "answer": "a@test"})

    w = ExcytinBenchWorkflow(staged_planning=False, llm=llm)
    w.set_semantic_schema(SCHEMA)
    w.start({"question": "Find sender", "context": "mail-1", "answer": "DO_NOT_LEAK"})
    action = w.propose_next_action()
    assert "IN ('mail-1')" in action.content
    assert len(sql_attempts) == 2
    assert not w.get_logging()["memory_retrievals"]
    assert w.get_logging()["planner_pm_enabled"]
    assert w.get_logging()["planner_em_enabled"]
    assert not w.get_logging()["task_procedural_contexts"]
    assert w.get_logging()["task_episodic_contexts"]["T1"]["memory_type"] == "episodic"
    assert "DO_NOT_LEAK" not in json.dumps(calls)
    w.accept_observation("[('mail-1', 'a@test')]", query_success=True)
    assert w.propose_next_action().submit
    assert w._procedural_session.runs["T1"]["observations"]["s1"]["rows"] == [
        {"NetworkMessageId": "mail-1", "SenderFromAddress": "a@test"}
    ]
    w.reset()
    assert w.get_logging()["procedural_retrievals"] == []


def test_rejected_candidate_falls_back_to_semantic_in_workflow():
    replies = iter(
        [
            {"memory_query": QUERY},
            {"memory_query": QUERY},
            initial({"table": "EmailEvents"}),
            {
                "sql": "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId='mail-1'"
            },
        ]
    )
    w = ExcytinBenchWorkflow(staged_planning=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.set_semantic_schema(SCHEMA)
    w.start("查发件人")
    assert w.propose_next_action().content.startswith("SELECT")
    log = w.get_logging()
    assert len(log["procedural_retrievals"]) == 2 and len(log["memory_retrievals"]) == 1
    assert not log["task_procedural_contexts"]


def test_schema_miss_and_strict_message_id_kind_do_not_force_template():
    assert session({}).retrieve(QUERY)["route"] == "semantic"
    wrong = deepcopy(QUERY)
    wrong["known"][0]["kind"] = "internet_message_id"
    assert session().retrieve(wrong)["route"] == "semantic"


def test_ast_value_rendering_preserves_injection_text_as_literal():
    t, b = normalize_sql(
        "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId='x'", SCHEMA
    )
    b["p1"] = "mail' OR 1=1; -- \\test"
    sql = literal_sql(t, b)
    tree = sqlglot.parse_one(sql, read="mysql")
    assert tree.args["where"].this.expression.this == b["p1"]
    assert len(sqlglot.parse(sql, read="mysql")) == 1


def test_live_chain_uses_observed_upstream_and_rejects_failed_or_invented_values():
    # Use the released three-step fork, keeping only the fields its SQL actually needs.
    p = session()
    sid = "proc_3cce7d309146babc5a846c3e"
    # Dependency-contract fixture deliberately exercises an archived deployment flow.
    p.disabled.discard(sid)
    snippet = p.store.snippets[sid]
    schema = {}
    for step in snippet["steps"]:
        for table, fields in p.store.templates[step["template_id"]][
            "schema_fields"
        ].items():
            schema.setdefault(table, {}).update({f: "text" for f in fields})
    p.semantic.memory.schema = schema
    p.offered[sid] = snippet
    context = p.bind(
        {"id": sid, "step": "s1", "inputs": {"p1": "%quarantine%", "p2": "%remove%"}}
    )
    p.register("T7", context)
    with pytest.raises(ValueError, match="dependency"):
        p.bind({"id": sid, "step": "s2", "run": "T7"})
    p.observe("T7", "[('mail-a', 'network-a'), ('mail-b', 'network-b')]", True)
    with pytest.raises(ValueError, match="ambiguous"):
        p.bind({"id": sid, "step": "s2", "run": "T7"})
    with pytest.raises(ValueError, match="not_supported"):
        p.bind(
            {
                "id": sid,
                "step": "s2",
                "run": "T7",
                "selections": {"s1.InternetMessageId": ["invented"]},
            }
        )
    bound = p.bind(
        {
            "id": sid,
            "step": "s2",
            "run": "T7",
            "selections": {"s1.InternetMessageId": ["mail-b"]},
        }
    )
    assert "'mail-b'" in bound["bound_sql"] and "network-b" not in bound["bound_sql"]
    p.observe("T7", "[]", True)
    with pytest.raises(ValueError, match="no_values"):
        p.bind({"id": sid, "step": "s2", "run": "T7"})
    p.observe("T7", "[('mail-a', 'network-a')] ... (Truncated)", True)
    with pytest.raises(ValueError, match="truncated"):
        p.bind({"id": sid, "step": "s2", "run": "T7"})
    p.observe("T7", "SQL error", False)
    with pytest.raises(ValueError, match="dependency"):
        p.bind({"id": sid, "step": "s2", "run": "T7"})


def test_failed_template_hands_control_to_planner_instead_of_rewriting_memory():
    pm = session()
    hit = pm.retrieve(QUERY)["candidates"][0]
    spec = {"id": hit["id"], "step": "s1", "inputs": {"p1": ["mail-1"]}}
    sql = pm.bind(spec)["bound_sql"]
    replies = iter(
        [
            {"memory_query": QUERY},
            initial({"episode": spec}),
            {"sql": sql},
            {"summary": "字段查询失败", "facts": [], "limitations": ["unknown column"]},
            {"can_answer": "no"},
            {"memory_query": QUERY},
            {
                "operations": [
                    {
                        "op": "add_task",
                        "entity_id": "E1",
                        "table": "EmailEvents",
                        "objective": "根据当前字段重新查询邮件发件人",
                    }
                ]
            },
        ]
    )
    w = ExcytinBenchWorkflow(staged_planning=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.set_semantic_schema(SCHEMA)
    w.start("Find sender")
    w.propose_next_action()
    w.accept_observation("ProgrammingError: Unknown column", query_success=False)
    assert not w.state.error_repair_counts
    assert "T2" in w.get_logging()["task_semantic_contexts"]
    assert not w._procedural_session.runs["T1"]["observations"]["s1"]["success"]


def test_table_choice_after_episodic_candidates_automatically_uses_semantic():
    replies = iter(
        [
            {"memory_query": QUERY},
            initial({"table": "EmailEvents"}),
            {
                "sql": "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId='mail-1'"
            },
        ]
    )
    w = ExcytinBenchWorkflow(staged_planning=False, llm=lambda s, u: yaml.safe_dump(next(replies)))
    w.set_semantic_schema(SCHEMA)
    w.start("Find sender")
    w.propose_next_action()
    log = w.get_logging()
    assert len(log["memory_retrievals"]) == 1
    assert (
        log["procedural_fallbacks"][0]["reason"]
        == "planner_selected_table_instead_of_template"
    )
    assert "T1" in log["task_semantic_contexts"]


def test_workflow_replan_continues_same_run_using_raw_results(monkeypatch):
    from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import (
        TrajectorySnippetStore,
    )
    from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
        digest,
    )

    pm = session()
    sid = "proc_3cce7d309146babc5a846c3e"
    snippet = pm.store.snippets[sid]
    templates = {
        step["template_id"]: pm.store.templates[step["template_id"]]
        for step in snippet["steps"]
    }
    doc = {
        "format_version": "trajectory-snippets-2",
        "snippets": [snippet],
        "templates": templates,
    }
    doc["catalog_id"] = digest(doc)
    store = TrajectorySnippetStore(doc)
    monkeypatch.setattr("src.memory.longterm_memory.procedural_memory.flows.load_runtime_catalog", lambda: store)
    schema = {}
    for template in templates.values():
        for table, fields in template["schema_fields"].items():
            schema.setdefault(table, {}).update({f: "text" for f in fields})
    phase = 0

    def llm(system, user):
        nonlocal phase
        data = yaml.safe_load(user)
        if "procedural_context" in data:
            return yaml.safe_dump({"sql": data["procedural_context"]["bound_sql"]})
        if "你是 Reviewer" in system:
            return yaml.safe_dump(
                {
                    "summary": "找到邮件标识",
                    "facts": [{"InternetMessageId": "observed-mail"}],
                    "limitations": [],
                }
            )
        if "你是 Verifier" in system:
            return "can_answer: no"
        phase += 1
        if phase == 1:
            return yaml.safe_dump(
                {
                    "memory_query": {
                        "known": ["Action"],
                        "wanted": ["Email_Sender", "URL"],
                        "intent": "查询隔离邮件的发件人和URL",
                    }
                }
            )
        if phase == 2:
            return yaml.safe_dump(
                initial(
                    {
                        "procedure": {
                            "id": sid,
                            "step": "s1",
                            "inputs": {"p1": "%quarantine%", "p2": "%remove%"},
                        }
                    }
                )
            )
        return yaml.safe_dump(
            {
                "operations": [
                    {
                        "op": "add_task",
                        "entity_id": "E1",
                        "objective": "根据本次返回的邮件ID查询发件人",
                        "procedure": {
                            "id": sid,
                            "step": "s2",
                            "run": "T1",
                            "inputs": {},
                        },
                    }
                ]
            }
        )

    w = ExcytinBenchWorkflow(staged_planning=False, llm=llm)
    w.set_semantic_schema(schema)
    w.start("查隔离邮件发件人")
    w.propose_next_action()
    w.accept_observation("[('observed-mail', 'observed-network')]", query_success=True)
    action = w.propose_next_action()
    assert "IN ('observed-mail')" in action.content
    assert "observed-network" not in action.content
    assert w.get_logging()["task_procedural_contexts"]["T2"]["run"] == "T1"
    assert not w.get_logging()["memory_retrievals"]


def test_selected_table_on_later_semantic_page_is_not_rejected():
    p = session(schema=None)
    query = {
        "known": ["IP", "Application"],
        "wanted": ["Event_Time", "Account_Name", "Login_Time"],
        "intent": "SAP authentication 登录时间",
    }
    p.retrieve(query)
    p.semantic.retrieve(query)
    assert "DeviceNetworkEvents" not in {
        c["table"] for c in p.semantic.retrievals[-1]["result"]["candidates"]
    }
    _, contexts = p.prepare(
        {
            "operations": [
                {
                    "op": "add_task",
                    "entity_id": "E1",
                    "table": "DeviceNetworkEvents",
                    "objective": "查询 IP 的认证时间",
                }
            ]
        }
    )
    assert contexts[0]["table"] == "DeviceNetworkEvents"
    assert any(f["reason"] == "selected_table_pagination" for f in p.fallbacks)
    count = len(p.semantic.retrievals)
    p.prepare(
        {
            "operations": [
                {
                    "op": "add_task",
                    "entity_id": "E1",
                    "table": "DeviceNetworkEvents",
                    "objective": "再次查询",
                }
            ]
        }
    )
    assert len(p.semantic.retrievals) == count
    with pytest.raises(ValueError, match="not an eligible"):
        p.prepare(
            {
                "operations": [
                    {
                        "op": "add_task",
                        "entity_id": "E1",
                        "table": "InventedTable",
                        "objective": "query",
                    }
                ]
            }
        )


def test_pm_only_trigger_concepts_are_visible_without_full_catalog():
    p = session()
    assert "Title" not in p.semantic.vocabulary()
    assert "Title" in p.vocabulary() and "Alert_ID" in p.vocabulary()
    assert "sql" not in json.dumps(p.vocabulary())


def test_pm_decline_can_fallback_to_alert_evidence():
    hybrid = session({'AlertEvidence': {'AlertId': 'TEXT', 'DeviceName': 'TEXT',
        'Title': 'TEXT', 'TimeGenerated': 'TEXT', 'EvidenceRole': 'TEXT'}})
    hybrid.retrieve({'known': ['Alert_ID'], 'wanted': ['Device_Name'], 'intent': '查询告警关联设备'})
    payload = initial({'table': 'AlertEvidence'})
    payload['operations'][-1]['objective'] = '按 AlertId 查询 DeviceName 和 EvidenceRole'
    _, contexts = hybrid.prepare(payload)
    assert contexts[0]['table'] == 'AlertEvidence'
    assert {'AlertId', 'DeviceName', 'EvidenceRole'} <= {f['field'] for f in contexts[0]['fields']}
