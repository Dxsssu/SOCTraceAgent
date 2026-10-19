import sqlglot
import pytest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.query_optimization import optimize_query
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.semantic_memory.v2 import SemanticMemoryV2
from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
from src.memory.longterm_memory.procedural_memory.flows import load_runtime_catalog


def test_projection_keeps_dependencies_and_interpretation_but_drops_payload():
    sql, outputs, audit = optimize_query(
        'SELECT AlertId, Title, DeviceName, FileName, TimeGenerated, Timestamp, AdditionalFields, SHA1 '
        "FROM AlertEvidence WHERE DeviceName='host' LIMIT 100",
        needed_fields={'FileName'}, dependency_outputs={'AlertId'})
    assert outputs == ['AlertId','Title','DeviceName','FileName','TimeGenerated']
    assert "DeviceName LIKE 'host.%' ESCAPE '!'" in sql
    assert audit['original_outputs'][-1] == 'SHA1'
    assert 'LIMIT 100' in sql


@pytest.mark.parametrize('predicate,changed',[
    ("DeviceName='host'",True), ("DeviceName='host_one'",True),
    ("DeviceName LIKE 'host'",True), ("DeviceName LIKE 'host_%'",False),
    ("DeviceName='host.corp.test'",False), ("NOT DeviceName='host'",False),
    ("DeviceName <> 'host'",False), ("AccountName='host'",False),
])
def test_host_predicate_scope(predicate,changed):
    sql,_,audit=optimize_query('SELECT DeviceName FROM events WHERE '+predicate,needed_fields={'DeviceName'})
    assert bool(audit['host_boundary_fields']) == changed
    if "'host_one'" in predicate:
        assert "'host!_one.%'" in sql


@pytest.mark.parametrize('sql',[
    'SELECT DISTINCT FileName, SHA1 FROM events',
    'SELECT FileName, COUNT(*) AS n FROM events GROUP BY FileName',
    'SELECT a.FileName, b.SHA1 FROM events a JOIN other b ON a.id=b.id',
])
def test_projection_does_not_change_distinct_aggregate_or_join(sql):
    optimized,_,_=optimize_query(sql,needed_fields={'FileName'})
    assert sqlglot.parse_one(optimized) == sqlglot.parse_one(sql)


def test_json_dependency_retained_and_unknown_target_preserves_outputs():
    sql='SELECT AlertId, AdditionalFields, FileName FROM AlertEvidence'
    _,outputs,_=optimize_query(sql,needed_fields={'Missing'},dependency_outputs={'AdditionalFields'})
    assert 'AdditionalFields' in outputs
    _,outputs,_=optimize_query(sql,needed_fields={'Missing'})
    assert outputs == ['AlertId','AdditionalFields','FileName']


def test_runtime_binding_output_contract_survives_projection_and_flow_dependency():
    store=load_runtime_catalog();schema={}
    for t in store.templates.values():
        for table,fields in t['schema_fields'].items():schema.setdefault(table,{}).update({f:'text' for f in fields})
    p=ProceduralPlanningSession(SemanticPlanningSession(SemanticMemoryV2.load(schema=schema)),store)
    q={'known':['Device_Name'],'wanted':['File_Name','Process_Command'],'intent':'主机告警关联进程文件'}
    p.retrieve(q)
    sid='pm_eval_mail_host_alert_entity_join'
    # Isolate binding from top-k ranking; same retrieved-query contract.
    p.offered[sid]=store.snippets[sid];p.offered_queries[sid]=q
    c=p.bind({'id':sid,'step':'s1','inputs':{'p1':'vnevado-win11t'}})
    assert 'AdditionalFields' not in c['outputs']
    assert 'AlertId' in c['outputs'] and len(c['outputs']) < 20
    assert "vnevado-win11t.%" in c['bound_sql']
    p.register('T1',c)
    row=tuple('alert-1' if f=='AlertId' else 'value' for f in c['outputs'])
    p.observe('T1',repr([row]),True)
    second=p.bind({'id':sid,'step':'s2','run':'T1','inputs':{}})
    assert second['bindings']=={'p1':['alert-1']}
    assert 'FileName' in second['outputs'] and 'AdditionalFields' not in second['outputs']
    # The original audited catalog and template output contract remain intact.
    original=store.templates[store.snippets[sid]['steps'][0]['template_id']]
    assert 'AdditionalFields' in original['outputs']
