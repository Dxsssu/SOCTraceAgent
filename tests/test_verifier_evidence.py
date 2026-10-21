from copy import deepcopy
import pytest
from src.schema.verifier_evidence import compact_verifier_itt
from src.agent.verifier import parse_verifier_response


def test_lossless_compaction_keeps_distinct_candidates_and_provenance():
    facts = [{'host': 'h', 'pid': '01', 'time': 't1'},
             {'host': 'h', 'pid': '01', 'time': 't1'},
             {'host': 'h', 'pid': '02', 'time': 't2'}]
    original = [{'id': 'G1', 'children': [{'id': 'E1', 'children': [{
        'id': 'T1', 'execution_code': 'SELECT ...', 'input_entity_ids': ['E1'],
        'execution_result': {'facts': facts, 'source': {'observation_truncated': True},
                             'limitations': ['partial'], 'summary': 'two candidates'}}]}]}]
    saved = deepcopy(original)
    view, stats = compact_verifier_itt(original)
    result = view[0]['children'][0]['children'][0]['execution_result']
    assert [{**result['facts_common'], **f} for f in result['facts']] == [facts[0], facts[2]]
    assert result['source']['observation_truncated'] is True
    assert result['limitations'] == ['partial']
    assert stats == {'facts_before': 3, 'facts_after': 2}
    assert original == saved


@pytest.mark.parametrize('text', ['Analysis first\ncan_answer: "no"', '```yaml\ncan_answer: "no"\n```', 'can_answer: "no"\nexplanation: text'])
def test_reject_prose_or_extra_fields(text):
    with pytest.raises(ValueError):
        parse_verifier_response(text)


def test_accept_minimal_verdict():
    assert parse_verifier_response('can_answer: "yes"\nanswer: "001"') == {'can_answer': 'yes', 'answer': '001'}
