import copy
import json
import pytest
from src.memory.longterm_memory.path_memory_v1.store import MemoryStore, ROOT
from src.memory.longterm_memory.path_memory_v1.retrieval import MemoryRetriever

FIXTURE=json.loads((ROOT/'training_example.json').read_text())
QUESTION=FIXTURE['question_record']
URL=FIXTURE['inputs']['url']
PM='pm_url_account_device_url'


def intent():
    return {'known':[{'id':'E1','concept':'URL','value':URL}],
      'wanted':{'concept':'URL','description':'同设备另一条恶意点击URL'},
      'constraints':['同设备','另一次事件'],'event_context':['可疑探索活动'],
      'retrieval_query':f'从已知恶意URL {URL} 查同设备另一条点击'}


def decision():
    return {'selected_pm':PM,'decision':'applicable','bindings':{'url':'E1'},
       'constraint_checks':[{'constraint':v,'supported':True,'reason':'执行后验证'} for v in intent()['constraints']],
       'reason':'输入与目标适配'}


class FakeChat:
    def __init__(self,*replies):
        self.replies=list(replies)
        self.payloads=[]
    def __call__(self,**kwargs):
        self.payloads.append(json.loads(kwargs['user_prompt']))
        return json.dumps(self.replies.pop(0),ensure_ascii=False)


def run(fake,store=None):
    return MemoryRetriever(store or MemoryStore(),fake).match(QUESTION['context'],QUESTION['question'])


def test_training_question_match_without_gold_or_prepared_inputs():
    fake=FakeChat(intent(),decision())
    result=run(fake)
    assert result['status']=='matched' and result['selected_pm']==PM
    assert result['execution_inputs']==FIXTURE['inputs']
    assert result['similarity_scores']=={} and result['recall_mode']=='direct_llm'
    assert URL not in result['intent']['retrieval_query']
    serialized=json.dumps(fake.payloads,ensure_ascii=False)
    for secret in [QUESTION['answer'], 'shortest_alert_path', 'S-1-5-21-561436748', 'mb-winclient', 'SELECT DISTINCT']:
        assert secret not in serialized


def test_semantically_wrong_pm_rejected_despite_matching_types():
    fake=FakeChat(intent(),{'selected_pm':None,'decision':'no_match','bindings':{},'constraint_checks':[], 'reason':'调查关系不一致'})
    assert run(fake)['status']=='no_match'


def test_type_filter_skips_second_llm_call():
    value=intent(); value['wanted']['concept']='Account_SID'
    fake=FakeChat(value)
    assert run(fake)['status']=='no_match' and len(fake.payloads)==1


@pytest.mark.parametrize('damage',['id','entity','binding_type','constraints','unsupported'])
def test_untrusted_matching_rejected(damage):
    value=intent(); answer=decision()
    if damage=='id': answer['selected_pm']='invented_pm'
    if damage=='entity': answer['bindings']['url']='E999'
    if damage=='binding_type':
        value['known'].append({'id':'E2','concept':'Device_Name','value':URL})
        answer['bindings']['url']='E2'
    if damage=='constraints': answer['constraint_checks'].pop()
    if damage=='unsupported': answer['constraint_checks'][0]['supported']=False
    with pytest.raises(ValueError): run(FakeChat(value,answer))


def test_hallucinated_entity_rejected():
    value=intent(); value['known'][0]['value']='not-in-source'
    with pytest.raises(ValueError): run(FakeChat(value))


def test_missing_event_mapping_blocks_execution():
    store=MemoryStore(); del store.sm['event_types']['suspicious_exploration']
    result=run(FakeChat(intent(),decision()),store)
    assert result['status']=='unresolved_event_mapping' and result['execution_inputs'] is None


def test_embedding_top_k_and_no_forced_selection():
    store=MemoryStore()
    other=copy.deepcopy(store.pm['procedures'][0]); other['id']='other'
    store.pm['procedures'].append(other)
    def embed(texts):
        assert len(texts)==3 and URL not in texts[0]
        return [[1.,0.],[1.,0.],[0.,1.]]
    fake=FakeChat(intent(),{'selected_pm':None,'decision':'no_match','bindings':{},'constraint_checks':[],'reason':'关系不符合'})
    result=MemoryRetriever(store,fake,embed=embed,top_k=1).match(QUESTION['context'],QUESTION['question'])
    assert result['candidate_ids']==[PM] and result['similarity_scores'][PM]==1.
    assert result['status']=='no_match'


def test_larger_catalog_requires_embeddings():
    store=MemoryStore(); store.pm['procedures']*=6
    with pytest.raises(ValueError,match='embeddings'): run(FakeChat(intent()),store)


def test_invalid_json_fails_closed():
    with pytest.raises(json.JSONDecodeError):
        MemoryRetriever(MemoryStore(),lambda **_: 'not json').match('c','q')
