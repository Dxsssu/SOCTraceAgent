"""Migrate existing typed fragments to the lossless raw-log SQL projection."""
import copy
import json
from .core import ROOT,new_episode,digest,write_json


def migrate():
    c=json.loads((ROOT/'catalog.json').read_text())
    if c.get('physical_revision')==2:return
    archive=ROOT/'archive';archive.mkdir(exist_ok=True)
    for name in ('catalog.json','summary.json','validation_report.json'):
        target=archive/('v1_'+name)
        if not target.exists():target.write_bytes((ROOT/name).read_bytes())
    mapping={};episodes={}
    for eid,e in c['episodic'].items():
        new=new_episode(e['signature']['known'],e['signature']['wanted'])
        new['source_refs']=e['source_refs'];mapping[eid]=new['id'];episodes[new['id']]=new
    plans={};plan_ids={}
    for pid,p in c['procedural'].items():
        p=copy.deepcopy(p)
        for v in p['variants']:
            for step in v['steps']:step['episode_id']=mapping[step['episode_id']]
        p['signature']['variants']=p['variants']
        p['id']='pm_'+digest(p['signature'])[:16]
        plan_ids[pid]=p['id'];plans[p['id']]=p
    c['episodic']=episodes;c['procedural']=plans;c['physical_revision']=2
    c['semantic']['tables']['LTMEntityEvidence']={
      'description':'原始SecurityAlert的会话临时实体投影；按SecurityIncident.AlertIds限制incident。不是从图或答案生成的表。',
      'fields':{'SourceRowHash':'CHAR(64)','AlertId':'VARCHAR(255)','AlertTitle':'TEXT','ObservedAt':'TEXT',
                'Concept':'VARCHAR(64)','GraphValue':'LONGTEXT BINARY COLLATION','RawComponents':'JSON','EntityIndex':'INT','Roles':'JSON'},
      'time_field':'ObservedAt','join_key':'SourceRowHash','scope':'temporary table per connection; source tables untouched'}
    c['semantic']['tables']['SecurityIncident']={'description':'原始事件告警成员关系，合并事件历次记录中的AlertIds，保留独立的告警快照。',
      'fields':{'IncidentNumber':'TEXT','AlertIds':'TEXT'},'source':'original global CSV'}
    for key,concept in c['semantic']['concepts'].items():
        concept['source_mapping']=concept['mapping']
        concept['mapping']={'table':'LTMEntityEvidence','field':'AlertTitle' if key=='SecurityAlert_Title' else 'GraphValue',
                            'type_filter':None if key=='SecurityAlert_Title' else key,
                            'raw_field':None if key=='SecurityAlert_Title' else 'RawComponents'}
    for event in c['semantic']['events'].values():
        event['source_mapping']=event['mapping'];event['mapping']={'table':'LTMEntityEvidence','field':'AlertTitle','value':event['title']}
    # Archive original failure results while retaining immutable official inputs.
    for file in sorted((ROOT/'cases').glob('*.json')):
        case=json.loads(file.read_text())
        if 'live_validation_v1' not in case:case['live_validation_v1']=case['live_validation']
        case['merged_ems']=[mapping[e] for e in case['merged_ems']]
        case['merged_pm']=plan_ids[case['merged_pm']]
        case['live_validation']=[]
        write_json(file,case)
    write_json(ROOT/'catalog.json',c)
    write_json(ROOT/'physical_migration.json',{'revision':2,'episode_ids':mapping,'plan_ids':plan_ids,
       'source_policy':'original global logs, not graph payloads or answers','original_tables_modified':False})

if __name__=='__main__':migrate()
