import json
from copy import deepcopy
import pytest
from src.memory.longterm_memory.procedural_memory.excytin_bench import DEFAULT_KNOWLEDGE_PATH
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import build_bundle
from src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j import load_knowledge, validate_knowledge, graph_rows
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import retrieve, MAX_INPUT_BYTES, SELECTION_PROMPT


def test_rebuild_is_deterministic_and_every_active_template_has_cross_case_support():
    k, audit = build_bundle()
    saved = load_knowledge(DEFAULT_KNOWLEDGE_PATH)
    k.pop('generated_at');saved.pop('generated_at')
    assert k == saved
    assert k['format_version'] == '5.0' and k['procedures']
    for p in k['procedures']:
        assert audit['templates'][p['id']]['episodes'] >= 2
        if p['status'] == 'active':assert audit['templates'][p['id']]['incidents'] >= 2
        assert ':' in p['sql_template']
    assert all('template_json' in r for r in graph_rows(k))


@pytest.mark.parametrize('mutation',['unknown_field','unknown_table','constant','duplicate','wrong_hash'])
def test_catalog_rejects_corrupt_template(mutation):
    k=json.loads(DEFAULT_KNOWLEDGE_PATH.read_text());p=k['procedures'][0]
    if mutation=='unknown_field':p['sql_template']=p['sql_template'].replace('SELECT ', 'SELECT Invented, ')
    if mutation=='unknown_table':p['sql_template']=p['sql_template'].replace('FROM ', 'FROM Invented, ')
    if mutation=='constant':p['sql_template']=p['sql_template'].replace(':p1', "'case-secret'")
    if mutation=='duplicate':k['procedures'][1]['id']=p['id']
    if mutation=='wrong_hash':k['semantic_catalog']['schema_hash']='wrong'
    with pytest.raises(Exception):validate_knowledge(k)


def test_retrieval_is_one_bounded_call_and_returns_canonical_copy():
    k=load_knowledge(DEFAULT_KNOWLEDGE_PATH);calls=[]
    def choose(system,user):
        calls.append(user);assert system==SELECTION_PROMPT
        assert len((system+user).encode())<=MAX_INPUT_BYTES
        payload=json.loads(user)
        assert 'sql_template' not in user and 'execution_result' not in user
        return json.dumps([payload['catalog'][0]['id']])
    found=retrieve(k,{'question':'Find process command line','context':'中'*10000,'itt':'secret'},selector=choose)
    assert len(calls)==len(found)==1
    found[0]['inputs'].clear()
    assert all(p['inputs'] for p in k['procedures'])


@pytest.mark.parametrize('response',['[]','not json','["invented"]','[1]','{}'])
def test_retrieval_miss_or_invalid_response_has_no_retry(response):
    calls=[]
    result=retrieve(load_knowledge(DEFAULT_KNOWLEDGE_PATH),{'question':'Find process'},selector=lambda *args:(calls.append(args) or response))
    assert result==[] and len(calls)==1
