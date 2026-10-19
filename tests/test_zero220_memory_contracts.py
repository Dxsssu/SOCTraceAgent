"""Contracts for raw-evidence EM retrieval, safe projection and audited guidance."""
from copy import deepcopy

import pytest

from src.memory.longterm_memory.procedural_memory.flows.catalog import apply_adaptations
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession


def fixture_store(*, flow=False, candidate=True):
    columns = ['SystemAlertId', 'AlertName', 'TimeGenerated', 'Entities', 'Description']
    template = {
        'id': 'sql_raw', 'sql': 'SELECT '+', '.join(columns)+' FROM SecurityAlert WHERE AlertName = :p1 LIMIT 100',
        'inputs': {'p1': {'field': 'AlertName', 'operator': 'EQ', 'type': 'string'}},
        'outputs': columns, 'schema_fields': {'SecurityAlert': columns}, 'row_cap': 100, 'scope': 'filtered',
    }
    episode = {
        'id': 'em_raw', 'kind': 'atomic', 'name': 'raw alert evidence', 'intent': 'investigate evidence',
        'trigger_entity': {'concept': 'Alert_Title', 'kind': 'alert_name'},
        'trigger_aliases': [{'concept': 'Device_Name', 'kind': 'device_name'},
                            {'concept': 'IP', 'kind': None}, {'concept': 'IP', 'kind': 'remote_ip'}],
        'required_inputs': [{'step': 's1', 'parameter': 'p1', 'table': 'SecurityAlert', 'field': 'AlertName'}],
        'steps': [{'id': 's1', 'template_id': 'sql_raw', 'planner_guide': 'Read raw evidence',
                   'outputs': {c: {'entity_concept': None} for c in columns}}],
        'bindings': [], 'caution': ['Verify entity roles'], 'status': 'active',
        'intent_variants': [{'id': 'v1', 'name': 'raw evidence', 'intent': 'investigate evidence',
                             'caution': ['Verify entity roles'],
                             'step_guides': [{'step': 's1', 'guide': 'Read variant evidence'}]}],
    }
    if candidate:
        episode['candidate_output_concepts'] = ['File_Name', 'Process_Command']
    snippets = [episode]
    if flow:
        pm = deepcopy(episode)
        pm.update(id='pm_raw', kind='evidence_chain', intent_variants=[])
        pm['steps'] = [dict(deepcopy(episode['steps'][0]), id=sid, episodic_id='em_raw') for sid in ['s1', 's2']]
        snippets.append(pm)
    doc = {'format_version': 'trajectory-snippets-2', 'templates': {'sql_raw': template},
           'snippets': snippets, 'memory_catalog_ids': {}, 'query_optimization': 'test'}
    doc['catalog_id'] = digest(doc)
    return TrajectorySnippetStore(doc)


def router(store):
    schema = {'SecurityAlert': {k: 'text' for k in store.templates['sql_raw']['schema_fields']['SecurityAlert']}}
    return ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)), store)


def test_trigger_alias_is_retrievable_and_vocabulary_handles_generic_kind():
    p = router(fixture_store())
    vocabulary = p.vocabulary('episodic')
    assert 'Device_Name' in vocabulary and 'IP' in vocabulary
    hit = p.retrieve({'known': [{'concept': 'Device_Name', 'kind': 'device_name'}],
                      'wanted': ['File_Name'], 'intent': 'investigate evidence'})
    assert hit['memory_type'] == 'episodic'
    assert [s['id'] for s in hit['candidates']] == ['em_raw']
    assert hit['candidates'][0]['output_contract'] == 'raw_evidence_candidates_require_role_validation'


