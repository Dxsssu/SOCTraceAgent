from pm_fakes import verify
import copy
import json
from unittest.mock import patch

import pytest
import yaml

from src.benchmarks.excytin_bench import ExcytinBenchWorkflow
from src.schema import Event
from src.schema.itt import parse_itt, itt_payload, parse_itt_response, planner_prompt
from src.schema.ttt_updates import change_node, next_task
from src.schema.ttt import TTTNodeStatus as S
from src.workflow.context import MemoryContextService, ExcytinBenchSnapshotRepository


def plan(suffix="1"):
    return [{"id": "G" + suffix, "objective": "Locate device", "status": "todo", "children": [
        {"id": "F" + suffix, "objective": "Alert evidence", "status": "todo", "children": [
            {"id": "T" + suffix, "objective": "Query device", "status": "todo",
             "semantic_refs": [], "execution_code": "", "execution_result": ""}]}]}]


def test_multi_goal_execution_order_and_history():
    tree = parse_itt(plan() + plan("2"), "event", 1)
    assert next_task(tree).node_id == "T1"
    tree = change_node(tree, "T1", status=S.DONE, evidence_refs=("E1",))
    executions = [{"node_id": "T1", "execution_id": "E1", "sql": "SELECT 1", "observation": "[(1,)]"}]
    payload = itt_payload(tree, executions)
    payload[0]["status"] = payload[0]["children"][0]["status"] = "done"
    updated = parse_itt(payload, "event", 2, previous=tree, executions=executions)
    assert next_task(updated).node_id == "T2"
    assert updated.version == tree.version + 1
    assert updated.root_nodes[0].children[0].children[0].metadata["execution_code"] == "SELECT 1"
    payload[0]["children"][0]["children"][0]["execution_result"] = "invented"
    with pytest.raises(ValueError, match="actual records"):
        parse_itt(payload, "event", 2, previous=tree, executions=executions)


@pytest.mark.parametrize("mutation", ["duplicate", "status", "empty_frontier", "extra_level", "fake_code", "done_without_execution", "old_format"])
def test_invalid_initial_itt(mutation):
    payload = plan()
    leaf = payload[0]["children"][0]["children"][0]
    if mutation == "duplicate": leaf["id"] = "G1"
    if mutation == "status": leaf["status"] = "open"
    if mutation == "empty_frontier": payload[0]["children"][0]["children"] = []
    if mutation == "extra_level": leaf["children"] = plan()
    if mutation == "fake_code": leaf["execution_code"] = "SELECT invented"
    if mutation == "done_without_execution": leaf["status"] = "done"
    if mutation == "old_format": payload = {"ttt": payload}
    with pytest.raises(ValueError):
        parse_itt(payload, "event", 1)


@pytest.mark.parametrize("mutation", ["delete", "rename", "move", "reopen"])
def test_update_preserves_paths_atomically(mutation):
    tree = parse_itt(plan() + plan("2"), "event", 1)
    tree = change_node(tree, "T1", status=S.BLOCKED)
    before = copy.deepcopy(tree.to_dict())
    payload = itt_payload(tree)
    if mutation == "delete": payload.pop(0)
    if mutation == "rename": payload[0]["objective"] = "changed"
    if mutation == "move":
        payload[1]["children"].append(payload[0]["children"][0])
        payload[0]["children"] = []
    if mutation == "reopen": payload[0]["children"][0]["children"][0]["status"] = "todo"
    with pytest.raises(ValueError):
        parse_itt(payload, "event", 2, previous=tree)
    assert tree.to_dict() == before


def test_planner_uses_sm_fallback_and_empty_pm_section():
    event = Event(event_name="ignored", message="Investigate alert", context={"memory_profile": "excytin_bench"})
    memory = MemoryContextService(procedure_selector=lambda *_: "[]").planner_view().build(event)
    assert memory["procedural_memory"]["procedures"] == []
    assert memory["semantic_memory"]["table_cards"] == []
    assert planner_prompt("system", memory) == "system\n# 适用的 Procedural Memory"


def test_runtime_metadata_excluded_from_every_model_call():
    calls = []
    def llm(system, prompt):
        if "你是 Verifier" in system: return verify(prompt)
        calls.append((system, prompt))
        assert "RUNTIME_SECRET" not in system + prompt
        assert "ANSWER_SECRET" not in system + prompt
        if system.startswith("你正在为当前 Agent"):
            task = json.loads(prompt)["task"]
            assert task["context"] == "Alert A"
            assert task["question"] == "Locate device"
            return "[]"
        if "中的 Planner" in system:
            instruction, content = prompt.split("\n", 1)
            assert instruction == "下面是此次事件的背景和调查问题，请你根据要求初始化并输出ITT。"
            assert json.loads(content) == {"context": "Alert A", "question": "Locate device"}
            return yaml.safe_dump(plan())
        if "你是 SQL 查询生成器" in system: return "sql: SELECT 1"
        if "你是 Reviewer" in system: return 'summary: device identified'
        return "answer: device"
    workflow = ExcytinBenchWorkflow(llm=llm)
    workflow.start({"context": "Alert A", "question": "Locate device", "answer": "ANSWER_SECRET", "runtime_info": "RUNTIME_SECRET"},
                   runtime_info={"attack": "RUNTIME_SECRET", "qid": "RUNTIME_SECRET"})
    workflow.propose_next_action()
    workflow.act("[(1,)]")
    assert workflow.state.event.context["runtime_info"]["qid"] == "RUNTIME_SECRET"
    assert len(calls) == 5  # No answer synthesizer; Verifier handled separately


