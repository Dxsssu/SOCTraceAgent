"""Protocol-shaped fake for routing tests; semantic gates have separate tests."""
import json
import yaml


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
    payload = yaml.safe_load(prompt)
    tasks = task_rows(payload['itt'])
    review = next(t['execution_result'] for t in reversed(tasks) if t['execution_result'])
    confirmed = review['summary'] in {
        'The queried alert identifier directly answers the question.',
        'No matching rows and no further leads.',
        'The L3 query could not retrieve evidence.',
        'E1 确认告警 ID 为 alert-1。',
        'device identified',
        '进程创建时间已确认',
    }
    return json.dumps({'can_answer': 'yes', 'answer': 'alert-1'} if confirmed else {'can_answer': 'no'})


def task_rows(itt):
    return [dict(task, entity_id=entity['id']) for goal in itt
            for entity in goal['children'] for task in entity['children']]


def initial_operations(tree):
    """Adapt legacy snapshot fixtures to the model's initial-operation protocol."""
    operations = []
    for gi, goal in enumerate(tree):
        goal_ref = f"goal_{gi}"
        operations.append({"op": "add_goal", "ref": goal_ref, "objective": goal["objective"]})
        for ei, entity in enumerate(goal["children"]):
            entity_ref = f"entity_{gi}_{ei}"
            operations.append({"op": "add_entity", "ref": entity_ref, "goal_id": "$" + goal_ref,
                               "objective": entity["objective"]})
            for task in entity["children"]:
                operations.append({"op": "add_task", "entity_id": "$" + entity_ref,
                    "objective": task["objective"],
                    **{k: task[k] for k in ("semantic_refs", "template_id") if k in task}})
    return {"operations": operations}
