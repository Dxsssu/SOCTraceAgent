"""Executable contracts for G/E-first planning, PM composition and EM clue loops."""
from copy import deepcopy
import json

import pytest
import yaml

from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest, normalize_sql
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
from src.schema.itt import itt_payload
from src.schema.planner_operations import apply_planner_operations
from src.schema.ttt_updates import walk

URL = 'https://observed.test/link'
SCHEMA = {'EmailUrlInfo': {'Url':'text','NetworkMessageId':'text'},
          'EmailEvents': {'NetworkMessageId':'text','SenderFromAddress':'text'},
          'EmailAttachmentInfo': {'NetworkMessageId':'text','SHA256':'text'}}


def catalog(monkeypatch, *, clue_only=False, multi_input=False):
    templates, atoms = {}, []
    def atom(sid, sql, concept, kind):
        template, _ = normalize_sql(sql, SCHEMA)
        templates[template['id']] = template
        node = {'id':sid,'name':'邮件调查 '+sid,'intent':'邮件调查','kind':'atomic','status':'candidate',
            'trigger_entity':{'concept':concept,'kind':kind},
            'required_inputs':[{'step':'s1','parameter':p,'field':v['field'],
                'table':next(t for t, fs in template['schema_fields'].items() if v['field'] in fs)}
                for p,v in template['inputs'].items()],
            'steps':[{'id':'s1','template_id':template['id'],'planner_guide':'邮件调查', 'outputs':{}}],
            'bindings':[],'caution':[]}
        atoms.append(node)
        return node
    seed=atom('url_message',f"SELECT NetworkMessageId FROM EmailUrlInfo WHERE Url='{URL}' LIMIT 100",'URL','url')
    sender=atom('message_sender',"SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId IN ('mail-1') LIMIT 100",'Email_ID','network_message_id')
    message=atom('sender_message',"SELECT NetworkMessageId FROM EmailEvents WHERE SenderFromAddress='sender@test' LIMIT 100",'Email_Sender','sender_address')
    hashed=atom('message_hash',"SELECT SHA256 FROM EmailAttachmentInfo WHERE NetworkMessageId IN ('mail-1') LIMIT 100",'Email_ID','network_message_id')
    def flow(sid, a, b, column, target):
        result=deepcopy(a)
        result.update(id=sid,name='邮件调查 '+sid,kind='multi_step')
        result['steps']=[dict(a['steps'][0],episodic_id=a['id']),dict(b['steps'][0],id='s2',episodic_id=b['id'])]
        result['bindings']=[{'from':'s1','to':'s2','from_column':column,'to_field':target,'transform':'identity'}]
        result['required_inputs']=a['required_inputs'] + [dict(v,step='s2') for v in b['required_inputs']]
        return result
    first=flow('url_sender',seed,sender,'NetworkMessageId','NetworkMessageId')
    second=flow('sender_hash',message,hashed,'NetworkMessageId','NetworkMessageId')
    flows=[first,second]
    if clue_only:
        direct_hash=atom('sender_direct_hash',"SELECT a.SHA256 FROM EmailEvents e JOIN EmailAttachmentInfo a ON e.NetworkMessageId=a.NetworkMessageId WHERE e.SenderFromAddress='sender@test' LIMIT 100",'Email_Sender','sender_address')
        flows=[flow('message_sender_hash',sender,direct_hash,'SenderFromAddress','SenderFromAddress')]
    if multi_input:
        both=atom('two_input',"SELECT NetworkMessageId FROM EmailEvents WHERE NetworkMessageId='mail-1' AND SenderFromAddress='sender@test' LIMIT 100",'Email_ID','network_message_id')
        flows=[flow('multi_known',both,hashed,'NetworkMessageId','NetworkMessageId')]
    doc={'format_version':'trajectory-snippets-2','templates':templates,'snippets':atoms+flows}
    doc['catalog_id']=digest(doc)
    store=TrajectorySnippetStore(doc)
    monkeypatch.setattr('src.memory.longterm_memory.procedural_memory.flows.load_runtime_catalog',lambda:deepcopy(store))
    return store,flows


def router(store):
    return ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=SCHEMA)),deepcopy(store))


def entities(*items):
    return {'operations':[{'op':'add_goal','ref':'g','objective':'邮件调查'}]+[
        {'op':'add_entity','ref':f'e{i}','goal_id':'$g','objective':str(value),'concept':concept,'value':value}
        for i,(concept,value) in enumerate(items)]}


