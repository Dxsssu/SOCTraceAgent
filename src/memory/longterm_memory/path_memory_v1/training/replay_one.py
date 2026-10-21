"""Replay one case using exported, merged memories and real MySQL queries."""
import argparse
import json
from functools import lru_cache
import networkx as nx
from .core import ROOT,write_json
from .backend import RawLogSource,ReplayBackend
from .runtime import MemoryBundle


@lru_cache(maxsize=8)
def expected_alert_ids(graph_file):
    graph=nx.read_graphml(graph_file)
    return {str(n):json.loads(d['entry'])['SystemAlertId'] for n,d in graph.nodes(data=True) if d['type']=='alert'}


def replay(case_id,backend=None,bundle=None,index=None):
    file=ROOT/'cases'/f'{case_id}.json';case=json.loads(file.read_text())
    bundle=bundle or MemoryBundle()
    index=index or json.loads((ROOT/'case_memory_index.json').read_text())['cases']
    ref=index[case_id];owned=backend is None
    if owned:backend=ReplayBackend(case_id.split('_train_')[0],RawLogSource())
    output=[];specs=[]
    if ref['kind']=='PM':
        for route in ref['allowed_variants']:
            for concept in ref['allowed_inputs'][route]:specs.append((route,concept,None))
    else:
        for eid in ref['episodes']:specs.append((eid,bundle.em[eid]['signature']['known'],eid))
    try:
        for route,known,direct_em in specs:
            anchors=[s for s in case['interpretation']['start_alternatives'] if s['concept']==known]
            values={case['nodes'][s['node']]['value'] for s in anchors}
            if direct_em:
                title=bundle.sm['events'][ref['event_sequence'][0]]['title']
                result=bundle.execute_steps(backend,[('T1',direct_em,title)],known,values)
            else:
                result=bundle.execute(backend,ref['pm'],ref['scenario'],route,known,values)
            # Only after the entire route runs, compare observed values with the official path.
            expected_nodes=case['path'][1::2]+[str(case['question_record']['end_entities'][0])]
            alert_ids=expected_alert_ids(case['graph_file'])
            trace=[]
            expected_inputs=values
            for i,s in enumerate(result['trace']):
                expected=case['nodes'][expected_nodes[i]]['value']
                expected_alert=alert_ids[case['path'][2*i]]
                witnesses=[r for r in s['outputs'] if r['graph_value']==expected
                           and set(r['matched_inputs']) & expected_inputs and r['alert_id']==expected_alert]
                trace.append({'step':s['step'],'episode_id':s['episode_id'],'status':s['status'],
                  'candidate_count':len(s['outputs']),'unique_values':sorted({r['graph_value'] for r in s['outputs']}),
                  'expected_present':bool(witnesses),'query_count':s['query_count'],
                  'expected_alert_id':expected_alert,
                  'witnesses':witnesses[:3],'witness_count':len(witnesses),
                  'sql':s.get('sql'),'parameters':s.get('parameters'),'error':s.get('error')})
                expected_inputs={expected}
            output.append({'variant':route,'seed_concept':known,
              'passed':result['status']=='candidate_ready' and len(trace)==len(expected_nodes) and all(s['expected_present'] for s in trace),
              'steps':trace})
    finally:
        if owned:backend.close()
    case['live_validation']=output
    case['live_scope']='Exported merged PM/EM; session-local MySQL projection of original CSV, membership from SecurityIncident.AlertIds. Official graph supplies seed, expected path checked only AFTER full route execution.'
    case['physical_revision']=2;case['verification']='exact_alert_and_edge_lineage';write_json(file,case)
    return {'case':case_id,'passed':any(v['passed'] for v in output),'all_variants_passed':all(v['passed'] for v in output),
      'queries':sum(s['query_count'] for v in output for s in v['steps']),
      'statuses':sorted({s['status'] for v in output for s in v['steps']})}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',required=True);args=p.parse_args()
    print(json.dumps(replay(args.case),ensure_ascii=False))
