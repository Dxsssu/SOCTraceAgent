"""Deduplicate already individually constructed plans; never generate new case paths."""
import collections
import copy
import json
from .core import ROOT, digest, write_json


def export():
    c=json.loads((ROOT/'catalog.json').read_text())
    families={}; assignment={};direct={}
    for p in c['procedural'].values():
        outputs=[c['episodic'][s['episode_id']]['signature']['wanted'] for s in p['variants'][0]['steps']]
        if len(outputs)==1:
            for source in p['source_refs']:
                direct[source]={'episodes':[v['steps'][0]['episode_id'] for v in p['variants']],
                  'event_sequence':p['event_sequence'],'kind':'direct_EM'}
            continue
        signature={'ordered_output_concepts':outputs,'relation':'event_scoped_association',
                   'policies':p['policies']}
        fid='pm_'+digest(signature)[:16]
        if fid not in families:
            families[fid]={'id':fid,'name':' → '.join(c['semantic']['concepts'][t]['name'] for t in outputs)+'：关联调查流程',
               'signature':signature,'known':{'any_of':[]},'wanted':p['wanted'],
               'guide':'根据已知实体选择适配的入口及完整事件场景，依次取得'+'、'.join(c['semantic']['concepts'][t]['name'] for t in outputs)+'。后续输入只来自前一步观测结果，保留全部候选和告警证据，完成后核验题目要求的目标关系。',
               'variants':{},'scenarios':{},'policies':p['policies'], 'source_refs':[]}
        f=families[fid];vids=[]
        for variant in p['variants']:
            v=copy.deepcopy(variant)
            for index,step in enumerate(v['steps']):
                step.pop('event_ref')
                step['bindings']['event_title']={'scenario_event_index':index}
            v['id']='route_'+digest(v['known'])[:12]
            vid=v['id'];vids.append(vid)
            if vid in f['variants']:assert f['variants'][vid]==v
            else:
                f['variants'][vid]=v;f['known']['any_of'].append(v['known'])
        scope={'events':p['event_sequence'],'allowed_variants':sorted(vids)}
        sid='scope_'+digest(scope)[:12]
        if sid not in f['scenarios']:
            f['scenarios'][sid]={'id':sid,**scope,'name':p['name'],'guide':p['guide'],'source_refs':[]}
        for source in p['source_refs']:
            if source not in f['source_refs']:f['source_refs'].append(source)
            if source not in f['scenarios'][sid]['source_refs']:f['scenarios'][sid]['source_refs'].append(source)
            assignment[source]={'pm':fid,'scenario':sid,'allowed_variants':sorted(vids),'kind':'PM'}
    for f in families.values():
        f['variants']=list(f['variants'].values());f['scenarios']=list(f['scenarios'].values())
        f['selection_rule']='必须同时选择一个scenario及其allowed_variants中的入口；禁止跨scenario混合事件。'
    write_json(ROOT/'semantic.json',{'version':'training-path-v1',**c['semantic']})
    write_json(ROOT/'episodic.json',{'version':'training-path-v1','episodes':list(c['episodic'].values())})
    write_json(ROOT/'procedural.json',{'version':'training-path-v1','procedures':list(families.values())})
    write_json(ROOT/'case_memory_index.json',{'cases':{**assignment,**direct}})
    summary={'cases':len(assignment)+len(direct),'sm_concepts':len(c['semantic']['concepts']),
      'sm_events':len(c['semantic']['events']),'em':len(c['episodic']),'pm':len(families),
      'scenarios':sum(len(f['scenarios']) for f in families.values()),'direct_em_cases':len(direct),
      'pm_step_distribution':dict(collections.Counter(len(f['signature']['ordered_output_concepts']) for f in families.values()))}
    write_json(ROOT/'summary.json',summary)
    return summary

if __name__=='__main__':print(json.dumps(export(),ensure_ascii=False))