def test_potential_em_outputs_do_not_certify_pm_output_coverage():
    p = router(fixture_store(flow=True))
    hit = p.retrieve({'known': ['Device_Name'], 'wanted': ['File_Name'], 'intent': 'investigate evidence'})
    assert p.retrievals[0]['memory_type'] == 'procedural'
    assert p.retrievals[0]['candidates'] == []
    assert hit['memory_type'] == 'episodic' and hit['candidates'][0]['id'] == 'em_raw'
    # Identical SQL without potential-output metadata cannot promise a filename.
    plain = router(fixture_store(candidate=False))
    clue = plain.retrieve({'known': ['Device_Name'], 'wanted': ['File_Name'],
                           'intent': 'investigate evidence'})['candidates'][0]
    assert clue['coverage']['mode'] == 'partial'
    assert clue['coverage']['covered_wanted'] == []
    assert clue['coverage']['investigation_role'] == 'clue_discovery'


def test_raw_entities_survive_projection_even_when_physical_known_field_matches():
    p = router(fixture_store())
    p.retrieve({'known': ['Alert_Title'], 'wanted': ['File_Name'], 'intent': 'investigate evidence'})
    bound = p.bind({'id': 'em_raw', 'step': 's1', 'inputs': {'p1': 'PRT attempt'}})
    assert 'Entities' in bound['outputs']
    assert 'Entities' in bound['bound_sql'] and 'Entities' in bound['sql_template']
    assert 'Description' not in bound['outputs']
    assert 'Description' in p.store.templates['sql_raw']['outputs']


def test_explicit_required_evidence_column_survives_without_candidate_metadata():
    store = fixture_store(candidate=False)
    store.snippets['em_raw']['required_evidence_columns'] = ['Entities']
    p = router(store)
    query = {'known': ['Alert_Title'], 'wanted': ['Alert_Title'], 'intent': 'investigate evidence'}
    p.retrieve(query)
    bound = p.bind({'id': 'em_raw', 'step': 's1', 'inputs': {'p1': 'Known alert'}})
    assert 'Entities' in bound['outputs']


def test_episode_guidance_patch_reaches_selected_variant_without_changing_sql():
    before = fixture_store()
    patch = {'id': 'patch1', 'episode_id': 'em_raw', 'source_cases': ['case-a'],
             'guide': 'Never guess a role from the first row', 'caution_add': ['Malformed JSON blocks binding']}
    after = apply_adaptations(before, {'provenance': 'evaluation_failure_adaptation',
                                      'episode_guidance_patches': [patch]})
    assert after.templates == before.templates
    assert 'Never guess' not in before.snippets['em_raw']['steps'][0]['planner_guide']
    p = router(after)
    hit = p.retrieve({'known': ['Device_Name'], 'wanted': ['File_Name'], 'intent': 'investigate evidence'})
    assert 'Never guess a role' in hit['candidates'][0]['steps'][0]['guide']
    assert 'Malformed JSON blocks binding' in hit['candidates'][0]['caution']
    assert after.snippets['em_raw']['adaptation_sources'] == ['patch1']


@pytest.mark.parametrize('target,source_cases', [('pm_raw', ['case-a']), ('em_raw', [])])
def test_episode_guidance_patch_rejects_flow_target_or_missing_sources(target, source_cases):
    with pytest.raises(ValueError, match='invalid_episode_guidance_patch'):
        apply_adaptations(fixture_store(flow=True), {'provenance': 'evaluation_failure_adaptation',
            'episode_guidance_patches': [{'id': 'bad', 'episode_id': target, 'source_cases': source_cases}]})


def test_partial_pm_is_not_treated_as_a_complete_direct_flow():
    p = router(fixture_store(flow=True, candidate=False))
    result = p.retrieve({'known': ['Alert_Title'], 'wanted': ['Alert_Title', 'Login_Time'],
                         'intent': 'investigate evidence'})
    assert not p.retrievals[0]['candidates']
    assert result['memory_type'] == 'episodic'
    coverage = result['candidates'][0]['coverage']
    assert coverage['mode'] == 'partial'
    assert 'Login_Time' in coverage['missing_wanted']
    assert 'Login_Time' not in coverage['covered_wanted']


