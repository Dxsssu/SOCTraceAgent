"""Read, derive, verify, merge and persist exactly ONE training record per call."""
import argparse
import collections
import fcntl
import hashlib
import json
from pathlib import Path
import networkx as nx
from .core import ROOT, TRAIN, GRAPHS, concept, digest, write_json, sm_definition, new_episode, step_query, configure_projection


def inventory():
    items=[]
    for file in sorted(TRAIN.glob('*.json'),key=lambda p:int(p.name.split('_')[1])):
        records=json.loads(file.read_text())
        for index in range(len(records)):
            items.append({'id':file.name.split('_qa_')[0]+f'_train_{index:03d}',
              'source_file':str(file),'index':index,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    return items


def initial_catalog():
    return {'version':'training-path-v1','scope':'official train path-derived, no workflow integration',
      'semantic':{'concepts':{},'events':{},'tables':{'SecurityAlert':{
        'description':'告警实体关系；SystemAlertId标识本表告警，不能直接当AlertEvidence.AlertId。',
        'fields':{'SystemAlertId':'TEXT','AlertName':'TEXT','TimeGenerated':'TEXT','Entities':'TEXT'},
        'time_field':'TimeGenerated','entity_decoder':'strict JSON only; no silent repair'}}},
      'episodic':{},'procedural':{}}


def append_source(record,cid):
    if cid not in record['source_refs']: record['source_refs'].append(cid)


def build_next(live_query=None):
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.build.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        items=inventory()
        manifest_path=ROOT/'manifest.json'
        if manifest_path.exists():
            assert json.loads(manifest_path.read_text())['items']==items,'Training sources changed'
        else: write_json(manifest_path,{'split':'train','count':len(items),'order':'numeric incident then zero-based index','items':items})
        item=next((i for i in items if not (ROOT/'cases'/f"{i['id']}.json").exists()),None)
        if item is None:return {'status':'complete','processed':len(items)}
        path=ROOT/'catalog.json'
        catalog=json.loads(path.read_text()) if path.exists() else initial_catalog()
        before={k:len(catalog[k]) for k in ('episodic','procedural')}
        before['semantic']=len(catalog['semantic']['concepts'])
        # Exactly one complete question and its entire official path are interpreted here.
        q=json.loads(Path(item['source_file']).read_text())[item['index']]
        incident=item['id'].split('_train_')[0]
        graph_file=GRAPHS/f'{incident}.graphml'
        g=nx.read_graphml(graph_file)
        official=[str(n) for n in q['shortest_alert_path']]
        assert official[0]==str(q['start_alert']) and official[-1]==str(q['end_alert'])
        assert len(official)%2==1
        assert all(g.has_edge(a,b) for a,b in zip(official,official[1:]))
        assert nx.shortest_path_length(g,official[0],official[-1])==len(official)-1
        assert all(g.nodes[n]['type']==('alert' if i%2==0 else 'entity') for i,n in enumerate(official))
        starts=[str(n) for n in q['start_entities']]; ends=[str(n) for n in q['end_entities']]
        assert starts and len(ends)==1
        assert all(g.has_edge(n,official[0]) for n in starts)
        assert g.has_edge(official[-1],ends[0])
        cid=item['id']; nodes={n:g.nodes[n] for n in dict.fromkeys(starts+official+ends)}
        for n,d in nodes.items():
            if d['type']=='entity':
                record=sm_definition(d); key=record['id']
                if key not in catalog['semantic']['concepts']:
                    catalog['semantic']['concepts'][key]={**record,'source_refs':[]}
                append_source(catalog['semantic']['concepts'][key],cid)
        catalog['semantic']['concepts'].setdefault('SecurityAlert_Title',{'id':'SecurityAlert_Title','name':'SecurityAlert事件标题',
          'kind':'SecurityAlert_Title','mapping':{'table':'SecurityAlert','field':'AlertName','storage_type':'TEXT'},
          'cautions':['相同标题不等于同一告警实例'],'source_refs':[]})
        append_source(catalog['semantic']['concepts']['SecurityAlert_Title'],cid)
        alert_nodes=official[::2]; bridges=official[1::2]+ends
        event_ids=[]
        for n in alert_nodes:
            title=g.nodes[n]['name']; eid='event_'+digest(title)[:16];event_ids.append(eid)
            if eid not in catalog['semantic']['events']:
                catalog['semantic']['events'][eid]={'id':eid,'title':title,'description':g.nodes[n]['description'],
                  'mapping':{'table':'SecurityAlert','field':'AlertName','value':title},'source_refs':[]}
            append_source(catalog['semantic']['events'][eid],cid)
        grouped=collections.defaultdict(list)
        for n in starts:grouped[concept(g.nodes[n])].append(n)
        variants=[]; offline=[]; live=[]; episode_ids=set()
        rows=[]
        for n,d in g.nodes(data=True):
            if d['type']=='alert':
                entry=json.loads(d['entry'])
                rows.append({k:entry.get(k) for k in ('SystemAlertId','AlertName','TimeGenerated','Entities')})
        for start_concept,start_nodes in sorted(grouped.items()):
            chain=[start_concept]+[concept(g.nodes[n]) for n in bridges]
            steps=[]; values={g.nodes[n]['value'] for n in start_nodes}; offline_steps=[]
            for i,(known,wanted) in enumerate(zip(chain,chain[1:])):
                em=new_episode(known,wanted);eid=em['id'];episode_ids.add(eid)
                if eid not in catalog['episodic']:catalog['episodic'][eid]=em
                append_source(catalog['episodic'][eid],cid)
                step={'id':f'T{i+1}','episode_id':eid,'event_ref':event_ids[i],
                      'bindings':{'entities':{'input':'seed_entities'} if i==0 else {'step':f'T{i}','field':'entities'},
                                  'event_title':{'semantic_event':event_ids[i]}}}
                steps.append(step)
                title=g.nodes[alert_nodes[i]]['name']
                outcome=step_query([r for r in rows if r['AlertName']==title],known,wanted,values)
                expected=g.nodes[bridges[i]]['value']
                actual={o['graph_value'] for o in outcome['outputs']}
                offline_steps.append({'step':step['id'],'status':outcome['status'],'row_count':len(outcome['outputs']),
                    'expected_node':bridges[i],'expected_present':expected in actual,
                    'values':sorted(actual),'evidence':outcome['outputs']})
                values=actual
            variant={'id':f'from_{start_concept}','known':{'seed_entities':{'concept':start_concept,'cardinality':'many'}},'steps':steps}
            variants.append(variant)
            offline.append({'variant':variant['id'],'passed':all(s['expected_present'] for s in offline_steps),'steps':offline_steps})
            if live_query is not None:
                values={g.nodes[n]['value'] for n in start_nodes}; trace=[]
                for i,(known,wanted) in enumerate(zip(chain,chain[1:])):
                    title=g.nodes[alert_nodes[i]]['name'];em=new_episode(known,wanted)
                    try:
                        outcome=live_query(incident,em,values,title)
                    except Exception as exc:outcome={'status':'sql_error','outputs':[],'error':str(exc)}
                    actual={o['graph_value'] for o in outcome['outputs']}
                    trace.append({'step':f'T{i+1}','status':outcome['status'],'candidate_count':len(outcome['outputs']),
                        'expected_present':g.nodes[bridges[i]]['value'] in actual,
                        'error':outcome.get('error'),'alert_id':outcome.get('alert_id')})
                    if outcome['status']!='ok':break
                    values=actual
                live.append({'variant':variant['id'],'passed':len(trace)==len(steps) and all(t['expected_present'] for t in trace),'steps':trace})
        assert all(o['passed'] for o in offline),f'Official path extraction mismatch in {cid}'
        wanted=concept(g.nodes[ends[0]])
        signature={'variants':variants,'wanted':wanted,'relation':'official_event_scoped_association_chain'}
        pmid='pm_'+digest(signature)[:16]
        if pmid not in catalog['procedural']:
            event_names=[g.nodes[n]['name'] for n in alert_nodes]
            catalog['procedural'][pmid]={'id':pmid,'signature':signature,
              'name':f'{event_names[0]} → {event_names[-1]}：{wanted}',
              'known':{'any_of':[v['known'] for v in variants]},'wanted':{'concept':wanted,'cardinality':'many'},
              'guide':'从已知实体定位“'+event_names[0]+'”，沿事件顺序“'+' → '.join(event_names)+'”逐步提取桥接实体。保留每一步候选及告警来源，最后核验目标事件与题目关系，不能把共现路径当作因果或唯一答案。',
              'variants':variants,'event_sequence':event_ids,
              'policies':{'order':'sequential','candidate_policy':'all deduplicated values with evidence',
                  'empty':'stop','invalid_json':'blocked','truncated':'stop','sql_error':'stop',
                  'weak_identifier':'require scope corroboration','missing_start_input':'resolve before execution; do not use training oracle',
                  'completion':'candidate path, not verified final answer'},
              'source_refs':[]}
        append_source(catalog['procedural'][pmid],cid)
        anchors=[]
        text=q['context']+'\n'+q['question']
        for n in starts:
            value=g.nodes[n]['value']
            anchors.append({'node':n,'concept':concept(g.nodes[n]),'value_literal_in_input':value in text,
                            'value_casefold_in_input':value.casefold() in text.casefold()})
        review={**item,'split':'train','question_record':q,'graph_file':str(graph_file),
          'graph_sha256':hashlib.sha256(graph_file.read_bytes()).hexdigest(),
          'path':official,'nodes':{n:{k:v for k,v in d.items() if k!='entry'} for n,d in nodes.items()},
          'interpretation':{'alerts':len(alert_nodes),'sql_steps_per_variant':len(alert_nodes),
             'start_alternatives':anchors,'target_concept':wanted,
             'weak_links':[n for n in official[1::2] if concept(g.nodes[n]) in ('File_Name','Command_File_Mention','Account_Name','Host_Name','IP_Address')],
             'scope':'official graph witness, not a proof of causality, actor identity or autonomous planning'},
          'merged_pm':pmid,'merged_ems':sorted(episode_ids),'offline_validation':offline,'live_validation':live,
          'offline_scope':'Uses official start entities; no intermediate or target value supplied to queries. Expected values checked only after results.',
          'live_scope':'Same title-scoped SQL and strict decoder; malformed JSON blocks. Uses oracle start when input lacks entity.',
          'delta':{'sm':len(catalog['semantic']['concepts'])-before['semantic'],
                   'em':len(catalog['episodic'])-before['episodic'],'pm':len(catalog['procedural'])-before['procedural']}}
        # One-case transaction: save catalog then audit; retry is idempotent if interrupted.
        configure_projection(catalog)
        write_json(path,catalog)
        write_json(ROOT/'cases'/f'{cid}.json',review)
        completed=len(list((ROOT/'cases').glob('*.json')))
        progress={'processed':completed,'total':len(items),'last_case':cid,
          'sm_concepts':len(catalog['semantic']['concepts']),'sm_events':len(catalog['semantic']['events']),
          'em':len(catalog['episodic']),'pm':len(catalog['procedural']),
          'delta':review['delta'],'offline_ok':all(o['passed'] for o in offline),
          'live_any_variant_ok':any(o['passed'] for o in live) if live else None}
        write_json(ROOT/'progress.json',progress)
        return progress


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--next',action='store_true',required=True)
    args=parser.parse_args()
    print(json.dumps(build_next(),ensure_ascii=False))

if __name__=='__main__':main()
