"""Question/role/provenance gates independent of any benchmark incident."""
from copy import deepcopy
import json

import pytest

from pm_fakes import assessment, approve
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import (
    SELECTION_PROMPT, GUARD_PROMPT, retrieve, evidence_sources,
)
from src.schema.itt import planner_prompt


def knowledge():
    return {'format_version': '4.0', 'procedures': [
        {'name': name, 'status': status, 'goal': 'Identify an entity through a known record',
         'input_slots': {'anchor': 'Known record identifier'}, 'operations': 'Read matching records',
         'semantic_refs': [{'table': 'Records', 'fields': ['RecordKey', 'Owner']}]}
        for name, status in [('first', 'active'), ('second', 'active'), ('inactive', 'candidate')]]}


def task():
    return {'context': 'Record key is key-17.', 'question': 'Who owns this record?', 'executions': []}


def answer(payload, procedure):
    check = assessment(payload, procedure)
    check['bindings'] = {'anchor': {'value': 'key-17', 'source': '/context',
                                  'quote': 'Record key is key-17.', 'role': 'record identifier'}}
    return check


def test_rejected_first_candidate_does_not_hide_applicable_second():
    report, calls = {}, []
    def llm(system, prompt):
        if system == GUARD_PROMPT:
            return approve(prompt)
        p = json.loads(prompt)
        calls.append(p)
        if system == SELECTION_PROMPT:
            assert all(c['status'] == 'active' for c in p['catalog'])
            assert all('input_slots' in c for c in p['catalog'])
            return '["first", "inactive", "unknown", "second"]'
        a, b = [answer(p, c) for c in p['candidates']]
        a['checks']['no_answer_assumption'] = False
        return json.dumps({'assessments': [a, b]})
    result = retrieve(knowledge(), task(), selector=llm, diagnostics=report)
    assert [p['name'] for p in result] == ['second']
    assert len(calls) == 2  # The guard is handled separately above.
    assert report['application']['target_question'] == task()['question']
    assert report['assessments'][0]['validation_error'] == 'failed_suitability_checks'


@pytest.mark.parametrize('mutation', [
    'question', 'role', 'answer', 'scope', 'path', 'plan_source', 'fake_quote',
    'fake_value', 'unknown_output', 'missing', 'rationale', 'duplicate_assessment', 'unknown_assessment',
])
def test_unsafe_application_is_rejected(mutation):
    def llm(system, prompt):
        if system == SELECTION_PROMPT:
            return '["first"]'
        if system == GUARD_PROMPT:
            return approve(prompt)
        p = json.loads(prompt)
        a = answer(p, p['candidates'][0])
        if mutation == 'question': a['target_question'] = 'Confirm key-17 exists'
        if mutation == 'role': a['checks']['input_roles_supported'] = False
        if mutation == 'answer': a['checks']['no_answer_assumption'] = False
        if mutation == 'scope': a['checks']['preserves_question_scope'] = False
        if mutation == 'path': a['checks']['evidence_path_supported'] = False
        if mutation == 'plan_source': a['bindings']['anchor']['source'] = '/itt/0/objective'
        if mutation == 'fake_quote': a['bindings']['anchor']['quote'] = 'Record key is invented.'
        if mutation == 'fake_value': a['bindings']['anchor']['value'] = 'invented'
        if mutation == 'unknown_output': a['output_refs'][0]['fields'] = ['UnknownField']
        if mutation == 'missing': a['missing'] = ['anchor']
        if mutation == 'rationale': a['contribution'] = ''
        if mutation == 'unknown_assessment': a['name'] = 'other'
        return json.dumps({'assessments': [a, a] if mutation == 'duplicate_assessment' else [a]})
    assert retrieve(knowledge(), task(), selector=llm) == []


def test_sources_exclude_plans_review_claims_and_failed_queries():
    value = dict(task(), itt=[{'objective': 'invented plan result'}], review={'findings': ['unsupported finding']},
        current_task={'objective': 'locate imaginary anchor'}, executions=[
            {'query_success': False, 'result_status': 'error', 'observation': 'failed record'},
            {'query_success': True, 'result_status': 'rows_returned', 'observation': 'actual record'},
            {'execution_status': 'completed', 'result': {'owner': 'actual owner'}}])
    sources = evidence_sources(value)
    assert '/executions/1/observation' in sources
    assert '/executions/2/result/owner' in sources
    assert set(sources) == {'/context', '/question', '/executions/1/observation', '/executions/2/result/owner'}


@pytest.mark.parametrize('anchor', ['abc-record', 'https://sample.invalid/a?b=1', '"quoted value"'])
def test_bindings_handle_plain_source_values_without_json_escape_false_negatives(anchor):
    core = {'context': anchor, 'question': 'Which owner is associated with this known record?'}
    def llm(system, prompt):
        if system == SELECTION_PROMPT: return '["first"]'
        if system == GUARD_PROMPT:
            return approve(prompt)
        p = json.loads(prompt)
        a = assessment(p, p['candidates'][0])
        return json.dumps({'assessments': [a]})
    assert retrieve(knowledge(), core, selector=llm)