def test_yaml_list_parser_rejects_wrappers():
    assert parse_itt_response(yaml.safe_dump(plan())) == plan()
    with pytest.raises(ValueError):
        parse_itt_response("ttt: []")


PM_REFS = [{"table": "DeviceProcessEvents", "fields": ["DeviceName", "ProcessCommandLine"]}]


def test_semantic_refs_survive_storage_and_replanning_for_executor(tmp_path):
    from src.agent.executor import ExecutorRuntime
    from src.memory.working_memory import TTTStore
    payload = plan()
    payload[0]["children"][0]["children"][0]["semantic_refs"] = copy.deepcopy(PM_REFS)
    tree = parse_itt(payload, "event", 1, procedures=[{"semantic_refs": PM_REFS}])
    store = TTTStore(tmp_path / "refs.db")
    store.save_snapshot(tree, expected_version=0)
    restored = store.get_latest_ttt("event")
    assert itt_payload(restored) == payload
    updated = parse_itt(itt_payload(restored), "event", 2, previous=restored)
    store.save_snapshot(updated, expected_version=restored.version)
    node = store.claim_next_todo_leaf("event", 2)
    assert node.metadata["semantic_refs"] == PM_REFS
    intent = ExecutorRuntime._build_tool_intent(Event(event_name="test", message="Find process command line"), node)
    assert '"DeviceProcessEvents"' in intent
    assert '"ProcessCommandLine"' in intent


@pytest.mark.parametrize("refs", [
    {"table": "DeviceProcessEvents", "fields": ["DeviceName"]},
    [{"table": "DeviceProcessEvents", "fields": "DeviceName"}],
    [{"table": "DeviceProcessEvents", "fields": []}],
])
def test_invalid_semantic_refs_rejected(refs):
    payload = plan()
    payload[0]["children"][0]["children"][0]["semantic_refs"] = refs
    with pytest.raises(ValueError, match="semantic_refs"):
        parse_itt(payload, "event", 1, procedures=[{"semantic_refs": PM_REFS}])


@pytest.mark.parametrize("procedures", [(), ({"semantic_refs": [{"table": "OtherTable", "fields": ["OtherField"]}]},)])
def test_nonempty_semantic_refs_do_not_require_pm_knowledge(procedures):
    payload = plan()
    payload[0]["children"][0]["children"][0]["semantic_refs"] = PM_REFS
    tree = parse_itt(payload, "event", 1, procedures=procedures)
    assert next_task(tree).metadata["semantic_refs"] == PM_REFS
    replanned = parse_itt(payload, "event", 2, previous=parse_itt(plan(), "event", 1), procedures=procedures)
    assert next_task(replanned).metadata["semantic_refs"] == PM_REFS


def test_benchmark_planner_passes_pm_knowledge_to_executor():
    def llm(system, prompt):
        if "你是 Verifier" in system: return verify(prompt)
        if "中的 Planner" in system:
            payload = plan()
            payload[0]["children"][0]["children"][0]["semantic_refs"] = PM_REFS
            return yaml.safe_dump(payload)
        if "你是 SQL 查询生成器" in system:
            assert '"semantic_refs"' in prompt
            assert '"ProcessCommandLine"' in prompt
            return "sql: SELECT ProcessCommandLine FROM DeviceProcessEvents LIMIT 1"
        return "[]"
    service = MemoryContextService(procedure_selector=llm)
    pm = {"name": "Process lookup", "status": "active", "goal": "Find process command line",
          "input_slots": {}, "operations": "Read process events", "semantic_refs": PM_REFS}
    with patch.object(ExcytinBenchSnapshotRepository, "retrieve_procedures", return_value=[pm]):
        workflow = ExcytinBenchWorkflow(llm=llm, memory_contexts=service)
        workflow.start({"context": "device A", "question": "What is the process command line?"})
    action = workflow.propose_next_action()
    assert "ProcessCommandLine" in action.content


