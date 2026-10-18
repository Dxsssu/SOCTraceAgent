"""Evidence survives role boundaries while model inputs omit unrelated state."""
import json
from unittest.mock import patch

import pytest
import yaml

from src.agent.reviewer import ReviewerRuntime
from src.benchmarks.excytin_bench.workflow import BenchmarkExecution, ExcytinBenchWorkflow
from src.memory.working_memory import TTTStore
from src.schema import Event, TracebackTaskTree
from src.schema.itt import compact_json, itt_payload, review_payload
from src.schema.planner_operations import apply_planner_operations
from src.schema.reviewer_output import evidence_result
from src.schema.ttt_updates import change_node, walk
from src.workflow.context import MemoryContextService
from pm_fakes import task_rows


FACTS = [
    {"host_id": "host-A", "pid": "0042", "process_guid": "000123456789012345678901234567890",
     "timestamp": "2026-10-17T08:30:00.123456Z", "path": "/tmp/first"},
    {"host_id": "host-B", "pid": "0042", "process_guid": "other-guid",
     "timestamp": "2026-10-17T09:30:00.123456+01:00", "path": "/tmp/second"},
]
REVIEW = {"summary": "同一 PID 对应不同主机上的两个进程。", "facts": FACTS, "limitations": ["不能仅按 PID 关联进程。"]}


def initial(task_count=1):
    operations = [
        {"op": "add_goal", "ref": "g", "objective": "Find executable"},
        {"op": "add_entity", "ref": "e", "goal_id": "$g", "objective": "Alert A"},
    ]
    operations.extend({"op": "add_task", "entity_id": "$e", "objective": f"Query process {i}"}
                      for i in range(task_count))
    return {"operations": operations}


def execution(truncated=False):
    raw = compact_json(FACTS)
    return BenchmarkExecution(1, 'T1', 'Query process 0', 'SELECT * FROM processes;', raw,
                              True, 'rows_returned', truncated, len(raw))


def test_nested_view_preserves_topology_and_evidence_after_storage_and_replanning(tmp_path):
    tree = apply_planner_operations(initial(20), 'event', 1)
    result = evidence_result(REVIEW, execution())
    tree = change_node(tree, 'T1', metadata={'execution_result': result, 'execution_result_source': 'reviewer'})
    store = TTTStore(tmp_path / 'evidence.db')
    store.save_snapshot(tree, expected_version=0)
    restored = store.get_latest_ttt('event')
    updated = apply_planner_operations({'operations': [{'op': 'add_entity', 'ref': 'next', 'goal_id': 'G1',
                                                        'objective': 'Host A'},
                    {'op': 'add_task', 'entity_id': '$next', 'objective': 'Check host A'}]},
                    'event', 2, previous=restored, executions=[execution()])
    snapshot = TracebackTaskTree.from_dict(json.loads(json.dumps(updated.to_dict())))
    view = itt_payload(snapshot, [execution()], planner_view=True)
    tasks = task_rows(json.loads(compact_json(view)))
    assert tasks[0]['execution_result'] == result
    assert tasks[0]['execution_code'] == execution().sql
    assert tasks[-1]['entity_id'] == 'E2'
    assert view[0]['id'] == 'G1'
    assert [entity['id'] for entity in view[0]['children']] == ['E1', 'E2']
    assert len(tasks) == 21
    assert tasks[0]['execution_result']['facts'] == FACTS
    assert yaml.safe_load(yaml.safe_dump(view, allow_unicode=True)) == view
    # Modifying a view must never rewrite stored evidence.
    view[0]['children'][0]['children'][0]['execution_result']['facts'][0]['pid'] = 'changed'
    assert next(n for n, _ in walk(snapshot) if n.node_id == 'T1').metadata['execution_result']['facts'] == FACTS


def test_reviewer_input_size_is_independent_of_other_tasks():
    one = apply_planner_operations(initial(), 'event', 1)
    many = apply_planner_operations(initial(50), 'event', 1)
    assert review_payload(one, execution()) == review_payload(many, execution())
    payload = review_payload(many, execution())
    assert set(payload) == {'task', 'executor_result'}
    assert payload['task'] == {'id': 'T1', 'objective': 'Query process 0'}
    assert payload['executor_result']['observation'] == compact_json(FACTS)
    with pytest.raises(ValueError, match='existing task'):
        review_payload(one, {'node_id': 'missing'})


def test_local_reviewer_persists_structured_result(tmp_path):
    event = Event(event_name='test', message='Find executable')
    tree = apply_planner_operations(initial(), event.event_id, event.current_round)
    store = TTTStore(tmp_path / 'local.db')
    tree = store.save_snapshot(tree, expected_version=0)
    runtime = ReviewerRuntime(ttt_store=store)
    with patch('src.agent.reviewer.call_llm', return_value=yaml.safe_dump(REVIEW)) as model:
        runtime._generate_round_review(event, tree.to_dict(), [execution()])
    assert set(json.loads(model.call_args.args[1])) == {'task', 'executor_result'}
    stored = store.get_latest_ttt(event.event_id).root_nodes[0].children[0].children[0]
    assert stored.metadata['execution_result']['facts'] == FACTS
    assert stored.metadata['execution_result']['source']['execution_id'] == 'E1'


def test_two_round_workflow_preserves_evidence_through_planner_and_verifier():
    calls = []
    workflow = None
    def model(system, user):
        if '系统中的 Planner' in system:
            calls.append('planner')
            if workflow.state is None:
                return yaml.safe_dump(initial())
            payload = yaml.safe_load(user)
            tasks = task_rows(payload['itt'])
            assert tasks[0]['execution_result']['facts'] == FACTS
            assert payload['review'] == {'can_answer': 'no'}
            return yaml.safe_dump({'operations': [{'op': 'add_task', 'entity_id': 'E1',
                'objective': 'Check host-A process GUID 000123456789012345678901234567890'}]})
        payload = yaml.safe_load(user)
        if '你是 Executor' in system:
            calls.append('executor')
            assert set(payload) == {'id', 'objective'}
            return 'sql: SELECT * FROM processes'
        if '你是 Reviewer' in system:
            calls.append('reviewer')
            assert set(payload) == {'task', 'executor_result'}
            if payload['task']['id'] == 'T1':
                return yaml.safe_dump(REVIEW)
            return yaml.safe_dump({'summary': '路径已确认', 'facts': [{'path': '/tmp/first'}], 'limitations': []})
        assert '你是 Verifier' in system
        calls.append('verifier')
        assert set(payload) == {'context', 'question', 'itt'}
        tasks = task_rows(payload['itt'])
        assert tasks[0]['execution_result']['facts'] == FACTS
        assert tasks[0]['execution_result']['source']['execution_id'] == 'E1'
        return 'can_answer: "no"' if len(tasks) == 1 else 'can_answer: "yes"\nanswer: /tmp/first'
    workflow = ExcytinBenchWorkflow(llm=model)
    action = workflow.act({'context': 'host-A alert', 'question': 'Find executable'})
    assert workflow.state.pending_node.metadata['execution_code'] == action.content
    second = workflow.act(compact_json(FACTS))
    assert second.node_id == 'T2'
    submitted = workflow.act('[{"path":"/tmp/first"}]')
    assert submitted.submit and submitted.content == '/tmp/first'
    assert calls == ['planner', 'executor', 'reviewer', 'verifier'] * 2
