"""Test-derived overlays preserve training evidence and respect retrieval boundaries."""
from copy import deepcopy
import json

import pytest

from src.memory.longterm_memory.procedural_memory.flows.catalog import (
    EPISODIC, PACKAGE, apply_adaptations, compose_catalog,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession


def signed(document):
    document.pop('catalog_id', None)
    document['catalog_id'] = digest(document)
    return TrajectorySnippetStore(document)


@pytest.fixture
def training_store():
    store = compose_catalog(json.loads((PACKAGE / 'catalog.json').read_text()), json.loads(EPISODIC.read_text()))
    document = deepcopy(store.document)
    document['disabled_procedural_ids'] = json.loads((PACKAGE / 'activation.json').read_text())['disabled_flow_ids']
    return signed(document)


def overlay(**kwargs):
    return {'provenance': 'evaluation_failure_adaptation', 'new_templates': {}, 'new_episodes': [], 'new_flows': [], 'guidance_patches': [], **kwargs}


def fresh_flow(store):
    flow = deepcopy(next(s for s in store.snippets.values() if len(s['steps']) > 1))
    flow['id'] = 'test_evaluation_flow'
    return flow


def test_overlay_does_not_mutate_training_store_or_suspension_policy(training_store):
    before = deepcopy(training_store.document)
    flow = fresh_flow(training_store)
    result = apply_adaptations(training_store, overlay(new_flows=[flow]))
    assert training_store.document == before
    assert flow['id'] not in training_store.snippets
    assert flow['id'] in result.snippets
    assert result.document['disabled_procedural_ids'] == before['disabled_procedural_ids']
    assert result.document['catalog_id'] != before['catalog_id']
    assert result.document['memory_provenance'] == 'training_catalog_plus_evaluation_failure_adaptation'
    # Detached copies also prevent later overlay edits changing the loaded catalog.
    flow['steps'][0]['planner_guide'] = 'mutated by caller'
    assert result.snippets[flow['id']]['steps'][0]['planner_guide'] != 'mutated by caller'


@pytest.mark.parametrize('provenance', [None, 'training', ''])
def test_overlay_requires_explicit_evaluation_provenance(training_store, provenance):
    with pytest.raises(ValueError, match='adaptation_provenance_required'):
        apply_adaptations(training_store, overlay(provenance=provenance))


def test_overlay_rejects_template_and_snippet_collisions(training_store):
    tid, template = next(iter(training_store.templates.items()))
    changed = deepcopy(template)
    changed['sql'] = 'SELECT 1'
    with pytest.raises(ValueError, match='adaptation_template_collision'):
        apply_adaptations(training_store, overlay(new_templates={tid: changed}))
    # Identical template reuse is allowed; mutating SQL under an existing ID is not.
    apply_adaptations(training_store, overlay(new_templates={tid: template}))
    existing = next(iter(training_store.snippets.values()))
    with pytest.raises(ValueError, match='adaptation_snippet_collision'):
        apply_adaptations(training_store, overlay(new_flows=[existing]))


@pytest.mark.parametrize('reference', ['missing_episode', 'wrong_existing_episode'])
def test_flow_steps_require_matching_single_step_episodic_owner(training_store, reference):
    flow = fresh_flow(training_store)
    if reference == 'wrong_existing_episode':
        reference = next(s['id'] for s in training_store.snippets.values() if len(s['steps']) == 1 and s['steps'][0]['template_id'] != flow['steps'][0]['template_id'])
    flow['steps'][0]['episodic_id'] = reference
    with pytest.raises(ValueError, match='adapted_flow_episodic_reference_mismatch'):
        apply_adaptations(training_store, overlay(new_flows=[flow]))


@pytest.mark.parametrize('wrong_partition', ['single_step_flow', 'multi_step_episode'])
def test_overlay_enforces_memory_partition_step_counts(training_store, wrong_partition):
    if wrong_partition == 'single_step_flow':
        item = deepcopy(next(s for s in training_store.snippets.values() if len(s['steps']) == 1))
        item['id'] = 'test_wrong_partition'
        patch = overlay(new_flows=[item])
    else:
        item = fresh_flow(training_store)
        patch = overlay(new_episodes=[item])
    with pytest.raises(ValueError, match='adaptation_step_count_mismatch'):
        apply_adaptations(training_store, patch)


def test_guidance_updates_every_intent_variant_without_changing_sql_or_dependencies(training_store):
    flow = next(s for s in training_store.snippets.values() if len(s['steps']) > 1 and len(s.get('intent_variants', [])) > 1)
    note, caution = '核对目标告警及角色；字段缺失时再次检索。', '不要把服务运行账户当成注册者。'
    patch = {'id': 'test_patch', 'flow_id': flow['id'], 'source_cases': ['incident-test-q0001'], 'step_guides': {'s1': note}, 'caution_add': [caution, caution]}
    result = apply_adaptations(training_store, overlay(guidance_patches=[patch]))
    updated = result.snippets[flow['id']]
    assert note in updated['steps'][0]['planner_guide']
    assert updated['caution'].count(caution) == 1
    for variant in updated['intent_variants']:
        assert note in next(g['guide'] for g in variant['step_guides'] if g['step'] == 's1')
        assert variant['caution'].count(caution) == 1
    assert updated['bindings'] == flow['bindings']
    assert result.templates == training_store.templates
    assert note not in flow['steps'][0]['planner_guide']
    assert result.document['disabled_procedural_ids'] == training_store.document['disabled_procedural_ids']


def output_annotation_router(training_store):
    # This real chain projects AlertId/Title then ApplicationId/Application.
    # Category appears ONLY in the first template's WHERE clause.
    flow = deepcopy(training_store.snippets['proc_c9128d43a1d099372df2b1c3'])
    atoms = [deepcopy(training_store.snippets[step['episodic_id']]) for step in flow['steps']]
    for snippet in [flow, *atoms]:
        for step in snippet['steps']:
            for value in step['outputs'].values():
                value['entity_concept'] = None
                value['entity_kind'] = None
    tids = {step['template_id'] for step in flow['steps']}
    templates = {tid: deepcopy(training_store.templates[tid]) for tid in tids}
    store = signed({'format_version': 'trajectory-snippets-2', 'snippets': [flow, *atoms], 'templates': templates})
    schema = {}
    for template in templates.values():
        for table, fields in template['schema_fields'].items():
            schema.setdefault(table, {}).update({field: 'text' for field in fields})
    session = ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)), store)
    return session, flow['id']


