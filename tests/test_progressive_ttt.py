from dataclasses import replace
from unittest.mock import patch

import pytest
import yaml

from src.schema import Event, EventStatus, RoundReview
from src.schema.ttt import TTTNodeStatus as S
from src.schema.ttt_updates import apply_updates, change_node, next_task, parse_initial, resolved, walk
from src.memory.working_memory import TTTStore
from src.benchmarks.excytin_bench import ExcytinBenchWorkflow, BenchmarkActionType
from src.workflow.orchestrator import SOCTraceWorkflow


def initial():
    return {'next_task_id': 'T9', 'selection_reason': '先定位告警实体', 'root_nodes': [
        {'node_id': 'G1', 'title': '确定进程 ID 和创建时间', 'level': 1, 'status': 'open', 'children': [
            {'node_id': 'Q1', 'title': '告警对应哪个进程？', 'level': 2, 'status': 'open', 'children': [
                {'node_id': 'T9', 'title': '读取告警实体', 'level': 3, 'status': 'todo', 'children': []}]},
            {'node_id': 'Q2', 'title': '进程创建时间是什么？', 'level': 2, 'status': 'open', 'children': []}]}]}


def tree():
    return parse_initial(initial(), 'event', 1)


def expansion(current):
    return {'base_version': current.version, 'updates': [
        {'operation': 'add_node', 'parent_id': 'Q2', 'node': {
            'node_id': 'T2', 'title': '读取已定位进程的创建记录', 'level': 3, 'status': 'todo', 'children': []}}
    ], 'next_task_id': 'T2', 'selection_reason': 'E1 已提供进程 ID，尚缺创建时间'}


def test_lazy_expansion_preserves_history_and_goal():
    old = change_node(tree(), 'T9', status=S.DONE, result_summary='进程 ID 为 42', evidence_refs=('E1',))
    assert not resolved(old)
    assert next_task(old) is None
    new = apply_updates(old, expansion(old), known_evidence=['E1'])
    assert next_task(new).node_id == 'T2'
    assert [(n.node_id, d) for n, d in walk(new)] == [('G1', 1), ('Q1', 2), ('T9', 3), ('Q2', 2), ('T2', 3)]
    assert new.root_nodes[0].status == S.OPEN
    assert new.root_nodes[0].children[0].children[0].evidence_refs == ('E1',)
    assert old.root_nodes[0].children[1].children == ()


@pytest.mark.parametrize('mutation', ['stale', 'fake_evidence', 'duplicate', 'rewrite_goal', 'select_l2', 'no_selection', 'empty_open', 'fourth_level'])
def test_invalid_updates_are_rejected_atomically(mutation):
    old = tree()
    before = old.to_dict()
    update = expansion(old)
    if mutation == 'stale': update['base_version'] = 0
    if mutation == 'fake_evidence': update['updates'][0]['node']['evidence_refs'] = ['invented']
    if mutation == 'duplicate': update['updates'][0]['node']['node_id'] = 'T9'
    if mutation == 'rewrite_goal': update['updates'] = [{'operation': 'update_node', 'node_id': 'G1', 'changes': {'title': '扩大调查范围'}}]
    if mutation == 'select_l2': update['next_task_id'] = 'Q2'
    if mutation == 'no_selection': del update['next_task_id']
    if mutation == 'empty_open': update['next_task_id'] = None
    if mutation == 'fourth_level': update['updates'][0]['parent_id'] = 'T9'
    with pytest.raises(ValueError): apply_updates(old, update)
    assert old.to_dict() == before


def test_reopen_requires_reason_and_keeps_evidence():
    old = change_node(tree(), 'T9', status=S.DONE, evidence_refs=('E1',))
    update = {'base_version': old.version, 'updates': [{'operation': 'update_node', 'node_id': 'T9', 'changes': {'status': 'todo', 'evidence_refs': []}}], 'next_task_id': 'T9'}
    with pytest.raises(ValueError, match='reason'): apply_updates(old, update, known_evidence=['E1'])
    update['updates'][0]['reason'] = '新证据与前次实体关联冲突'
    new = apply_updates(old, update, known_evidence=['E1'])
    assert next_task(new).evidence_refs == ('E1',)


def test_sqlite_roundtrip_selection_and_compare_and_swap(tmp_path):
    store = TTTStore(tmp_path / 'ttt.db')
    first = store.save_snapshot(tree(), expected_version=0)
    new = apply_updates(first, expansion(first))
    saved = store.save_snapshot(new, expected_version=first.version)
    assert store.get_latest_ttt('event').to_dict() == saved.to_dict()
    assert store.claim_next_todo_leaf('event', 1).node_id == 'T2'
    with pytest.raises(ValueError, match='version changed'):
        store.save_snapshot(new, expected_version=first.version)
    assert store.get_latest_ttt('event').root_nodes[0].status == S.OPEN