def test_bootstrap_allows_g_e_without_t_and_rejects_cross_goal_task_inputs():
    tree=apply_planner_operations(entities(('Email_ID','mail-1'),('Email_Sender','sender@test')),'event',1,allow_pending_entities=True)
    assert all(n.level!=3 for n,_ in walk(tree))
    tree=apply_planner_operations({'operations':[{'op':'add_task','entity_id':'E1','input_entity_ids':['E1','E2'],'objective':'共同输入'}]},'event',1,previous=tree,allow_pending_entities=True,require_next_task=True)
    tasks=[n for n,_ in walk(tree) if n.level==3]
    assert len(tasks)==1 and tasks[0].metadata['input_entity_ids']==['E1','E2']
    assert itt_payload(tree)[0]['children'][0]['children'][0]['input_entity_ids']==['E1','E2']
    extra=entities(('Email_ID','mail-2'))
    extra['operations'][0]['ref']='other'
    extra['operations'][1]['goal_id']='$other'
    extra['operations'][1]['ref']='other_entity'
    both=entities(('Email_ID','mail-1'))['operations']+extra['operations']
    tree=apply_planner_operations({'operations':both},'event',1,allow_pending_entities=True)
    with pytest.raises(ValueError,match='same G'):
        apply_planner_operations({'operations':[{'op':'add_task','entity_id':'E1','input_entity_ids':['E1','E2'],'objective':'invalid'}]},'event',2,previous=tree,allow_pending_entities=True)


def test_known_is_and_and_wanted_is_terminal_only(monkeypatch):
    store,_=catalog(monkeypatch,multi_input=True)
    p=router(store)
    query={'known':['Email_ID'],'wanted':['File_Hash'],'intent':'邮件调查'}
    p.retrieve(query)
    assert p.retrievals[0]['candidates']==[]
    p=router(store)
    assert p.retrieve(dict(query,known=['Email_ID','Email_Sender']))['candidates'][0]['id']=='multi_known'
    store,_=catalog(monkeypatch)
    p=router(store)
    result=p.retrieve({'known':['URL'],'wanted':['Email_ID'],'intent':'邮件调查'})
    assert p.retrievals[0]['candidates']==[]
    assert result['memory_type']=='episodic' and result['candidates'][0]['id']=='url_message'


def test_composition_rejects_namespace_mismatch(monkeypatch):
    store,flows=catalog(monkeypatch)
    p=router(store)
    query={'known':['URL'],'wanted':['File_Hash'],'intent':'邮件调查'}
    result=p.retrieve(query)
    assert result['layer']=='composed'
    assert result['candidates'][0]['component_ids']==['url_sender','sender_hash']
    p=router(store)
    for flow in flows:
        if flow['id']=='sender_hash':
            p.store.snippets[flow['id']]['memory_contract']=p.contract(p.store.snippets[flow['id']])
            for slot in p.store.snippets[flow['id']]['memory_contract']['known']:
                slot['kind']='recipient_address'
    p.retrieve(query)
    assert not next(x for x in p.retrievals if x.get('layer')=='composed')['candidates']


def fake_workflow(monkeypatch, *, clue_only=False, multi_input=False):
    store,_=catalog(monkeypatch,clue_only=clue_only,multi_input=multi_input)
    role_calls=[]
    def llm(system,text):
        data=yaml.safe_load(text.split('\n上一次输出')[0])
        if '你是 Reviewer' in system:
            return yaml.safe_dump({'summary':'真实查询结果','facts':[],'limitations':[]})
        if '你是 Verifier' in system:
            done=sum(r=='executor' for r in role_calls)
            return yaml.safe_dump({'can_answer':'yes','answer':'hash'} if done>= (3 if clue_only else 2 if multi_input else 4) else {'can_answer':'no'})
        if '你是 Executor' in system:
            role_calls.append('executor')
            return yaml.safe_dump({'sql':data.get('procedural_context',data.get('episodic_context'))['bound_sql']})
        role_calls.append('planner')
        if data.get('planning_phase')=='initialize_entities':
            return yaml.safe_dump(entities(('Email_ID','mail-1'),('Email_Sender','sender@test')) if multi_input else entities(('URL',URL)))
        if data.get('planning_phase')=='update_entities':
            return yaml.safe_dump({'operations':[{'op':'add_entity','ref':'mail','goal_id':'G1','objective':'mail-1','concept':'Email_ID','value':'mail-1','source_refs':['E1']}]})
        if not data['procedural_results'] and not data['episodic_results']:
            known=['Email_ID','Email_Sender'] if multi_input else ['Email_ID'] if clue_only and len(data['itt'][0]['children'])==2 else ['URL']
            return yaml.safe_dump({'memory_query':{'known':known,'wanted':['File_Hash'],'intent':'邮件调查'}})
        views=data['procedural_results'] or data['episodic_results']
        memory=views[-1]['candidates'][0]
        selector='procedure' if memory['memory_type']=='procedural' else 'episode'
        return yaml.safe_dump({'operations':[{'op':'add_task','entity_id':'E2' if clue_only and selector=='procedure' else 'E1','objective':'邮件调查',selector:{'id':memory['id'],'step':'s1','inputs':{}}}]})
    w=ExcytinBenchWorkflow(llm=llm);w.set_semantic_schema(SCHEMA)
    w.start({'context':f'{URL} mail-1 sender@test','question':'邮件调查文件哈希'})
    return w,role_calls


