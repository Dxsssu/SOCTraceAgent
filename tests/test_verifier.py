import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from src.agent.verifier import validate_verification
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.schema.itt import itt_payload
from test_excytin_benchmark_workflow import INITIAL_TTT


def record(success=True):
    return SimpleNamespace(execution_id='E1', query_success=success,
                           result_status='rows_returned', observation='PRT access: browsercore.exe')


def confirmation():
    return {'confirmed':True,'goals':[{'id':'G1','relation':'PRT access is linked to this executable',
        'evidence':[{'execution_id':'E1','quote':'PRT access: browsercore.exe'}]}], 'gap':'','suggestion':''}


@pytest.mark.parametrize('mutation', ['unknown_id','fake_quote','failed_sql','missing_goal','no_relation','duplicate_goal'])
def test_confirmation_requires_real_evidence_for_every_goal(mutation):
    value=confirmation(); e=record()
    if mutation=='unknown_id': value['goals'][0]['evidence'][0]['execution_id']='E99'
    if mutation=='fake_quote': value['goals'][0]['evidence'][0]['quote']='invented'
    if mutation=='failed_sql': e=record(False)
    if mutation=='missing_goal': value['goals']=[]
    if mutation=='no_relation': value['goals'][0]['relation']=''
    if mutation=='duplicate_goal': value['goals']*=2
    with pytest.raises(ValueError): validate_verification(value, {'G1'}, [e])


@pytest.mark.parametrize('planner_closes', [False, True])
def test_premature_completion_reopens_and_queries_relation(planner_closes):
    workflow=None
    def model(system,user):
        if 'SM 表选择助手' in system: return '[]'
        if 'Procedural Memory' in system and 'Planner' not in system: return '[]'
        if '系统中的 Planner' in system:
            if workflow.state is None: return INITIAL_TTT
            payload=json.loads(user)['itt']
            if not workflow.state.verifications or len(workflow.state.executions) > 1:
                payload[0]['status']='done';payload[0]['children'][0]['status']='done'
            else:
                assert 'Verifier' in json.loads(user)['review']['summary']
                payload[0]['children'][0]['children'].append(dict(id='T2',objective='Verify PRT alert process',
                    status='todo',semantic_refs=[],execution_code='',execution_result=''))
            return yaml.safe_dump(payload)
        if 'SQL 查询生成器' in system: return 'sql: SELECT FileName FROM AlertEvidence'
        if '你是 Reviewer' in system:
            return json.dumps({'summary':'Execution returned a process',
                'suggestion':'继续核查' if planner_closes else '提交答案'})
        if '最终答案整理器' in system:
            return 'answer: '+('powershell.exe' if len(workflow.state.executions)==1 else 'browsercore.exe')
        if '你是 Verifier' in system:
            p=json.loads(user)
            assert 'workflow_memory_context' not in p
            if len(workflow.state.executions)==1:
                return json.dumps(dict(confirmed=False,goals=[],gap='Only script host observed, not target relation.',suggestion='Find process linked to the PRT alert.'))
            return json.dumps(dict(confirmed=True,goals=[dict(id='G1',relation='Alert links PRT access to browsercore.exe',
                evidence=[dict(execution_id='E2',quote='PRT access: browsercore.exe')])],gap='',suggestion=''))
        raise AssertionError(system)
    workflow=ExcytinBenchWorkflow(llm=model)
    workflow.act('Which process accessed PRT?')
    action=workflow.act('powershell.exe runs script')
    assert not action.submit and action.node_id=='T2'
    assert not workflow.state.verifications[0]['confirmed']
    final=workflow.act('PRT access: browsercore.exe')
    assert final.submit and final.content=='browsercore.exe'
    assert workflow.state.verifications[-1]['confirmed']


def test_budget_exit_does_not_submit_unverified_candidate():
    workflow=None
    def model(system,user):
        if '系统中的 Planner' in system: return INITIAL_TTT
        if 'SQL 查询生成器' in system:return 'sql: SELECT 1'
        if '你是 Reviewer' in system:return 'summary: Candidate found\nsuggestion: 提交答案'
        if '最终答案整理器' in system:return 'answer: wrong-candidate'
        if '你是 Verifier' in system:return 'confirmed: false\ngoals: []\ngap: Missing relationship\nsuggestion: Check linked evidence'
        return '[]'
    workflow=ExcytinBenchWorkflow(llm=model,max_steps=2)
    workflow.act('Find target')
    final=workflow.act('candidate')
    assert final.submit and 'wrong-candidate' not in final.content
    assert not workflow.state.ready_to_submit


def test_target_concept_cannot_be_only_script_name_substring():
    e=record();e.observation='powershell.exe runs get-mfatoken.ps1'
    value=confirmation();value['goals'][0]['evidence'][0]['quote']=e.observation
    with pytest.raises(ValueError,match='target concept MFA'):
        validate_verification(value,{'G1'},[e],question='Which process is linked to Multi Factor Authentication (MFA)?')
    e.observation='Multi Factor Authentication alert: process.exe'
    value=confirmation();value['goals'][0]['evidence'][0]['quote']=e.observation
    assert validate_verification(value,{'G1'},[e],question='Which process is linked to Multi Factor Authentication (MFA)?')['confirmed']


def test_excerpt_ids_are_bound_to_actual_execution_and_result():
    from src.agent.verifier import evidence_excerpts
    e=record();e.sql='SELECT Title, FileName FROM Events'
    records,ledger=evidence_excerpts([e],'Which process accessed PRT?','browsercore.exe')
    value=confirmation();value['goals'][0]['evidence']=[{'execution_id':'E1','excerpt_id':records[0]['excerpts'][0]['excerpt_id']}]
    assert validate_verification(value,{'G1'},[e],ledger)['goals'][0]['evidence'][0]['quote']==e.observation
    value=confirmation();value['goals'][0]['evidence']=[{'execution_id':'E2','excerpt_id':'E1:0'}]
    with pytest.raises(ValueError): validate_verification(value,{'G1'},[e],ledger)
