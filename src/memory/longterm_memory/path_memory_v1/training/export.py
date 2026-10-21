"""Merge related investigation strategies while preserving executable typed routes."""
import collections
import copy
import itertools
import json
from .core import ROOT,digest,write_json

FAMILY={
 'Account_Name':'account','Account_SID':'account','Account_AAD_ID':'account','Account_Derived_Email':'account',
 'Host_Name':'host','Device_AAD_ID':'host','Process_Identity':'process','Command_File_Mention':'process',
 'File_Name':'file','IP_Address':'ip','URL':'url','Mailbox_Address':'mail','Mail_Recipient':'mail',
 'Mail_Sender':'mail','Mail_Sender_IP':'mail','Cloud_App_ID':'app','Cloud_App_Instance':'app',
 'OAuth_Object_ID':'app','OAuth_App_ID':'app','Azure_Resource_ID':'resource',
 'Azure_Subscription_ID':'resource','Azure_Resource_Group':'resource'}
LABEL={'account':'账户','host':'主机','process':'进程及命令行线索','file':'文件','ip':'IP','url':'URL',
       'mail':'邮件实体','app':'云应用','resource':'云资源'}


def strategy_signature(outputs):
    # Collapse repeated investigative stages, NOT actual execution steps.
    stages=[k for k,_ in itertools.groupby(FAMILY[t] for t in outputs)]
    return {'ordered_stages':stages,'relation':'event_scoped_association','execution':'exact_typed_route'}


def export():
    c=json.loads((ROOT/'catalog.json').read_text())
    families={};assignment={};direct={}
    for p in c['procedural'].values():
        outputs=[c['episodic'][s['episode_id']]['signature']['wanted'] for s in p['variants'][0]['steps']]
        if len(outputs)==1:
            for source in p['source_refs']:
                direct[source]={'episodes':[v['steps'][0]['episode_id'] for v in p['variants']],
                  'event_sequence':p['event_sequence'],'kind':'direct_EM'}
            continue
        signature=strategy_signature(outputs);fid='pm_'+digest(signature)[:16]
        if fid not in families:
            stages=signature['ordered_stages']
            families[fid]={'id':fid,'name':' → '.join(LABEL[t] for t in stages)+'关联调查',
              'signature':signature,'known':{'any_of':[]},'wanted':{'any_of':[]},
              'guide':'从已知实体选择满足输入与目标类型的具体路线及完整事件场景，依次调查'+'、'.join(LABEL[t] for t in stages)+'。严格按路线的EM依赖顺序传递观测结果，保留证据和候选；阶段归并不代表标识等价，不可跳过重复追踪步骤。',
              'variants':{},'scenarios':{},'policies':p['policies'],'source_refs':[]}
        f=families[fid]
        rid='route_'+digest({'typed_outputs':outputs})[:16]
        allowed_inputs=[]
        for variant in p['variants']:
            v=copy.deepcopy(variant);v.pop('id');v['wanted']=p['wanted']
            for index,step in enumerate(v['steps']):
                step.pop('event_ref');step['bindings']['event_title']={'scenario_event_index':index}
            seed=variant['known']['seed_entities']['concept']
            first_em=v['steps'][0].pop('episode_id')
            v['steps'][0]['episode_by_input']={seed:first_em}
            v['id']=rid;v['known']={'any_of':[variant['known']]}
            if rid not in f['variants']:f['variants'][rid]=v
            else:
                saved=f['variants'][rid]
                assert saved['steps'][1:]==v['steps'][1:] and saved['wanted']==v['wanted']
                saved['steps'][0]['episode_by_input'][seed]=first_em
                if variant['known'] not in saved['known']['any_of']:saved['known']['any_of'].append(variant['known'])
            allowed_inputs.append(seed)
            if variant['known'] not in f['known']['any_of']:f['known']['any_of'].append(variant['known'])
            if v['wanted'] not in f['wanted']['any_of']:f['wanted']['any_of'].append(v['wanted'])
        sid='scope_'+digest(p['event_sequence'])[:16]
        if sid not in f['scenarios']:
            f['scenarios'][sid]={'id':sid,'events':p['event_sequence'],'allowed_variants':[],
                'allowed_inputs':{},'guide':p['guide'],'source_refs':[]}
        scope=f['scenarios'][sid]
        if rid not in scope['allowed_variants']:scope['allowed_variants'].append(rid)
        scope['allowed_inputs'][rid]=sorted(set(scope['allowed_inputs'].get(rid,[])+allowed_inputs))
        for source in p['source_refs']:
            if source not in f['source_refs']:f['source_refs'].append(source)
            if source not in scope['source_refs']:scope['source_refs'].append(source)
            assignment[source]={'pm':fid,'scenario':sid,'allowed_variants':[rid],
                                'allowed_inputs':{rid:sorted(set(allowed_inputs))},'kind':'PM'}
    for f in families.values():
        f['variants']=list(f['variants'].values());f['scenarios']=list(f['scenarios'].values())
        f['selection_rule']='同时选定完整scenario和其中的typed variant，校验known及wanted；不得跨场景混用，不得互换SID/Name等标识。'
        f['step_range']=[min(len(v['steps']) for v in f['variants']),max(len(v['steps']) for v in f['variants'])]
    write_json(ROOT/'semantic.json',{'version':'training-path-v2',**c['semantic']})
    write_json(ROOT/'episodic.json',{'version':'training-path-v2','episodes':list(c['episodic'].values())})
    write_json(ROOT/'procedural.json',{'version':'training-path-v2','procedures':list(families.values())})
    write_json(ROOT/'case_memory_index.json',{'cases':{**assignment,**direct}})
    summary={'cases':len(assignment)+len(direct),'sm_concepts':len(c['semantic']['concepts']),
       'sm_events':len(c['semantic']['events']),'em':len(c['episodic']),'pm':len(families),
       'scenarios':sum(len(f['scenarios']) for f in families.values()),
       'typed_routes':sum(len(f['variants']) for f in families.values()),'direct_em_cases':len(direct),
       'route_step_distribution':dict(collections.Counter(len(v['steps']) for f in families.values() for v in f['variants']))}
    write_json(ROOT/'summary.json',summary)
    return summary

if __name__=='__main__':print(json.dumps(export(),ensure_ascii=False))