def test_background_planner_accepts_pm_refs():
    from src.agent.planner import PlannerRuntime
    payload = plan()
    payload[0]["children"][0]["children"][0]["semantic_refs"] = PM_REFS
    class Context:
        def build(self, *args, **kwargs):
            return {"procedural_memory": {"procedures": [{"semantic_refs": PM_REFS}]}}
    with patch("src.agent.planner.call_llm", return_value=yaml.safe_dump(payload)):
        tree = PlannerRuntime(context_provider=Context())._generate_initial_ttt(
            Event(event_name="test", message="What is the process command line?"))
    assert next_task(tree).metadata["semantic_refs"] == PM_REFS


def test_planner_template_without_memory_fields_accepts_initial_plan():
    from src.agent.planner import PLANNER_SYSTEM_PROMPT
    payload = parse_itt_response(("- id: G1" + PLANNER_SYSTEM_PROMPT.split("\n- id: G1", 1)[1]).replace("todo｜done｜error", "todo"))
    tree = parse_itt(payload, "event", 1)
    assert next_task(tree).node_id == "T1"
    assert next_task(tree).metadata["semantic_refs"] == []


def test_omitted_memory_fields_preserve_existing_task_metadata():
    template = {"id": "pm-test", "inputs": [], "sql_template": "SELECT 1",
                "returns": [], "semantic_refs": PM_REFS}
    payload = plan()
    payload[0]["children"][0]["children"][0]["template_id"] = template["id"]
    tree = parse_itt(payload, "event", 1, procedures=[template])
    executions = [{"node_id": "T1", "execution_id": "E1", "sql": "SELECT 1", "observation": "[(1,)]"}]
    tree = change_node(tree, "T1", status=S.DONE, evidence_refs=("E1",))
    payload = itt_payload(tree, executions)
    payload[0]["status"] = payload[0]["children"][0]["status"] = "done"
    leaf = payload[0]["children"][0]["children"][0]
    del leaf["semantic_refs"]
    del leaf["template_id"]
    updated = parse_itt(payload, "event", 2, previous=tree, executions=executions)
    node = updated.root_nodes[0].children[0].children[0]
    assert node.metadata["semantic_refs"] == PM_REFS
    assert node.metadata["query_template"] == template
    assert node.metadata["execution_code"] == "SELECT 1"


@pytest.mark.parametrize("task_status,parent_status", [("done", S.RESOLVED), ("error", S.BLOCKED)])
def test_entity_contract_derives_parent_state_and_preserves_history(task_status, parent_status):
    from src.agent.planner import PLANNER_SYSTEM_PROMPT
    assert all(line.strip() for line in PLANNER_SYSTEM_PROMPT.splitlines())
    payload = parse_itt_response(("- id: G1" + PLANNER_SYSTEM_PROMPT.split("\n- id: G1", 1)[1]).replace("todo｜done｜error", "todo"))
    tree = parse_itt(payload, "event", 1)
    assert tree.root_nodes[0].children[0].node_id == "E1"
    executions = [{"node_id": "T1", "execution_id": "exec-1", "sql": "SELECT 1", "observation": "[(1,)]"}]
    payload = itt_payload(tree, executions, planner_view=True)
    goal = payload[0]
    entity = goal["children"][0]
    assert "status" not in goal and "status" not in entity
    entity["children"][0]["status"] = task_status
    updated = parse_itt(payload, "event", 2, previous=tree, executions=executions)
    assert updated.root_nodes[0].status == parent_status
    assert updated.root_nodes[0].children[0].status == parent_status
    assert next_task(updated) is None
    payload = itt_payload(updated, executions, planner_view=True)
    assert payload[0]["children"][0]["children"][0]["status"] == task_status
    payload[0]["children"][0]["children"].append({"id": "T2", "objective": "Verify process relationship", "status": "todo", "execution_code": "", "execution_result": ""})
    continued = parse_itt(payload, "event", 3, previous=updated, executions=executions)
    assert next_task(continued).node_id == "T2"
    assert continued.root_nodes[0].status == S.OPEN


def test_reviewer_summary_survives_json_yaml_and_cannot_be_rewritten():
    from src.schema import TracebackTaskTree
    tree = parse_itt(plan(), 'event', 1)
    tree = change_node(tree, 'T1', metadata={'execution_result': '已核对：返回两条记录。', 'execution_result_source': 'reviewer'})
    restored = TracebackTaskTree.from_dict(json.loads(json.dumps(tree.to_dict())))
    payload = yaml.safe_load(yaml.safe_dump(itt_payload(restored, planner_view=True), allow_unicode=True))
    task = payload[0]['children'][0]['children'][0]
    assert task['execution_result'] == '已核对：返回两条记录。'
    updated = parse_itt(payload, 'event', 2, previous=restored)
    assert updated.root_nodes[0].children[0].children[0].metadata['execution_result'] == task['execution_result']
    task['execution_result'] = '模型改写的总结'
    with pytest.raises(ValueError, match='Execution code/result'):
        parse_itt(payload, 'event', 2, previous=restored)
