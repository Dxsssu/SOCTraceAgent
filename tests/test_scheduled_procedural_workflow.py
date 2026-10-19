# Historical transport fixtures use the pre-bootstrap adapter; default G/E-first execution is covered by test_staged_memory_planning.py.
"""Flow execution uses real upstream data without per-step planning calls."""
from copy import deepcopy
from collections import Counter
import json

import pytest
import yaml

from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.flows import load_runtime_catalog
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest, normalize_sql
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore
from src.schema.ttt_updates import walk


def install(monkeypatch, snippets, templates):
    document = {'format_version': 'trajectory-snippets-2', 'snippets': snippets, 'templates': templates}
    document['catalog_id'] = digest(document)
    store = TrajectorySnippetStore(document)
    monkeypatch.setattr('src.memory.longterm_memory.procedural_memory.flows.load_runtime_catalog', lambda: store)
    schema = {}
    for template in templates.values():
        for table, fields in template['schema_fields'].items():
            schema.setdefault(table, {}).update({f: 'text' for f in fields})
    return store, schema


def fork_catalog(monkeypatch):
    source = load_runtime_catalog()
    flow = source.snippets['proc_3cce7d309146babc5a846c3e']
    templates = {s['template_id']: source.templates[s['template_id']] for s in flow['steps']}
    _, schema = install(monkeypatch, [flow], templates)
    query = {'known': ['Action'], 'wanted': ['Email_Sender', 'URL'], 'intent': '隔离邮件发件人和URL'}
    return flow, schema, query, {'p1': '%quarantine%', 'p2': '%remove%'}


def clue_catalog(monkeypatch, extra_input=False):
    schema = {'EmailUrlInfo': {'Url':'text', 'NetworkMessageId':'text'},
              'EmailEvents': {'NetworkMessageId':'text','SenderFromAddress':'text','SenderIPv4':'text'}}
    templates = {}
    def atom(sid, sql, concept, kind):
        template, _ = normalize_sql(sql, schema)
        templates[template['id']] = template
        return {'id':sid,'kind':'atomic','name':sid,'intent':'查邮件线索',
                'trigger_entity':{'concept':concept,'kind':kind},
                'required_inputs':[{'step':'s1','parameter':p,'table':next(iter(template['schema_fields'])),
                                    'field':v['field']} for p,v in template['inputs'].items()],
                'steps':[{'id':'s1','template_id':template['id'],'planner_guide':sid,'outputs':{}}],
                'bindings':[],'caution':[],'status':'candidate'}
    seed = atom('seed', "SELECT NetworkMessageId FROM EmailUrlInfo WHERE Url='https://known.test' LIMIT 100", 'URL','url')
    sender = atom('sender', "SELECT NetworkMessageId, SenderFromAddress FROM EmailEvents WHERE NetworkMessageId IN ('mail-1') LIMIT 100", 'Email_ID','network_message_id')
    sql = "SELECT SenderFromAddress, SenderIPv4 FROM EmailEvents WHERE SenderFromAddress='sender@test'"
    if extra_input:
        sql += " AND NetworkMessageId='mail-1'"
    source = atom('source', sql + ' LIMIT 100', 'Email_Sender','sender_address')
    flow = deepcopy(sender)
    flow.update(id='flow', kind='multi_step', name='邮件到发件人再到来源IP', intent='查询发件人和来源IP')
    flow['steps'] = [dict(sender['steps'][0], episodic_id='sender'),
                     dict(source['steps'][0], id='s2', episodic_id='source')]
    flow['bindings']=[{'from':'s1','to':'s2','from_column':'SenderFromAddress',
                       'to_field':'SenderFromAddress','transform':'identity'}]
    install(monkeypatch, [seed,sender,source,flow],templates)
    query={'known':['Email_ID'],'wanted':['Email_Sender','Field:SenderIPv4'],'intent':'查询发件人和来源IP'}
    return flow,schema,query,{'p1':['mail-1']}


def start_operations(spec, *, existing=False, selector='procedure'):
    prefix=[] if existing else [{'op':'add_goal','ref':'g','objective':'Investigate'},
                                {'op':'add_entity','ref':'e','goal_id':'$g','objective':'Known clue'}]
    return {'operations':prefix+[{'op':'add_task','entity_id':'E1' if existing else '$e',
                                 'objective':'Investigate known clue',selector:spec}]}


