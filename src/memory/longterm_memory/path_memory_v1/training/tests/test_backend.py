import csv
import json
from types import SimpleNamespace
import pytest
from src.memory.longterm_memory.path_memory_v1.training.backend import RawLogSource,ReplayBackend
from src.memory.longterm_memory.path_memory_v1.training.core import new_episode
from src.memory.longterm_memory.path_memory_v1.training.export import strategy_signature
from src.memory.longterm_memory.path_memory_v1.training.runtime import MemoryBundle


def fixture(root,invalid_member=False):
    (root/'SecurityAlert').mkdir()
    with (root/'SecurityIncident.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f,delimiter='❖');w.writerow(['IncidentNumber','AlertIds']);w.writerow(['1',json.dumps(['a'])])
    first=[{'Type':'account','Sid':'S1'},{'Type':'process','ProcessId':'1','CreatedTimeUtc':'t',
              'CommandLine':'"powershell.exe" -File C:\\Temp\\helper.ps1'}]
    second=first+[{'Type':'url','Url':'https://x/Case'}]
    with (root/'SecurityAlert'/'a.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f,delimiter='❖');w.writerow(['SystemAlertId','AlertName','TimeGenerated','Entities'])
        w.writerow(['a','event','t1',json.dumps(first)]);w.writerow(['a','event','t2',json.dumps(second)])
        w.writerow(['a' if invalid_member else 'unrelated','event','t3','invalid'])
    return first,second


def test_csv_quotes_and_backslashes_preserved(tmp_path):
    first,_=fixture(tmp_path)
    source=RawLogSource(tmp_path);rows,manifest=source.evidence('incident_1')
    processes=[json.loads(r[7]) for r in rows if r[5]=='Process_Identity']
    assert processes and all(p['CommandLine']==first[1]['CommandLine'] for p in processes)
    assert manifest['snapshots']==2 and manifest['missing_source_alert_ids']==[]
    assert 'unrelated' in source.invalid_rows


def test_invalid_in_scope_source_not_silently_repaired(tmp_path):
    fixture(tmp_path,invalid_member=True)
    with pytest.raises(ValueError,match='Invalid original source'):
        RawLogSource(tmp_path).evidence('incident_1')


def test_history_retained_instead_of_only_latest(tmp_path):
    fixture(tmp_path);source=RawLogSource(tmp_path)
    rows,_=source.evidence('incident_1')
    assert len({r[0] for r in rows})==2
    assert sum(r[5]=='URL' for r in rows)==1


class FakeCursor:
    fields=['AlertId','AlertTitle','ObservedAt','EntityIndex','value','RawComponents','Roles','SourceRowHash','matched_input']
    description=[(f,) for f in fields]
    def __init__(self):self.calls=[]
    def execute(self,sql,params):self.calls.append((sql,params))
    def fetchall(self):
        count=501 if len(self.calls)==1 else 1
        return [('a','event','t',i,'https://x','{"Url":"https://x"}','[]','hash','S1') for i in range(count)]


def test_sql_parameters_and_pagination():
    backend=ReplayBackend.__new__(ReplayBackend);cursor=FakeCursor()
    backend.db=SimpleNamespace(cursor=cursor);backend.query_count=0
    poison="S1' OR 1=1 --"
    result=backend.execute(new_episode('Account_SID','URL'),{poison},'event')
    assert result['status']=='ok' and len(result['outputs'])==501
    assert result['query_count']==2 and cursor.calls[1][0].endswith('OFFSET 500')
    assert all(poison not in sql and params==('event',poison) for sql,params in cursor.calls)
    assert 'Entities' not in cursor.calls[0][0]


def test_invalid_concepts_cannot_be_interpolated_into_sql():
    with pytest.raises(ValueError):new_episode("URL' OR 1=1",'URL')


def test_strategy_merging_keeps_order_and_typed_routes():
    assert strategy_signature(['Account_SID','Account_Name','URL'])==strategy_signature(['Account_AAD_ID','URL'])
    assert strategy_signature(['Account_SID','Host_Name','URL'])!=strategy_signature(['Host_Name','Account_SID','URL'])
    bundle=MemoryBundle()
    assert len(bundle.pm)==85 and sum(len(p['variants']) for p in bundle.pm.values())==232
    assert any(p['step_range'][0]!=p['step_range'][1] for p in bundle.pm.values())


def test_disallowed_scenario_input_never_queries():
    b=MemoryBundle();p=next(iter(b.pm.values()));s=p['scenarios'][0]
    backend=SimpleNamespace(execute=lambda *_:pytest.fail('Should not query'))
    with pytest.raises(ValueError):b.execute(backend,p['id'],s['id'],s['allowed_variants'][0],'Bogus',{'x'})