def test_benchmark_progressive_two_query_investigation():
    workflow = None
    def llm(system, prompt):
        if '最终答案整理器' in system:
            return 'answer: "42, 2026-01-01T00:00:00Z"'
        if '中的 Executor' in system:
            return 'sql: SELECT ProcessId FROM AlertEvidence LIMIT 1'
        if '中的 Reviewer' in system:
            if len(workflow.state.executions) == 1:
                return 'decision: continue\nfindings: ["进程 ID 为 42"]\ngaps: ["缺少创建时间"]'
            return 'decision: ready_to_submit\nfindings: ["进程创建时间已确认"]\nanswer_facts: ["42, 2026-01-01T00:00:00Z"]'
        if workflow.state is None:
            return yaml.safe_dump({'ttt': initial()}, allow_unicode=True)
        return yaml.safe_dump(expansion(workflow.state.ttt), allow_unicode=True)
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=llm)
    first = workflow.act({'context': '告警 A', 'question': '进程 ID 和创建时间是什么？'})
    assert first.node_id == 'T9'
    second = workflow.act('[{"ProcessId":42}]')
    assert second.node_id == 'T2'
    assert not resolved(workflow.state.ttt)
    final = workflow.act('[{"ProcessCreationTime":"2026-01-01T00:00:00Z"}]')
    assert final.action_type == BenchmarkActionType.SUBMIT
    assert resolved(workflow.state.ttt)
    assert workflow.state.ttt.root_nodes[0].evidence_refs == ('E1', 'E2')
    assert workflow.get_logging()['executions'][0]['execution_id'] == 'E1'


def test_deferred_only_tree_replans_instead_of_submitting():
    workflow = None
    def llm(system, prompt):
        if '中的 Executor' in system: return 'sql: SELECT 1'
        if workflow.state is None:
            payload = initial()
            payload['root_nodes'][0]['children'][0]['children'] = []
            payload['next_task_id'] = None
            return yaml.safe_dump({'ttt': payload}, allow_unicode=True)
        return yaml.safe_dump(expansion(workflow.state.ttt), allow_unicode=True)
    workflow = ExcytinBenchWorkflow(max_steps=5, llm=llm)
    action = workflow.act('确定进程 ID 和创建时间')
    assert action.action_type == BenchmarkActionType.QUERY
    assert action.node_id == 'T2'


def test_background_planner_uses_same_patch_protocol(tmp_path):
    workflow = SOCTraceWorkflow(db_path=str(tmp_path / 'runtime.db'))
    event = Event(event_id='event', event_name='alert', message='确定进程 ID 和创建时间', event_status=EventStatus.REPLANNING)
    current = workflow.ttt_store.save_snapshot(tree())
    workflow.storage.save_event(event)
    workflow.storage.save_round_review(RoundReview(event_id='event', round_id=1, gaps=('缺少创建时间',)))
    with patch('src.agent.planner.call_llm', return_value=yaml.safe_dump(expansion(current), allow_unicode=True)):
        updated = workflow.planner.process_replanning(event)
    assert updated.event_status == EventStatus.PLANNED
    saved = workflow.ttt_store.get_latest_ttt('event')
    assert saved.next_task_id == 'T2'
    assert saved.round_id == 2


def test_resolution_requires_evidence_and_blocked_is_not_success():
    old = tree()
    update = {'base_version': old.version, 'updates': [{'operation': 'update_node', 'node_id': 'G1', 'changes': {'status': 'resolved', 'result_summary': '已确认'}}], 'next_task_id': None}
    with pytest.raises(ValueError, match='evidence'):
        apply_updates(old, update)
    update['updates'][0]['changes']['evidence_refs'] = ['E1']
    assert resolved(apply_updates(old, update, known_evidence=['E1']))
    update['updates'][0]['changes']['status'] = 'blocked'
    blocked = apply_updates(old, update, known_evidence=['E1'])
    assert not resolved(blocked)
    assert next_task(blocked) is None


def test_background_open_goal_without_task_is_not_completed(tmp_path):
    workflow = SOCTraceWorkflow(db_path=str(tmp_path / 'runtime.db'))
    event = Event(event_id='event', event_name='alert', message='定位进程', event_status=EventStatus.REPLANNING)
    current = workflow.ttt_store.save_snapshot(tree())
    workflow.storage.save_event(event)
    workflow.storage.save_round_review(RoundReview(event_id='event', round_id=1, gaps=('缺少证据',)))
    update = {'base_version': current.version, 'updates': [], 'next_task_id': None}
    with patch('src.agent.planner.call_llm', return_value=yaml.safe_dump(update)):
        result = workflow.planner.process_replanning(event)
    assert result.event_status == EventStatus.FAILED
    assert workflow.ttt_store.get_latest_ttt('event').version == current.version


def test_flat_id_children_produce_actionable_validation_error():
    payload = initial()
    payload['root_nodes'][0]['children'] = ['Q1']
    with pytest.raises(ValueError, match='nested node objects'):
        parse_initial(payload, 'event', 1)
