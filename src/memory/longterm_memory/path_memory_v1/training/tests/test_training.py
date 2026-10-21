import json
import pytest
from src.memory.longterm_memory.path_memory_v1.training.core import extract, new_episode, step_query


def test_distinct_namespaces_and_raw_values():
    rows=extract([{'Type':'account','Name':'Alice','Sid':'S-1','AadUserId':'AAD','UPNSuffix':'example.org'}])
    assert {r['concept'] for r in rows}=={'Account_Name','Account_SID','Account_AAD_ID','Account_Derived_Email'}
    assert next(r for r in rows if r['concept']=='Account_AAD_ID')['raw_components']=={'AadUserId':'AAD'}


def test_process_file_mention_is_not_image_identity():
    rows=extract([{'Type':'process','ProcessId':'1','CreatedTimeUtc':'2024-01-01T00:00:00Z','CommandLine':'powershell.exe -File helper.ps1'}])
    assert {r['graph_value'] for r in rows if r['concept']=='Command_File_Mention'}=={'powershell.exe','helper.ps1'}
    assert len([r for r in rows if r['concept']=='Process_Identity'])==1


def test_no_cross_alert_join():
    rows=[{'SystemAlertId':'a','Entities':json.dumps([{'Type':'account','Sid':'S1'}])},
          {'SystemAlertId':'b','Entities':json.dumps([{'Type':'url','Url':'https://x'}])}]
    assert step_query(rows,'Account_SID','URL',{'S1'})['status']=='empty'


def test_invalid_json_is_blocked_not_empty():
    assert step_query([{'SystemAlertId':'a','Entities':'[{broken]'}],'Account_SID','URL',{'S1'})['status']=='invalid_json'


def test_overflow_not_success():
    assert step_query([{}]*501,'Account_SID','URL',{'S1'})['status']=='truncated'


def test_candidate_set_and_provenance():
    rows=[{'SystemAlertId':'a','Entities':json.dumps([{'Type':'account','Sid':'S1'},{'Type':'url','Url':'https://a'},{'Type':'url','Url':'https://b'}])}]
    out=step_query(rows,'Account_SID','URL',{'S1'})
    assert len(out['outputs'])==2
    assert all(x['matched_inputs']==['S1'] and x['alert_id']=='a' for x in out['outputs'])


def test_episode_dedup_is_typed_directional():
    assert new_episode('Account_SID','URL')['id']==new_episode('Account_SID','URL')['id']
    assert new_episode('Account_SID','URL')['id']!=new_episode('URL','Account_SID')['id']
    assert new_episode('Account_SID','URL')['id']!=new_episode('Account_Name','URL')['id']


def test_url_raw_preserved_despite_upstream_graph_normalization():
    result=extract([{'Type':'url','Url':'https://x/CaseSensitive'}])[0]
    assert result['raw_components']['Url']=='https://x/CaseSensitive'
    assert result['graph_value']=='https://x/casesensitive'


def test_derived_subscription_not_resource_id():
    resource='/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/Group1/providers/a/b'
    records=extract([{'Type':'azure-resource','ResourceId':resource}])
    assert {r['concept'] for r in records}=={'Azure_Subscription_ID','Azure_Resource_Group','Azure_Resource_ID'}


def test_exported_catalog_contracts_and_coverage():
    from src.memory.longterm_memory.path_memory_v1.training.core import ROOT
    sm=json.loads((ROOT/'semantic.json').read_text())
    episodes={e['id']:e for e in json.loads((ROOT/'episodic.json').read_text())['episodes']}
    plans=json.loads((ROOT/'procedural.json').read_text())['procedures']
    index=json.loads((ROOT/'case_memory_index.json').read_text())['cases']
    manifest=json.loads((ROOT/'manifest.json').read_text())['items']
    assert set(index)=={i['id'] for i in manifest}
    for p in plans:
        routes={v['id']:v for v in p['variants']}
        for scope in p['scenarios']:
            assert scope['id'] and scope['source_refs']
            assert all(e in sm['events'] for e in scope['events'])
            assert set(scope['allowed_variants'])<=set(routes)
            for vid in scope['allowed_variants']:
                v=routes[vid]
                assert len(v['steps'])==len(scope['events'])
                for entry in v['known']['any_of']:
                    seed=entry['seed_entities']['concept'];expected=seed
                    for n,s in enumerate(v['steps']):
                        eid=s['episode_by_input'][seed] if n==0 else s['episode_id']
                        e=episodes[eid]
                        assert e['signature']['known']==expected
                        assert s['bindings']['event_title']=={'scenario_event_index':n}
                        expected=e['signature']['wanted']
                    assert expected==v['wanted']['concept']



def test_memory_cards_do_not_embed_case_records():
    from src.memory.longterm_memory.path_memory_v1.training.core import ROOT
    def visit(value):
        if isinstance(value,dict):
            assert not {'answer','question_record','shortest_alert_path','context','expected_present'} & value.keys()
            for v in value.values():visit(v)
        elif isinstance(value,list):
            for v in value:visit(v)
    for name in ('semantic.json','episodic.json','procedural.json'):
        visit(json.loads((ROOT/name).read_text()))
