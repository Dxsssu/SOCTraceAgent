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


def initial(controller, located=False):
    tree=controller.initialize(Event(event_id='case',event_name='test',message='q',source='test'),{'context':'url-a','question':'q'})
    if located:
        tree=controller.build_tree(tree.event_id)
    return tree


def output(controller,tree,value):
    task=next(n for n,_ in walk(tree) if n.node_id==tree.next_task_id)
    es={e['id']:e for e in controller.entities(tree)}
    row={'value':value,'AlertId':'a','AlertTitle':task.metadata['event_title'], 'EntityIndex':0,
        'SourceRowHash':'snapshot','matched_input':es[task.metadata['input_entity_ids'][0]]['value']}
    execution=SimpleNamespace(query_success=True,execution_id='exec-'+task.node_id)
    return controller.observe(tree,task,execution,json.dumps([row]))


def test_initial_has_only_grounded_ge(controller):
    tree=initial(controller)
    assert tree.root_nodes==()
    assert tree.next_task_id is None
    assert controller.preparation_entities[0]['value']=='url-a'


def test_full_pm_materializes_only_after_observation(controller):
    start=initial(controller, located=True)
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
    tree=controller.plan(output(controller,controller.plan(initial(controller, located=True)),'sid'))
    task=next(n for n,_ in walk(tree) if n.node_id=='T2')
    invalid=change_node(tree,'T2',metadata={**task.metadata,'input_entity_ids':['E1']})
    with pytest.raises(ValueError):controller.validate(invalid)


def test_empty_or_failed_step_cannot_spawn_next(controller):
    for status in (S.DONE,S.BLOCKED):
        tree=controller.plan(initial(controller, located=True))
        tree=change_node(tree,'T1',status=status)
        after=controller.plan(tree)
        assert len([n for n,_ in walk(after) if n.level==3])==1
        assert after.root_nodes[0].metadata['procedure_runs'][0]['status']=='paused'


def test_observation_wrong_event_rejected_atomically(controller):
    tree=controller.plan(initial(controller, located=True));node=next(n for n,_ in walk(tree) if n.node_id=='T1')
    row={'value':'sid','AlertId':'a','AlertTitle':'wrong','EntityIndex':0,'SourceRowHash':'s','matched_input':'url-a'}
    with pytest.raises(ValueError,match='event mismatch'):
        controller.observe(tree,node,SimpleNamespace(query_success=True,execution_id='x'),json.dumps([row]))
    assert len(controller.entities(tree))==1


def test_sql_driver_quoting_handles_bytearray(controller):
    from mysql.connector.conversion import MySQLConverter
    from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
    controller.backend=SimpleNamespace(db=SimpleNamespace(connection=SimpleNamespace(converter=MySQLConverter(),sql_mode='')))
    tree=controller.plan(initial(controller, located=True));task=next(n for n,_ in walk(tree) if n.node_id=='T1')
    sql=controller.sql(tree,task)
    assert 'bytearray' not in sql
    assert "'url-a'" in sql
    ReadOnlySQLValidator.validate(sql)


def test_budget_stop_does_not_leave_active_procedure(controller):
    tree=controller.plan(initial(controller, located=True))
    stopped=controller.stop(tree,'budget_exhausted')
    assert stopped.next_task_id is None
    assert stopped.root_nodes[0].metadata['procedure_runs'][0]['status']=='paused_budget_exhausted'
    assert next(n for n,_ in walk(stopped) if n.level==3).status==S.BLOCKED


def bootstrap_controller(controller):
    from mysql.connector.conversion import MySQLConverter
    controller.retriever.identify=lambda *a:{'known':[], 'wanted':{'concept':'URL'},
        'constraints':[], 'retrieval_query':'blocked C2'}
    class Cursor:
        def execute(self, sql, params=None): self.sql=sql; self.params=params
        def fetchall(self): return [("C2 ' alert",'URL'),("C2 ' alert",'Host_Name')]
    controller.backend=SimpleNamespace(query_count=0,db=SimpleNamespace(cursor=Cursor(),
        connection=SimpleNamespace(converter=MySQLConverter(),sql_mode='')))
    controller.retriever.ask=lambda *a:{'decision':'match','event_title':"C2 ' alert",'output_concepts':['URL']}
    return controller



def prep_observe(controller, tree, rows, success=True):
    node=controller.preparation_node
    execution=SimpleNamespace(query_success=success,execution_id='exec-'+node.node_id,
        to_dict=lambda:{'node_id':node.node_id,'query_success':success})
    return controller.accept_preparation(tree,node,execution,json.dumps(rows))


def prep_row(value='url-a'):
    return {'value':value,'Concept':'URL','AlertId':'a','AlertTitle':"C2 ' alert",
        'SourceRowHash':'s','EntityIndex':0,'matched_input':"C2 ' alert"}


