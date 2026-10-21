"""Execute an explicitly selected merged PM route; no questions, graphs or gold access."""
import json
from .core import ROOT


class MemoryBundle:
    def __init__(self,root=ROOT):
        self.em={e['id']:e for e in json.loads((root/'episodic.json').read_text())['episodes']}
        self.pm={p['id']:p for p in json.loads((root/'procedural.json').read_text())['procedures']}
        self.sm=json.loads((root/'semantic.json').read_text())

    def execute(self,backend,pm_id,scenario_id,route_id,seed_concept,seed_values):
        p=self.pm[pm_id]
        scope=next(s for s in p['scenarios'] if s['id']==scenario_id)
        if route_id not in scope['allowed_variants'] or seed_concept not in scope['allowed_inputs'][route_id]:
            raise ValueError('Input/route not permitted in this event scenario')
        route=next(v for v in p['variants'] if v['id']==route_id)
        steps=[]
        for i,s in enumerate(route['steps']):
            eid=s['episode_by_input'][seed_concept] if i==0 else s['episode_id']
            event=scope['events'][s['bindings']['event_title']['scenario_event_index']]
            steps.append((s['id'],eid,self.sm['events'][event]['title']))
        return self.execute_steps(backend,steps,seed_concept,seed_values)

    def execute_steps(self,backend,steps,seed_concept,seed_values):
        values=set(seed_values);concept=seed_concept;trace=[]
        for sid,eid,title in steps:
            e=self.em[eid]
            if e['signature']['known']!=concept:raise ValueError('Invalid typed dependency')
            result=backend.execute(e,values,title)
            trace.append({'step':sid,'episode_id':eid,**result})
            if result['status']!='ok':return {'status':result['status'],'trace':trace}
            values={o['graph_value'] for o in result['outputs']}
            concept=e['signature']['wanted']
        return {'status':'candidate_ready','trace':trace,'candidate_values':sorted(values),'concept':concept}
