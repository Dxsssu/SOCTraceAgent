"""Layer order, lossless partition, and flow-to-experience references."""
from copy import deepcopy
import json

import pytest

from src.memory.longterm_memory.procedural_memory.flows.catalog import (
    SOURCE, PACKAGE, EPISODIC, split_catalog, compose_catalog, load_runtime_catalog,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession


def test_lossless_split_and_all_flow_steps_reference_episodic():
    source = json.loads(SOURCE.read_text())
    pm, em = split_catalog(source)
    assert pm == json.loads((PACKAGE / 'catalog.json').read_text())
    assert em == json.loads(EPISODIC.read_text())
    assert len(pm['flows']) == 99 and len(em['snippets']) == 320
    assert 'templates' not in pm and len(em['templates']) == 320
    assert em['templates'] == source['templates']
    combined = compose_catalog(pm, em)
    assert set(combined.snippets) == {s['id'] for s in source['snippets']}
    for original in source['snippets']:
        restored = deepcopy(combined.snippets[original['id']])
        for step in restored['steps']:
            if len(restored['steps']) > 1:
                atom = combined.snippets[step.pop('episodic_id')]
                assert len(atom['steps']) == 1
                assert atom['steps'][0]['template_id'] == step['template_id']
        assert restored == original
    broken = deepcopy(pm)
    broken['flows'][0]['steps'][0]['episodic_id'] = 'invented'
    broken['catalog_id'] = digest({k:v for k,v in broken.items() if k != 'catalog_id'})
    with pytest.raises(ValueError, match='reference_mismatch'):
        compose_catalog(broken, em)


def router():
    # Exercise all structural routes independently of the deployment suspension list.
    store = compose_catalog(json.loads((PACKAGE / 'catalog.json').read_text()), json.loads(EPISODIC.read_text()))
    schema = {}
    for template in store.templates.values():
        for table, fields in template['schema_fields'].items():
            schema.setdefault(table, {}).update({f:'text' for f in fields})
    return ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)),store)


QUERY = {'known':['Action'], 'wanted':['Email_ID'], 'intent':'查询隔离或移除邮件的消息标识'}


def test_pm_first_decline_then_em_then_semantic():
    p = router()
    first = p.retrieve(QUERY)
    assert first['memory_type'] == 'procedural' and first['candidates']
    assert all(len(c['steps']) > 1 for c in first['candidates'])
    assert len(p.retrievals) == 1 and not p.semantic.retrievals
    # A changed intent still declines the same known/wanted contract; the router
    # checks composition before offering an EM, without exact-string repetition.
    second = p.retrieve({**QUERY, 'intent':'进一步查询隔离邮件标识'})
    assert second.get('layer') == 'composed' and second['candidates']
    third = p.retrieve(QUERY)
    assert third['memory_type'] == 'episodic' and third['candidates']
    assert all(len(c['steps']) == 1 for c in third['candidates'])
    assert not p.semantic.retrievals
    assert p.retrieve({**QUERY, 'layer':'semantic'}) is None
    assert [r.get('layer', r['memory_type']) for r in p.retrievals] == ['procedural','composed','episodic']
    p.semantic.retrieve(QUERY)
    assert len(p.semantic.retrievals) == 1


def test_pm_requires_output_coverage_not_one_matching_word():
    p=router()
    result=p.retrieve({**QUERY,'wanted':['Email_ID','uncovered_investigation_target']})
    assert p.retrievals[0]['memory_type']=='procedural'
    assert not p.retrievals[0]['candidates']
    # Single-step experiences may serve an intermediate goal chosen by Planner.
    assert result['memory_type']=='episodic'


def test_pm_miss_automatically_queries_em_and_em_miss_reaches_semantic():
    schema={'EmailEvents':{'NetworkMessageId':'text','SenderFromAddress':'text','TimeGenerated':'text'}}
    p=ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)))
    query={'known':[{'concept':'Email_ID','kind':'network_message_id'}], 'wanted':['Email_Sender'],'intent':'查询发件人'}
    em=p.retrieve(query)
    assert em['memory_type']=='episodic' and em['candidates']
    assert not p.retrievals[0]['candidates']
    assert not p.semantic.retrievals
    spec={'id':em['candidates'][0]['id'],'step':'s1','inputs':{'p1':['live-mail']}}
    with pytest.raises(ValueError, match='Use procedure'):
        p.prepare({'operations':[{'op':'add_task','entity_id':'E1','objective':'sender','procedure':spec}]})
    clean,contexts=p.prepare({'operations':[{'op':'add_task','entity_id':'E1','objective':'sender','episode':spec}]})
    assert 'episode' not in clean['operations'][0]
    assert contexts[0]['memory_type']=='episodic'
    assert contexts[0]['episodic_id']==spec['id']
    assert 'live-mail' in contexts[0]['bound_sql']
    p=ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema={})))
    result=p.retrieve(query)
    assert result['route']=='semantic' and not result['candidates']
    assert [r.get('layer',r['memory_type']) for r in p.retrievals]==['procedural','composed','episodic']


def test_cross_layer_selection_is_exclusive():
    p=router()
    for task in [{'procedure':{},'episode':{}},{'episode':{},'table':'EmailEvents'}]:
        with pytest.raises(ValueError,match='exactly one'):
            import yaml
            p.parse(yaml.safe_dump({'operations':[{'op':'add_task','entity_id':'E1','objective':'test',**task}]}))


def test_suspended_flows_are_retained_but_not_retrieved_or_bound():
    store = load_runtime_catalog()
    disabled = set(store.document['disabled_procedural_ids'])
    assert len(disabled) == 57
    assert disabled <= store.snippets.keys()
    p = router()
    p.disabled = disabled
    result = p.retrieve(QUERY)
    assert all(c['id'] not in disabled for c in result['candidates'])
    sid = next(iter(disabled))
    p.offered[sid] = store.snippets[sid]
    with pytest.raises(ValueError, match='temporarily disabled'):
        p.bind({'id':sid,'step':'s1','inputs':{}})