def test_prepare_before_ge_then_pm(controller):
    controller=bootstrap_controller(controller)
    tree=controller.plan(initial(controller))
    assert not tree.root_nodes
    assert controller.preparation_node.node_id=='B1'
    from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
    ReadOnlySQLValidator.validate(controller.sql(tree,controller.preparation_node))
    tree=prep_observe(controller,tree,[prep_row()])
    assert [n.level for n,_ in walk(tree)]==[1,2]
    assert controller.entities(tree)[0]['value']=='url-a'
    tree=controller.plan(tree)
    assert [r['layer'] for r in controller.retrievals]==['BOOTSTRAP','PM']
    assert next(n for n,_ in walk(tree) if n.level==3).node_id=='T1'


@pytest.mark.parametrize('success', [True,False])
def test_empty_or_failed_preparation_never_builds_tree(controller,success):
    controller=bootstrap_controller(controller)
    tree=controller.plan(initial(controller))
    tree=prep_observe(controller,tree,[],success)
    assert not tree.root_nodes
    controller.plan(tree)
    assert controller.preparation_exhausted
    assert controller.preparation_node is None
    assert not controller.stop(tree,'no_match').root_nodes


def known_bootstrap(controller):
    controller=bootstrap_controller(controller)
    controller.retriever.identify=lambda *a:{'known':[{'concept':'URL','value':'url-a'}],
        'wanted':{'concept':'URL'},'constraints':[],'retrieval_query':'q'}
    cursor=controller.backend.db.cursor
    cursor.fetchall=lambda: [("C2 ' alert",)] if cursor.params else [("C2 ' alert",'URL')]
    return controller


def test_known_and_discovered_entities_merged_with_sources(controller):
    controller=known_bootstrap(controller)
    controller.retriever.ask=lambda *a:{'decision':'match','event_title':"C2 ' alert",
        'output_concepts':['URL'],'anchor_entity_ids':['E1']}
    tree=controller.plan(initial(controller))
    assert not tree.root_nodes
    sql=controller.sql(tree,controller.preparation_node)
    assert 'a.SourceRowHash=o.SourceRowHash' in sql and "a.GraphValue='url-a'" in sql
    tree=prep_observe(controller,tree,[prep_row(),prep_row(),prep_row('url-b')])
    assert len(controller.entities(tree))==2
    e=next(n for n,_ in walk(tree) if n.node_id=='E1')
    assert [x['kind'] for x in e.metadata['sources']]==['input','bootstrap_query']


@pytest.mark.parametrize('anchors', [[], ['E99']])
def test_bootstrap_requires_observed_anchor_when_available(controller,anchors):
    controller=known_bootstrap(controller)
    controller.retriever.ask=lambda *a:{'decision':'match','event_title':"C2 ' alert",
        'output_concepts':['URL'],'anchor_entity_ids':anchors}
    with pytest.raises(ValueError,match='anchor_entity_ids'):controller.plan(initial(controller))


def test_preparation_rejects_wrong_event(controller):
    controller=bootstrap_controller(controller);tree=controller.plan(initial(controller))
    row=prep_row();row['AlertTitle']='wrong'
    with pytest.raises(ValueError,match='event mismatch'):prep_observe(controller,tree,[row])
    assert not tree.root_nodes


def test_workflow_bootstrap_bypasses_verifier_and_counts_budget(controller):
    from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
    controller=bootstrap_controller(controller)
    w=ExcytinBenchWorkflow(max_steps=3,llm=lambda *a:pytest.fail('No Reviewer/Verifier during preparation'))
    w.memory_controller=controller
    tree=w.start({'context':'url-a','question':'q'})
    assert not tree.root_nodes
    a=w.propose_next_action();assert a.node_id=='B1'
    w.accept_observation(json.dumps([prep_row()]),query_success=True)
    assert w.state.actions_issued==1 and not w.state.verifications
    assert [r['layer'] for r in controller.retrievals]==['BOOTSTRAP','PM']
    assert w.state.ttt.next_task_id=='T1'
    assert w.propose_next_action().node_id=='T1'
    from scripts.check_ltm_wm_workflow import audit
    assert audit(w.get_logging())['passed']
    from src.schema.itt import itt_payload
    assert itt_payload(w.state.ttt)[0]['children'][0]['bootstrap_evidence'][0]['alert_id']=='a'


def test_budget_exhausted_after_bootstrap_does_not_verify(controller):
    from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
    controller=bootstrap_controller(controller)
    w=ExcytinBenchWorkflow(max_steps=2,llm=lambda *a:pytest.fail('No premature verification'))
    w.memory_controller=controller
    w.start({'context':'url-a','question':'q'});w.propose_next_action()
    w.accept_observation(json.dumps([prep_row()]),query_success=True)
    assert not w.state.verifications
    assert w.propose_next_action().submit
    assert w.state.actions_issued==2