def harness(monkeypatch, *, catalog=fork_catalog, max_steps=25, planner_after=None, final_answer=True, step_inputs=None):
    flow,schema,query,inputs=catalog(monkeypatch)
    calls=Counter();payloads=[]
    def llm(system,user):
        data=yaml.safe_load(user.split('\n上一次输出')[0]);payloads.append(data)
        if 'procedural_context' in data or 'episodic_context' in data:
            calls['executor']+=1
            return yaml.safe_dump({'sql':data.get('procedural_context',data.get('episodic_context'))['bound_sql']})
        if '你是 Reviewer' in system:
            calls['reviewer']+=1
            return yaml.safe_dump({'summary':'返回真实记录','facts':[],'limitations':[]})
        if '你是 Verifier' in system:
            calls['verifier']+=1
            return yaml.safe_dump({'can_answer':'yes','answer':'done'} if final_answer else {'can_answer':'no'})
        calls['planner']+=1
        if calls['planner']==1:return yaml.safe_dump({'memory_query':query})
        if calls['planner']==2:
            spec={'id':flow['id'],'step':'s1','inputs':inputs}
            if step_inputs:spec['step_inputs']=step_inputs
            return yaml.safe_dump(start_operations(spec))
        assert planner_after is not None, 'Normal flow must not call Planner between steps'
        return yaml.safe_dump(planner_after(data,flow))
    w=ExcytinBenchWorkflow(staged_planning=False, llm=llm,max_steps=max_steps);w.set_semantic_schema(schema);w.start('Investigate')
    return w,calls,payloads


def test_entire_fork_preallocated_then_runs_without_planner_or_verifier_between_steps(monkeypatch):
    w,calls,_=harness(monkeypatch)
    tasks=[n for n,_ in walk(w.state.ttt) if n.level==3]
    assert [n.node_id for n in tasks]==['T1','T2','T3']
    assert tasks[1].metadata['procedure']['depends_on']==['T1']
    assert tasks[2].metadata['procedure']['depends_on']==['T1']
    assert w._task_procedural_contexts['T2']['deferred']
    assert 'bound_sql' not in w._task_procedural_contexts['T2']
    w.propose_next_action()
    w.accept_observation("[('mail-a','network-a'),('mail-b','network-b')]",query_success=True)
    assert calls==Counter(planner=2,executor=1,reviewer=1)
    second=w.propose_next_action();assert "'mail-a', 'mail-b'" in second.content
    w.accept_observation("[('mail-a','A','a@test'),('mail-b','B','b@test')]",query_success=True)
    third=w.propose_next_action();assert "'network-a', 'network-b'" in third.content
    w.accept_observation("[('network-a','https://a'),('network-b','https://b')]",query_success=True)
    assert calls==Counter(planner=2,executor=3,reviewer=3,verifier=1)
    assert w.propose_next_action().submit
    assert w._procedural_session.runs['T1']['status']=='completed'


@pytest.mark.parametrize('observation,success,reason',[
    ('[]',True,'dependency_has_no_values'),
    ("[('mail-a','network-a')] ... (Truncated)",True,'dependency_missing_failed_or_truncated'),
    ('SQL error',False,'upstream_sql_failed'),
])
def test_bad_upstream_interrupts_unexecuted_tasks(monkeypatch,observation,success,reason):
    w,calls,_=harness(monkeypatch,final_answer=False)
    replans=[]
    w._replan=lambda tree,review: replans.append(tree) or tree
    w.propose_next_action();w.accept_observation(observation,query_success=success)
    assert w._procedural_session.runs['T1']['reason']==reason
    assert [w._find_node(w.state.ttt,t).status.value for t in ['T2','T3']]==['blocked','blocked']
    assert calls['executor']==1 and calls['verifier']==1 and len(replans)==1


def test_budget_stops_preallocated_tail_and_submits_without_answer(monkeypatch):
    w,calls,_=harness(monkeypatch,max_steps=2,final_answer=False)
    w.propose_next_action();w.accept_observation("[('a','n')]",query_success=True)
    action=w.propose_next_action()
    assert action.submit and '无法确认' in action.content
    assert calls['planner']==2 and calls['executor']==1
    assert w._find_node(w.state.ttt,'T2').status.value=='blocked'


def test_scalar_ambiguity_resumes_existing_task_with_observed_selection(monkeypatch):
    def resume(data,flow):
        return {'operations':[{'op':'select_task','task_id':'T2',
            'procedure':{'id':flow['id'],'step':'s2','run':'T1','inputs':{},
                         'selections':{'s1.SenderFromAddress':['b@test']}}}]}
    w,calls,_=harness(monkeypatch,catalog=clue_catalog,final_answer=False,planner_after=resume)
    w.propose_next_action();w.accept_observation("[('a','a@test'),('b','b@test')]",query_success=True)
    assert calls['planner']==3 and calls['executor']==1
    assert len([n for n,_ in walk(w.state.ttt) if n.level==3])==2
    assert w.state.ttt.next_task_id=='T2'
    assert "'b@test'" in w.propose_next_action().content
    assert w._procedural_session.runs['T1']['status']=='active'


