import json
from types import SimpleNamespace
import pytest
from src.schema import Event
from src.schema.ttt_updates import walk, change_node
from src.schema.ttt import TTTNodeStatus as S
from src.workflow.ltm_wm import LTMWMController
from src.memory.longterm_memory.path_memory_v1.training.retrieval import Retriever


@pytest.fixture
def controller(monkeypatch):
    r=Retriever(lambda **kw:'{}')
    r.identify=lambda *a:{'known':[{'id':'E1','concept':'URL','value':'url-a'}],
       'wanted':{'concept':'URL'},'constraints':[],'retrieval_query':'q'}
    events=['A potentially malicious URL click was detected','Suspicious sequence of exploration activities','Malicious URL was clicked on that device']
    monkeypatch.setattr('src.workflow.ltm_wm.available_events',lambda *a:(events,{}))
    def select(c,q,i,es,cs):
        chosen=next((c for c in cs if c.get('pm_id')=='pm_946f48bf4383cee1' and c.get('route_id')=='route_a4a73f68d563b0d7'),None)
        return ({**chosen,'values':['url-a']},{'decision':'match'}) if chosen else (None,{'decision':'no_match'})
    r.select=select
    return LTMWMController(SimpleNamespace(),lambda **kw:'{}',retriever=r)


def initial(controller):
    return controller.initialize(Event(event_id='case',event_name='test',message='q',source='test'),{'context':'url-a','question':'q'})


def output(controller,tree,value):
    task=next(n for n,_ in walk(tree) if n.node_id==tree.next_task_id)
    es={e['id']:e for e in controller.entities(tree)}
    row={'value':value,'AlertId':'a','AlertTitle':task.metadata['event_title'], 'EntityIndex':0,
        'SourceRowHash':'snapshot','matched_input':es[task.metadata['input_entity_ids'][0]]['value']}
    execution=SimpleNamespace(query_success=True,execution_id='exec-'+task.node_id)
    return controller.observe(tree,task,execution,json.dumps([row]))


def test_initial_has_only_grounded_ge(controller):
    tree=initial(controller)
    assert [n.level for n,_ in walk(tree)]==[1,2]
    assert tree.next_task_id is None
    assert tree.root_nodes[0].metadata['procedure_runs']==[]


def test_full_pm_materializes_only_after_observation(controller):
    start=initial(controller)
    first=controller.plan(start)
    assert len([n for n,_ in walk(first) if n.level==3])==1
    assert len(controller.entities(first))==1
    observed=output(controller,first,'sid-observed')
    second=controller.plan(observed)
    t2=next(n for n,_ in walk(second) if n.node_id=='T2')
    assert t2.metadata['depends_on']==['T1']
    assert t2.metadata['input_entity_ids']==['E2']
    assert controller.entities(second)[1]['value']=='sid-observed'
    assert len(controller.retrievals)==1
    third=controller.plan(output(controller,second,'host-observed'))
    assert next(n for n,_ in walk(third) if n.node_id=='T3').metadata['depends_on']==['T2']
    done=controller.finish(output(controller,third,'url-b'))
    assert done.root_nodes[0].metadata['procedure_runs'][0]['status']=='completed'
    assert len(controller.entities(start))==1  # snapshots must not mutate
    assert controller.validate(done)


def test_reject_unobserved_dependency(controller):
    tree=controller.plan(output(controller,controller.plan(initial(controller)),'sid'))
    task=next(n for n,_ in walk(tree) if n.node_id=='T2')
    invalid=change_node(tree,'T2',metadata={**task.metadata,'input_entity_ids':['E1']})
    with pytest.raises(ValueError):controller.validate(invalid)


def test_empty_or_failed_step_cannot_spawn_next(controller):
    for status in (S.DONE,S.BLOCKED):
        tree=controller.plan(initial(controller))
        tree=change_node(tree,'T1',status=status)
        after=controller.plan(tree)
        assert len([n for n,_ in walk(after) if n.level==3])==1
        assert after.root_nodes[0].metadata['procedure_runs'][0]['status']=='paused'


def test_observation_wrong_event_rejected_atomically(controller):
    tree=controller.plan(initial(controller));node=next(n for n,_ in walk(tree) if n.node_id=='T1')
    row={'value':'sid','AlertId':'a','AlertTitle':'wrong','EntityIndex':0,'SourceRowHash':'s','matched_input':'url-a'}
    with pytest.raises(ValueError,match='event mismatch'):
        controller.observe(tree,node,SimpleNamespace(query_success=True,execution_id='x'),json.dumps([row]))
    assert len(controller.entities(tree))==1