def test_unknown_target_does_not_become_partial_pm_promise():
    p = router(fixture_store(flow=True, candidate=False))
    p.retrieve({'known': ['Alert_Title'], 'wanted': ['Alert_Title', 'invented_target'],
                'intent': 'investigate evidence'})
    assert p.retrievals[0]['candidates'] == []


def test_sid_spelling_resolves_without_conflating_upn_or_display_name():
    p = router(fixture_store())
    assert p.canonical('Account_SID') == 'Field:AccountSid'
    assert p.canonical('Field:AadUserId') == 'Field:AccountObjectId'
    assert p.canonical('Account_UPN') != p.canonical('Field:AadUserId')
    assert p.canonical('Field:UserDisplayName') != p.canonical('Account_Name')


def test_display_name_related_hit_does_not_certify_account_name():
    store = fixture_store(candidate=False)
    store.snippets['em_raw']['evidence_outputs'] = [
        {'column': 'Entities', 'entity_type': 'account', 'identifier_fields': 'Name',
         'concept': 'Field:UserDisplayName'}]
    p = router(store)
    hit = p.retrieve({'known': ['Alert_Title'], 'wanted': ['Account_Name'],
                      'intent': 'investigate evidence'})['candidates'][0]
    assert hit['coverage']['related_wanted'] == ['Account_Name']
    assert hit['coverage']['covered_wanted'] == []
    assert hit['coverage']['missing_wanted'] == ['Account_Name']


def test_raw_candidate_output_is_not_reported_as_verified_coverage():
    p = router(fixture_store())
    c = p.retrieve({'known': ['Alert_Title'], 'wanted': ['File_Name'],
                    'intent': 'investigate evidence'})['candidates'][0]
    assert c['coverage']['covered_wanted'] == []
    assert c['coverage']['potential_wanted'] == ['File_Name']
    assert c['coverage']['mode'] == 'partial'


def test_workflow_scope_prevents_same_entities_from_matching_wrong_investigation():
    store = fixture_store(flow=True)
    store.snippets['pm_raw']['required_intent_groups'] = [['impacket']]
    p = router(store)
    hit = p.retrieve({'known': ['Alert_Title'], 'wanted': ['Alert_Title'],
                      'intent': 'investigate ordinary login evidence'})
    assert hit['memory_type'] == 'episodic'
    assert p.retrievals[0]['rejection_counts']['investigation_scope_mismatch'] == 1


def test_merged_variant_preserves_direction_from_goal_over_background():
    store = fixture_store(flow=True, candidate=False)
    pm = store.snippets['pm_raw']
    pm['intent_variants'] = [
        {'id': 'to_login', 'name': 'fraud to login', 'intent': 'fraud to login',
         'goal_intent': 'login', 'caution': [],
         'step_guides': [{'step':'s1','guide':'start fraud'}, {'step':'s2','guide':'finish login'}]},
        {'id': 'to_fraud', 'name': 'login to fraud', 'intent': 'login to fraud',
         'goal_intent': 'fraud', 'caution': [],
         'step_guides': [{'step':'s1','guide':'start login'}, {'step':'s2','guide':'finish fraud'}]},
    ]
    p = router(store)
    p.semantic.default_intent = 'login evidence login event login source'
    p.semantic.goal_intent = 'find fraud'
    c = p.retrieve({'known': ['Alert_Title'], 'wanted': ['Alert_Title'],
                    'intent': 'find fraud'})['candidates'][0]
    assert c['matched_variant_id'] == 'to_fraud'
    assert c['steps'][-1]['guide'] == 'finish fraud'


def test_metadata_update_cannot_replace_sql_or_dependencies():
    store = fixture_store()
    changed = deepcopy(store.snippets['em_raw'])
    changed['steps'][0]['template_id'] = 'another_sql'
    with pytest.raises(ValueError, match='invalid_existing_memory_update'):
        apply_adaptations(store, {'provenance':'evaluation_failure_adaptation',
            'existing_memory_updates':[{'id':'em_raw','source_cases':['case-a'],
                                        'reason':'reviewed metadata','snippet':changed}]})