def test_composed_flow_executes_four_dependent_tasks_once(monkeypatch):
    w,calls=fake_workflow(monkeypatch)
    assert all(not e['children'] for g in w.state.ttt_history[0]['root_nodes'] for e in g['children'])
    assert len([n for n,_ in walk(w.state.ttt) if n.level==3])==4
    for result in ["[('mail-1',)]","[('sender@test',)]","[('mail-1',)]","[('abc123',)]"]:
        w.propose_next_action();w.accept_observation(result,query_success=True)
    assert calls.count('executor')==4
    assert calls.count('planner')==3
    assert w.propose_next_action().submit
    assert 'sender@test' in w.state.executions[2].sql


def test_multiple_existing_entities_feed_same_tasks(monkeypatch):
    w,calls=fake_workflow(monkeypatch,multi_input=True)
    tasks=[n for n,_ in walk(w.state.ttt) if n.level==3]
    assert len(tasks)==2 and all(n.metadata['input_entity_ids']==['E1','E2'] for n in tasks)
    for result in ["[('mail-1',)]","[('abc123',)]"]:
        w.propose_next_action();w.accept_observation(result,query_success=True)
    assert calls.count('executor')==2 and w.propose_next_action().submit


def test_em_new_clue_updates_e_then_rechecks_pm(monkeypatch):
    w,calls=fake_workflow(monkeypatch,clue_only=True)
    assert w._task_procedural_contexts['T1']['memory_type']=='episodic'
    w.propose_next_action();w.accept_observation("[('mail-1',)]",query_success=True)
    es=[n for n,_ in walk(w.state.ttt) if n.level==2]
    assert es[1].metadata['entity']['value']=='mail-1'
    assert w._task_procedural_contexts['T2']['memory_type']=='procedural'
    for result in ["[('sender@test',)]","[('abc123',)]"]:
        w.propose_next_action();w.accept_observation(result,query_success=True)
    assert w.propose_next_action().submit


def test_repeated_em_rows_are_not_new_clues(monkeypatch):
    store,_=catalog(monkeypatch)
    p=router(store)
    p.retrieve({'known':['URL'],'wanted':['Email_ID'],'intent':'邮件调查'})
    context=p.bind({'id':'url_message','step':'s1','inputs':{'p1':URL}})
    p.register('T1',context);p.observe('T1',"[('mail-1',)]",True)
    assert p.needs_pm_recheck
    p.needs_pm_recheck=False
    p.observe('T1',"[('mail-1',)]",True)
    assert not p.needs_pm_recheck


def test_required_known_entities_must_share_one_goal(monkeypatch):
    store,_=catalog(monkeypatch,multi_input=True)
    p=router(store)
    batch=entities(('Email_ID','mail-1'))['operations']
    batch += [ {'op':'add_goal','ref':'g2','objective':'独立目标'},
               {'op':'add_entity','ref':'other_sender','goal_id':'$g2','objective':'sender@test',
                'concept':'Email_Sender','value':'sender@test'} ]
    tree=apply_planner_operations({'operations':batch},'event',1,allow_pending_entities=True)
    p.set_entities(tree)
    p.retrieve({'known':['Email_ID','Email_Sender'],'wanted':['File_Hash'],'intent':'邮件调查'})
    assert not p.retrievals[0]['candidates']


def test_all_dag_sinks_contribute_outputs_and_wanted_kind_is_checked(monkeypatch):
    store,flows=catalog(monkeypatch)
    p=router(store)
    flow=deepcopy(flows[0])
    flow['bindings']=[]  # s1 and s2 are both DAG sinks.
    assert {'Email_ID','Email_Sender'} <= {x['concept'] for x in p.terminal_outputs(flow)}
    query=p.semantic.queries({'known':['URL'],'wanted':[{'concept':'Email_Sender','kind':'recipient_address'}]})
    assert not p.covers_wanted(p.terminal_outputs(flow),query['wanted'])