def test_future_external_inputs_can_be_declared_without_faking_derived_values(monkeypatch):
    w,calls,_=harness(monkeypatch,catalog=lambda m:clue_catalog(m,extra_input=True),
                      step_inputs={'s2':{'p2':'mail-1'}})
    w.propose_next_action();w.accept_observation("[('mail-1','sender@test')]",query_success=True)
    sql=w.propose_next_action().content
    assert "'sender@test'" in sql and "'mail-1'" in sql
    assert calls['planner']==2


def test_future_step_cannot_execute_before_predecessors(monkeypatch):
    w,_,_=harness(monkeypatch)
    with pytest.raises(ValueError,match='Earlier procedural steps'):
        w._procedural_session.bind_scheduled('T3')


def test_em_new_clue_restarts_pm_retrieval_and_then_runs_the_selected_flow(monkeypatch):
    flow,schema,query,inputs=clue_catalog(monkeypatch)
    calls=Counter();recheck_seen=[]
    def llm(system,user):
        d=yaml.safe_load(user.split('\n上一次输出')[0])
        if 'episodic_context' in d or 'procedural_context' in d:
            calls['executor']+=1
            return yaml.safe_dump({'sql':d.get('episodic_context',d.get('procedural_context'))['bound_sql']})
        if '你是 Reviewer' in system:
            return yaml.safe_dump({'summary':'获得线索','facts':[{'NetworkMessageId':'mail-1'}],'limitations':[]})
        if '你是 Verifier' in system:
            calls['verifier']+=1
            return yaml.safe_dump({'can_answer':'no'} if calls['verifier']==1 else {'can_answer':'yes','answer':'done'})
        calls['planner']+=1
        phase=calls['planner']
        if phase==1:return yaml.safe_dump({'memory_query':{'known':['URL'],'wanted':['Email_ID'],'intent':'从URL发现邮件ID'}})
        if phase==2:return yaml.safe_dump(start_operations({'id':'seed','step':'s1','inputs':{'p1':'https://known.test'}},selector='episode'))
        if phase==3:
            recheck_seen.append(d['procedural_memory']['must_query_after_new_em_evidence'])
            return yaml.safe_dump({'memory_query':query})
        if phase==4:return yaml.safe_dump(start_operations({'id':'flow','step':'s1','inputs':{'p1':['mail-1']}},existing=True))
        raise AssertionError('Unexpected per-step Planner call')
    w=ExcytinBenchWorkflow(staged_planning=False, llm=llm);w.set_semantic_schema(schema);w.start('Find sender IP from URL')
    w.propose_next_action();w.accept_observation("[('mail-1',)]",query_success=True)
    assert recheck_seen==[True]
    history=w.get_logging()['memory_routing_history']
    assert [r.get('layer',r['memory_type']) for r in history]==['procedural','composed','episodic','procedural']
    assert history[-1]['candidates'][0]['id']=='flow'
    assert [n.node_id for n,_ in walk(w.state.ttt) if n.level==3]==['T1','T2','T3']
    w.propose_next_action();w.accept_observation("[('mail-1','sender@test')]",query_success=True)
    assert calls['planner']==4 and calls['verifier']==1
    assert "'sender@test'" in w.propose_next_action().content
    w.accept_observation("[('sender@test','192.0.2.1')]",query_success=True)
    assert w.propose_next_action().submit and calls['verifier']==2


def test_missing_later_input_pauses_and_can_resume_existing_task(monkeypatch):
    def resume(data, flow):
        assert data['procedural_memory']['active_runs'][0]['status'] == 'paused'
        return {'operations':[{'op':'select_task','task_id':'T2',
            'procedure':{'id':flow['id'],'step':'s2','run':'T1','inputs':{'p2':'mail-1'}}}]}
    w,calls,_=harness(monkeypatch,catalog=lambda m:clue_catalog(m,extra_input=True),
                      final_answer=False,planner_after=resume)
    w.propose_next_action();w.accept_observation("[('mail-1','sender@test')]",query_success=True)
    assert calls['planner']==3
    assert "'mail-1'" in w.propose_next_action().content


def test_em_new_evidence_requires_query_before_any_new_operations(monkeypatch):
    from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
    from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
    _,schema,_,_=clue_catalog(monkeypatch)
    pm=ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)))
    query={'known':['URL'],'wanted':['Email_ID'],'intent':'邮件ID'}
    pm.retrieve(query)
    context=pm.bind({'id':'seed','step':'s1','inputs':{'p1':'https://known.test'}})
    pm.register('T1',context);pm.observe('T1',"[('mail-1',)]",True)
    with pytest.raises(ValueError,match='New Episodic evidence'):
        pm.prepare({'operations':[]})
    pm.retrieve(query)
    assert not pm.needs_pm_recheck
    pm.observe('T1',"[('mail-1',)]",True)
    assert not pm.needs_pm_recheck  # Duplicate observations do not reset the router.