def test_semantic_projection_mapping_recovers_missing_output_annotation(training_store):
    session, fid = output_annotation_router(training_store)
    result = session.retrieve({'known': ['Field:Title'], 'wanted': ['Field:ApplicationId'], 'intent': '按告警标题查询云应用标识'})
    assert result['memory_type'] == 'procedural'
    assert fid in {item['id'] for item in result['candidates']}


def test_predicate_only_field_does_not_count_as_retrievable_output(training_store):
    session, fid = output_annotation_router(training_store)
    result = session.retrieve({'known': ['Field:Title'], 'wanted': ['Field:Category'], 'intent': '按告警标题查询类别'})
    assert not any(fid == item['id'] for record in session.retrievals for item in record['candidates'])
    assert result['route'] == 'semantic'


@pytest.mark.parametrize('entry', ['Field:UserDisplayName', 'Account_UPN'])
def test_display_name_search_entry_retains_confirmed_upn_entry(training_store, entry):
    # The first query searches display name OR UPN; only its returned UPN is
    # eligible for the next query. Display name is not declared an Account_Name.
    ids = ['proc_f0f739adc78a1c3f5f87e6d5', 'proc_03a53afd6657f28a251d0790']
    atoms = [deepcopy(training_store.snippets[eid]) for eid in ids]
    flow = deepcopy(atoms[0])
    flow.update(id='test_display_name_identity_chain', trigger_entity={'concept': 'Field:UserDisplayName', 'kind': 'physical_field_or_scope'}, steps=[], bindings=[])
    for index, atom in enumerate(atoms, 1):
        step = deepcopy(atom['steps'][0])
        step.update(id=f's{index}', episodic_id=atom['id'])
        flow['steps'].append(step)
    flow['required_inputs'] = deepcopy(atoms[0]['required_inputs'])
    flow['bindings'] = [{'from': 's1', 'to': 's2', 'from_column': 'UserPrincipalName', 'to_field': 'UserPrincipalName', 'to_parameter': 'p1', 'transform': 'identity'}]
    templates = {step['template_id']: deepcopy(training_store.templates[step['template_id']]) for step in flow['steps']}
    store = signed({'format_version': 'trajectory-snippets-2', 'snippets': [*atoms], 'templates': templates, 'memory_catalog_ids': {}})
    store = apply_adaptations(store, overlay(new_flows=[flow]))
    schema = {}
    for template in templates.values():
        for table, fields in template['schema_fields'].items():
            schema.setdefault(table, {}).update({field: 'text' for field in fields})
    session = ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)), store)
    result = session.retrieve({'known': [entry], 'wanted': ['IP'], 'intent': '查询用户登录IP和风险'})
    assert result['memory_type'] == 'procedural'
    assert flow['id'] in {item['id'] for item in result['candidates']}
    assert flow['trigger_entity']['concept'] != 'Account_Name'