def test_entity_phase_rejects_invented_values_and_preallocated_tasks(monkeypatch):
    w,_=fake_workflow(monkeypatch)
    with pytest.raises(ValueError,match='present in the question'):
        w._apply_entity_updates(entities(('Email_ID','invented-mail')),{'context':URL,'question':'邮件调查'},'event',1)
    with pytest.raises(ValueError,match='G/E only'):
        w._apply_entity_updates({'operations':[{'op':'add_task','entity_id':'E1','objective':'early'}]},
            {'context':URL,'question':'邮件调查'},'event',1)


def test_complete_runtime_pm_contracts_and_em_family_count():
    from src.memory.longterm_memory.procedural_memory.flows.catalog import load_runtime_catalog
    store=load_runtime_catalog()
    enabled=[s for s in store.snippets.values() if len(s['steps'])>1 and s['id'] not in store.document['disabled_procedural_ids']]
    assert len(enabled)==65 and all(s.get('memory_contract') for s in enabled)
    assert sum(s['memory_contract'].get('auto_execution_allowed') is False for s in enabled)==5
    assert sum(len(s['steps'])==1 for s in store.snippets.values())==352
    for s in store.snippets.values():
        if len(s['steps']) != 1:
            continue
        template=store.templates[s['steps'][0]['template_id']]
        for parameter,spec in template['inputs'].items():
            assert any(spec.get('source_field',spec['field']) in fields for fields in template['schema_fields'].values())
        for item in s['required_inputs']:
            spec=template['inputs'][item['parameter']]
            assert item['field'] in {spec['field'],spec.get('source_field')}
    for s in enabled:
        terminal={st['id']:store.templates[st['template_id']]['outputs'] for st in s['steps']
                  if not any(e['from']==st['id'] for e in s['bindings'])}
        for output in s['memory_contract']['wanted']:
            assert output['column'] in terminal[output['step']]


def test_ip_input_role_hint_does_not_equate_output_roles(monkeypatch):
    store,_=catalog(monkeypatch)
    p=router(store)
    assert p.slot_matches({'concept':'IP','kind':'evidence_remote_ip'},'IP','sender_ipv4')
    wanted=p.semantic.queries({'known':['IP'],'wanted':[{'concept':'IP','kind':'login_ip'}]})['wanted']
    assert not p.covers_wanted([{'concept':'IP','kind':'evidence_remote_ip'}],wanted)
    assert not p._bridge_matches({'concept':'IP','kind':'evidence_remote_ip'},
                                 {'concept':'IP','kind':'login_ip'})


def test_unsafe_internal_pm_is_not_offered_or_bound(monkeypatch):
    store,_=catalog(monkeypatch,multi_input=True)
    p=router(store)
    flow=p.store.snippets['multi_known']
    flow['memory_contract']=p.contract(flow)
    flow['memory_contract'].update(auto_execution_allowed=False,auto_composition_allowed=False)
    p.retrieve({'known':['Email_ID','Email_Sender'],'wanted':['File_Hash'],'intent':'邮件调查'})
    assert not p.retrievals[0]['candidates']
    p.offered[flow['id']]=flow
    with pytest.raises(ValueError,match='unsafe internal'):
        p.bind({'id':flow['id'],'step':'s1','inputs':{}})


def test_same_entity_can_anchor_independent_goals_without_duplicate_in_one_goal(monkeypatch):
    w,_=fake_workflow(monkeypatch)
    batch=entities(('Email_ID','mail-1'))['operations']+[{'op':'add_goal','ref':'g2','objective':'第二目标'},
        {'op':'add_entity','ref':'second','goal_id':'$g2','objective':'sender@test','concept':'Email_Sender','value':'sender@test'}]
    tree=w._apply_entity_updates({'operations':batch},{'context':'mail-1 sender@test'},'event',1)
    op={'op':'add_entity','ref':'shared_value','goal_id':'G2','objective':'mail-1','concept':'Email_ID','value':'mail-1'}
    updated=w._apply_entity_updates({'operations':[op]},{'context':'mail-1 sender@test'},'event',2,previous=tree)
    assert len(updated.root_nodes[1].children)==2
    with pytest.raises(ValueError,match='Reuse the existing E'):
        w._apply_entity_updates({'operations':[op]},{'context':'mail-1 sender@test'},'event',3,previous=updated)
