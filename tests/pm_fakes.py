"""Protocol-shaped fake for routing tests; semantic gates have separate tests."""
import json


def assessment(payload, procedure):
    source, quote = next(iter(payload['sources'].items()))
    return {
        'name': procedure['name'], 'decision': 'use',
        'target_question': payload['task']['question'],
        'bindings': {slot: {'value': quote, 'source': source, 'quote': quote, 'role': 'test input'}
                     for slot in procedure['input_slots']},
        'missing': [], 'expected_observation': 'Test observation',
        'output_refs': procedure['semantic_refs'][:1],
        'contribution': 'Test output advances the requested question',
        'next_step': 'Test next operation',
        'checks': {'input_roles_supported': True, 'no_answer_assumption': True,
                   'preserves_question_scope': True, 'evidence_path_supported': True},
        'limitations': [], 'reason': 'Test fixture approval',
    }


def approve(prompt):
    payload = json.loads(prompt)
    if 'applications' in payload:
        return json.dumps({'selected': payload['applications'][0]['name'], 'reason': 'Independent fixture review'})
    return json.dumps({'assessments': [assessment(payload, p) for p in payload['candidates']]})


def verify(prompt):
    payload = json.loads(prompt)
    review = payload['reviewer']
    assert set(review) == {'summary'}
    confirmed = review['summary'] in {
        'The queried alert identifier directly answers the question.',
        'No matching rows and no further leads.',
        'The L3 query could not retrieve evidence.',
        'E1 确认告警 ID 为 alert-1。',
        'device identified',
        '进程创建时间已确认',
    }
    return json.dumps({'can_answer': 'yes', 'answer': 'alert-1'} if confirmed else {'can_answer': 'no'})
