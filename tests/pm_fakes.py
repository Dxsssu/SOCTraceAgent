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
    records = payload['executions']
    if not records:
        return json.dumps({'confirmed':False,'goals':[], 'gap':'没有可用结果。','suggestion':'补查目标关系。'})
    record = records[-1]
    return json.dumps({'confirmed':True, 'goals':[{'id':g['id'],'relation':'Test evidence establishes the requested relation',
        'evidence':[{'execution_id':record['execution_id'],'excerpt_id':record['excerpts'][0]['excerpt_id']}]} for g in payload['goals']],
        'gap':'','suggestion':''})
