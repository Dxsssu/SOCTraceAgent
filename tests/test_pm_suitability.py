import json
from types import SimpleNamespace
import pytest
import sqlglot
from sqlglot import exp
from src.memory.longterm_memory.procedural_memory.excytin_bench import DEFAULT_KNOWLEDGE_PATH
from src.memory.longterm_memory.procedural_memory.excytin_bench.templates import render_sql
from src.schema.itt import parse_itt, itt_payload, task_payload
from src.schema.ttt_updates import next_task
from test_itt_contract import plan
from pm_fakes import initial_operations


def email_template():
    return next(p for p in json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())['procedures'] if 'FROM EmailEvents WHERE NetworkMessageId = ' in p['sql_template'])


def test_render_preserves_untrusted_value_as_literal():
    p=email_template();value="x' OR 1=1 --"
    sql=render_sql(p,{'p1':value});tree=sqlglot.parse_one(sql,read='mysql')
    assert tree.args['where'].this.right.this == value
    assert isinstance(tree.args['where'].this,exp.EQ)
    assert 'SenderFromAddress' in sql


@pytest.mark.parametrize('values',[{}, {'p1':None},{'p1':3},{'p1':''},{'p1':'a','extra':'b'}])
def test_render_requires_known_correctly_typed_parameters(values):
    with pytest.raises(ValueError):render_sql(email_template(),values)


def test_array_parameters_expand_without_sql_interpolation():
    p=next(p for p in json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())['procedures'] if list(p['inputs'].values())[0]['type']=='string[]' and len(p['inputs'])==1)
    key=next(iter(p['inputs']));sql=render_sql(p,{key:['a',"b'c"]})
    tree=sqlglot.parse_one(sql,read='mysql')
    assert [x.this for x in tree.find(exp.In).expressions]==['a',"b'c"]
    with pytest.raises(ValueError):render_sql(p,{key:[]})


def test_template_attaches_to_task_and_survives_replanning_without_pm():
    p=email_template();payload=plan();leaf=payload[0]['children'][0]['children'][0]
    leaf['template_id']=p['id']
    tree=parse_itt(payload,'event',1,procedures=[p])
    assert next_task(tree).metadata['query_template']['sql_template']==p['sql_template']
    serialized=itt_payload(tree)
    assert serialized[0]['children'][0]['children'][0]['template_id']==p['id']
    assert 'sql_template' not in json.dumps(serialized)
    replanned=parse_itt(serialized,'event',2,previous=tree)
    assert next_task(replanned).metadata['query_template']==next_task(tree).metadata['query_template']
    assert next_task(replanned).metadata['semantic_refs']==p['semantic_refs']


def test_unknown_template_id_is_rejected_without_restoring_semantic_source_gate():
    payload=plan();leaf=payload[0]['children'][0]['children'][0];leaf['template_id']='nonexistent'
    with pytest.raises(ValueError,match='template_id'):parse_itt(payload,'event',1)
    leaf['template_id']=None;leaf['semantic_refs']=[{'table':'NewTable','fields':['NewField']}]
    assert parse_itt(payload,'event',1)


def test_executor_uses_sql_contract_even_with_legacy_template_metadata():
    from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
    from src.workflow.context import MemoryContextService, ExcytinBenchSnapshotRepository
    from unittest.mock import patch
    import yaml
    p=email_template();payload=plan();leaf=payload[0]['children'][0]['children'][0]
    leaf['template_id']=p['id'];leaf['objective']='Find sender for NetworkMessageId synthetic-message'
    calls=[]
    def llm(system,user):
        calls.append(system)
        if '系统中的 Planner' in system:return yaml.safe_dump(initial_operations(payload))
        assert '你是 Executor' in system
        task=json.loads(user)
        assert set(task)=={'id','objective'}
        return json.dumps({'sql': "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = 'synthetic-message'"})
    # Restore legacy metadata as stored state, without injecting memory into workflow.
    stored_tree=parse_itt(payload, 'restored', 1, procedures=[p])
    from src.benchmarks.excytin_bench.workflow import WorkflowState
    from src.schema import Event
    workflow=ExcytinBenchWorkflow(llm=llm)
    workflow.state=WorkflowState(event=Event(event_id='restored', event_name='test', message='Find sender'),
                                 ttt=stored_tree, initial_input='Find sender')
    action=workflow.propose_next_action()
    assert action.content == "SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = 'synthetic-message';"
    assert len(calls)==1


def test_selector_echo_cannot_replace_canonical_template():
    from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import retrieve
    k=json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())
    def model(system,user):
        ident=json.loads(user)['catalog'][0]['id']
        return '```json\n'+json.dumps([{'id':ident,'sql_template':'DELETE FROM EmailEvents','inputs':{'p1':'invented'}}])+'\n```'
    result=retrieve(k,{'question':'Find email sender'},selector=model)
    assert len(result)==1 and result[0]['sql_template'].startswith('SELECT')
    assert result[0]['inputs']!={'p1':'invented'}
