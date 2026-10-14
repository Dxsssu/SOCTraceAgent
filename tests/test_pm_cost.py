from types import SimpleNamespace
from unittest.mock import patch

from src.workflow.context import MemoryContextService, ExcytinBenchSnapshotRepository
from src.schema import Event, RoundReview
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import compact_task, shortlist_headers


def test_retrieval_task_does_not_repeat_tree_or_raw_observations():
    task={'context':'anchor','question':'Find owner','itt':[{'execution_result':'BIG_RESULT'}],
          'executions':[{'execution_id':'E1','sql':'SELECT 1','observation':'BIG_RESULT','query_success':True,'result_status':'rows_returned'}]}
    import json
    text=json.dumps(compact_task(task))
    assert 'BIG_RESULT' not in text and 'itt' not in text
    assert 'E1' in text


def test_local_shortlist_is_bounded_and_no_overlap_is_stable():
    entries=[{'name':f'entry{i}','goal': 'process' if i<10 else 'mail'} for i in range(15)]
    assert len(shortlist_headers(entries,{'question':'Find process'},limit=8))==8
    assert shortlist_headers(entries,{'question':'unmatchedtarget'})==entries


def test_negative_result_reused_until_new_evidence_or_verifier_gap():
    repository=ExcytinBenchSnapshotRepository()
    service=MemoryContextService(repositories={'excytin_bench':repository})
    event=Event(event_id='cache-case',event_name='case',message='Find owner',context={'memory_profile':'excytin_bench'})
    view=service.planner_view()
    with patch.object(repository,'retrieve_procedures',return_value=[]) as lookup:
        view.build(event)
        result=view.build(event,review=RoundReview(event_id=event.event_id,round_id=1,recommendations=('Try another query',)))
        assert result['procedural_memory']['reuse']=='unchanged_evidence'
        assert lookup.call_count==1
        e={'execution_id':'E1','query_success':True,'result_status':'rows_returned','observation':'record-17'}
        view.build(event,executions=[e])
        assert lookup.call_count==2
        view.build(event,executions=[e],review=RoundReview(event_id=event.event_id,round_id=2,gaps=('Verifier 缺少关联',)))
        assert lookup.call_count==3