def test_sql_driver_quoting_handles_bytearray(controller):
    from mysql.connector.conversion import MySQLConverter
    from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
    controller.backend=SimpleNamespace(db=SimpleNamespace(connection=SimpleNamespace(converter=MySQLConverter(),sql_mode='')))
    tree=controller.plan(initial(controller));task=next(n for n,_ in walk(tree) if n.node_id=='T1')
    sql=controller.sql(tree,task)
    assert 'bytearray' not in sql
    assert "'url-a'" in sql
    ReadOnlySQLValidator.validate(sql)


def test_budget_stop_does_not_leave_active_procedure(controller):
    tree=controller.plan(initial(controller))
    stopped=controller.stop(tree,'budget_exhausted')
    assert stopped.next_task_id is None
    assert stopped.root_nodes[0].metadata['procedure_runs'][0]['status']=='paused_budget_exhausted'
    assert next(n for n,_ in walk(stopped) if n.level==3).status==S.BLOCKED


def bootstrap_controller(controller):
    from mysql.connector.conversion import MySQLConverter
    controller.retriever.identify=lambda *a:{'known':[], 'wanted':{'concept':'URL'},
        'constraints':[], 'retrieval_query':'blocked C2'}
    class Cursor:
        def execute(self, sql): self.sql=sql
        def fetchall(self): return [("C2 ' alert",'URL'),("C2 ' alert",'Host_Name')]
    controller.backend=SimpleNamespace(query_count=0,db=SimpleNamespace(cursor=Cursor(),
        connection=SimpleNamespace(converter=MySQLConverter(),sql_mode='')))
    controller.retriever.ask=lambda *a:{'decision':'match','event_title':"C2 ' alert",'output_concepts':['URL']}
    return controller


def test_empty_known_bootstraps_observed_entities_then_pm(controller):
    from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
    controller=bootstrap_controller(controller)
    start=initial(controller)
    assert controller.entities(start)==[]
    tree=controller.plan(start)
    node=next(n for n,_ in walk(tree) if n.level==3)
    assert node.metadata['layer']=='BOOTSTRAP'
    assert node.metadata['output_entity_ids']==[]
    assert controller.entities(tree)[0]['concept']=='SecurityAlert_Title'
    sql=controller.sql(tree,node)
    ReadOnlySQLValidator.validate(sql)
    assert "Concept IN ('URL')" in sql
    row={'value':'url-a','Concept':'URL','AlertId':'a','AlertTitle':"C2 ' alert",
        'SourceRowHash':'s','EntityIndex':0,'matched_input':"C2 ' alert"}
    tree=controller.observe(tree,node,SimpleNamespace(query_success=True,execution_id='exec'),json.dumps([row]))
    entity=next(e for e in controller.entities(tree) if e['concept']=='URL')
    assert entity['value']=='url-a'
    next_tree=controller.plan(tree)
    task=next(n for n,_ in walk(next_tree) if n.node_id==next_tree.next_task_id)
    assert task.metadata['layer']=='PM'
    assert task.metadata['input_entity_ids']==[entity['id']]
    assert [r['layer'] for r in controller.retrievals]==['BOOTSTRAP','PM']


def test_bootstrap_rejects_invented_title(controller):
    controller=bootstrap_controller(controller)
    controller.retriever.ask=lambda *a:{'decision':'match','event_title':'invented','output_concepts':['URL']}
    with pytest.raises(ValueError,match='observed title'):controller.plan(initial(controller))


def test_empty_bootstrap_does_not_invent_entities_or_repeat(controller):
    controller=bootstrap_controller(controller)
    tree=controller.plan(initial(controller));node=next(n for n,_ in walk(tree) if n.level==3)
    tree=controller.observe(tree,node,SimpleNamespace(query_success=True,execution_id='exec'),'[]')
    tree=controller.plan(tree)
    assert tree.next_task_id is None
    assert all(e['concept']=='SecurityAlert_Title' for e in controller.entities(tree))
    assert len([n for n,_ in walk(tree) if n.level==3])==1


def test_bootstrap_repairs_invalid_selection_once(controller):
    controller=bootstrap_controller(controller)
    requests=[]
    def ask(prompt,payload):
        requests.append(payload)
        return {'decision':'match','event_title':"C2 ' alert",
                'output_concepts':['URL']*4 if len(requests)==1 else ['URL']}
    controller.retriever.ask=ask
    tree=controller.plan(initial(controller))
    assert tree.next_task_id=='T1'
    assert len(requests)==2 and requests[1]['error']['previous_response']['output_concepts']==['URL']*4