def test_missing_gate_protocol_and_inactive_only_never_inject():
    assert retrieve(knowledge(), task(), selector=lambda *_: '["inactive"]') == []
    responses = iter(['["first"]', '{"bindings":{"anchor":"key-17"},"missing":[]}', '{}'])
    assert retrieve(knowledge(), task(), selector=lambda *_: next(responses)) == []


def test_application_is_separate_from_reusable_pm_and_reaches_planner():
    original = knowledge()
    before = deepcopy(original)
    report = {}
    def llm(system, prompt):
        if system == SELECTION_PROMPT: return '["first"]'
        if system == GUARD_PROMPT:
            return approve(prompt)
        p = json.loads(prompt)
        return json.dumps({'assessments': [answer(p, p['candidates'][0])]})
    procedures = retrieve(original, task(), selector=llm, diagnostics=report)
    prompt = planner_prompt('system', {'procedural_memory': {'procedures': procedures, 'application': report['application']}})
    assert 'Who owns this record?' in prompt
    assert 'next_step' in prompt and 'limitations' in prompt
    assert original == before
    assert set(procedures[0]) == {'name', 'status', 'goal', 'input_slots', 'operations', 'semantic_refs'}


@pytest.mark.parametrize('guard_response', [
    {'selected': None, 'reason': 'Input role does not match the target event'},
    {'selected': 'inactive', 'reason': 'Cannot revive an unapproved procedure'},
    {'selected': 'second', 'reason': 'Cannot choose outside the assessed shortlist'},
    {'selected': 'first', 'reason': ''},
    {'selected': ['first'], 'reason': 'Invalid selection type'},
])
def test_independent_guard_can_veto_but_cannot_invent_candidates(guard_response):
    report = {}
    def llm(system, prompt):
        if system == SELECTION_PROMPT: return '["first"]'
        if system == GUARD_PROMPT: return json.dumps(guard_response)
        p = json.loads(prompt)
        return json.dumps({'assessments': [answer(p, p['candidates'][0])]})
    assert retrieve(knowledge(), task(), selector=llm, diagnostics=report) == []
    assert report['application'] is None
    assert report['status'] in {'rejected_by_guard', 'invalid_guard', 'invalid_guard_selection', 'invalid_model_response'}


def test_independent_guard_can_choose_a_better_validated_alternative():
    def llm(system, prompt):
        if system == SELECTION_PROMPT: return '["first", "second"]'
        if system == GUARD_PROMPT:
            p = json.loads(prompt)
            assert len(p['applications']) == 2
            return json.dumps({'selected': 'second', 'reason': 'Fewer unsupported relations to the target'})
        p = json.loads(prompt)
        return json.dumps({'assessments': [answer(p, c) for c in p['candidates']]})
    report = {}
    assert retrieve(knowledge(), task(), selector=llm, diagnostics=report)[0]['name'] == 'second'
    assert report['application']['independent_review'] == 'Fewer unsupported relations to the target'


def test_echoed_catalog_objects_cannot_override_stored_procedure():
    def llm(system, prompt):
        if system == SELECTION_PROMPT:
            return json.dumps([{'name': 'inactive', 'status': 'active'},
                               {'name': 'first', 'operations': 'invented operation', 'input_slots': {}}])
        if system == GUARD_PROMPT: return approve(prompt)
        p = json.loads(prompt)
        assert len(p['candidates']) == 1
        assert p['candidates'][0]['operations'] == 'Read matching records'
        assert p['candidates'][0]['input_slots'] == {'anchor': 'Known record identifier'}
        return json.dumps({'assessments': [answer(p, p['candidates'][0])]})
    assert retrieve(knowledge(), task(), selector=llm)[0]['operations'] == 'Read matching records'


def test_format_repair_is_bounded_and_does_not_bypass_semantic_guard():
    report, stage_calls = {}, {'assessment': 0, 'guard': 0}
    saved = {}
    def llm(system, prompt):
        if system == SELECTION_PROMPT: return '["first"]'
        if system == GUARD_PROMPT:
            stage_calls['guard'] += 1
            return json.dumps({'selected': None, 'reason': 'Role is unsupported after independent review'})
        stage_calls['assessment'] += 1
        if stage_calls['assessment'] == 1:
            p = json.loads(prompt)
            saved['check'] = answer(p, p['candidates'][0])
            malformed = deepcopy(saved['check'])
            malformed['checks']['wrong_key'] = malformed['checks'].pop('preserves_question_scope')
            return json.dumps({'assessments': [malformed]})
        assert '格式错误' in prompt
        return json.dumps({'assessments': [saved['check']]})
    assert retrieve(knowledge(), task(), selector=llm, diagnostics=report) == []
    assert report['format_retries'] == 1
    assert stage_calls == {'assessment': 2, 'guard': 1}
    assert report['status'] == 'rejected_by_guard'


def test_repeated_bad_format_stops_after_one_repair():
    calls, report = [], {}
    def llm(system, prompt):
        calls.append(prompt)
        return 'not json'
    assert retrieve(knowledge(), task(), selector=llm, diagnostics=report) == []
    assert len(calls) == 2
    assert report['format_retries'] == 1
