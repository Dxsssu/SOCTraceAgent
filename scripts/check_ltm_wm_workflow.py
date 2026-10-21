"""Opt-in real-model train/test smoke test for integrated LTM/ITT workflow."""
import argparse
import json
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime
from src.agent.llm import LLMClient, LLMConfig
from src.benchmarks.excytin_bench.runner import ExcytinBenchmarkRunner, BenchmarkCase
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.schema.ttt import TracebackTaskTree
from src.schema.ttt_updates import walk
from src.workflow.ltm_wm import LTMWMController


def audit(logging):
    history=logging.get('ttt_history',[])
    if not history:return {'passed':False,'error':'No ITT snapshots'}
    trees=[TracebackTaskTree.from_dict(h) for h in history]
    assert not any(n.level==3 for n,_ in walk(trees[0])), 'Initial snapshot must contain only G/E'
    for tree in trees:LTMWMController.validate(tree)
    final=trees[-1]
    nodes={n.node_id:n for n,_ in walk(final)}
    actions=[a for a in logging['action_history'] if a['action_type']=='query']
    bootstrap_ids={e['node_id'] for e in logging.get('executions',[]) if e['node_id'].startswith('B')}
    for index,a in enumerate(actions):
        if a['node_id'] in bootstrap_ids:
            assert a['node_id'] not in nodes, 'Preparation must not be an ITT task'
            continue
        task=nodes[a['node_id']]
        prior={v['node_id'] for v in actions[:index]}
        assert set(task.metadata['depends_on'])<=prior
    if bootstrap_ids:
        assert not trees[0].root_nodes, 'Preparation must precede formal G/E construction'
        formal=next((t for t in trees if t.root_nodes), None)
        if formal:
            assert not any(n.level==3 for n,_ in walk(formal)), 'Build G/E before creating T'
        trace=logging.get('trace',[])
        first_task=next((i for i,t in enumerate(trace) if t['event_type']=='action_issued'
            and str(t.get('payload',{}).get('action',{}).get('node_id','')).startswith('T')),len(trace))
        assert not any(t['event_type']=='answer_verified' for t in trace[:first_task]), 'Premature Verifier'
    runs=final.root_nodes[0].metadata['procedure_runs'] if final.root_nodes else []
    return {'passed':True,'snapshot_count':len(trees),'entities':len([n for n in nodes.values() if n.level==2]),
        'tasks':[{'id':n.node_id,'state':n.status.value,'inputs':n.metadata['input_entity_ids'],
                 'depends_on':n.metadata['depends_on'],'outputs':n.metadata['output_entity_ids'],
                 'layer':n.metadata['layer']} for n in nodes.values() if n.level==3],
        'procedures':[{'id':r['id'],'status':r['status'],'expected_steps':len(r['steps']),'task_ids':r['task_ids']} for r in runs]}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--cases',nargs='+',default=['incident_55_train_000','incident_34_train_000','incident_55_train_025'])
    p.add_argument('--split',choices=['train','test'],default='train')
    p.add_argument('--model',default='deepseek-v4.1-flash')
    p.add_argument('--output',type=Path,default=Path('runtime/excytin_bench')/('ltm-wm-'+datetime.now().strftime('%Y%m%d-%H%M%S')))
    a=p.parse_args();os.environ['SOCAGENT_LLM_PROVIDER']='fancybag'
    client=LLMClient(replace(LLMConfig.from_env(),model=a.model,request_timeout_seconds=60,max_retries=0))
    calls=[]
    def llm(system,user):
        r=client.client.chat.completions.create(model=a.model,messages=[{'role':'system','content':system},{'role':'user','content':user}],extra_body={'thinking':{'type':'disabled'}})
        calls.append({'usage':r.usage.model_dump() if r.usage else {},'response':r.choices[0].message.content, 'role': ('verifier' if '你是 Verifier' in system else 'reviewer' if '你是 Reviewer' in system else 'memory'), 'finish_reason': r.choices[0].finish_reason, 'reasoning_content': getattr(r.choices[0].message, 'reasoning_content', None)})
        print(f'LLM {len(calls)} completed',flush=True)
        return r.choices[0].message.content or ''
    runner=ExcytinBenchmarkRunner(ltm_enabled=True,llm_eval=False,max_steps=12,
       workflow_factory=lambda limit:ExcytinBenchWorkflow(max_steps=limit,llm=llm),output_dir=a.output)
    results=[];a.output.mkdir(parents=True,exist_ok=True)
    for cid in a.cases:
        incident,index=cid.split(f'_{a.split}_')
        file=next((Path('data/excytin-bench/huggingface/questions')/a.split).glob(incident+'_qa_*.json'))
        row=json.loads(file.read_text())[int(index)];begin=len(calls)
        print('START '+cid,flush=True)
        result=runner.run_case(BenchmarkCase(incident,int(index),row),case_dir=a.output,split=a.split)
        try:result['wm_audit']=audit(result['workflow'])
        except Exception as exc:result['wm_audit']={'passed':False,'error':str(exc)}
        result['llm_calls']=calls[begin:]
        result['tokens']=sum(x['usage'].get('total_tokens',0) for x in calls[begin:])
        (a.output/(cid+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        compact={k:result.get(k) for k in ('status','error','submitted_answer','evaluation','wm_audit','tokens')}
        compact['case']=cid;results.append(compact)
        (a.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(compact,ensure_ascii=False),flush=True)
    print('OUTPUT '+str(a.output.resolve()),flush=True)

if __name__=='__main__':main()
