"""Opt-in real LLM + MySQL tests; labels accessed only after retrieval/execution."""
import argparse
import json
import os
import random
import time
from dataclasses import replace
from datetime import datetime, timezone
from openai import APIConnectionError, APIStatusError, APITimeoutError
from src.agent.llm import LLMClient, LLMConfig
from .core import ROOT, TRAIN, write_json
from .backend import RawLogSource, ReplayBackend
from .retrieval import Retriever


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--cases',nargs='+',default=['incident_55_train_000','incident_5_train_000','incident_34_train_000'])
    p.add_argument('--provider',default='fancybag')
    p.add_argument('--model',default='deepseek-v4.1-flash')
    p.add_argument('--ablations',action='store_true')
    p.add_argument('--embedding',action='store_true')
    p.add_argument('--mode',choices=['normal','without_PM','SM_only'],default=None)
    p.add_argument('--output',default=str(ROOT/'retrieval_test_results.json'))
    args=p.parse_args()
    os.environ['SOCAGENT_LLM_PROVIDER']=args.provider
    client=LLMClient(replace(LLMConfig.from_env(),model=args.model,request_timeout_seconds=45,max_retries=0))
    calls=[]
    def chat(*,system_prompt,user_prompt):
        for attempt in range(3):
            try:
                response=client.client.chat.completions.create(model=args.model,
                    messages=[{'role':'system','content':system_prompt},{'role':'user','content':user_prompt}],
                    max_tokens=2600,extra_body={'thinking':{'type':'disabled'}})
                raw=response.choices[0].message.content or ''
                print(f'LLM call {len(calls)+1} completed',flush=True)
                calls.append({'usage':response.usage.model_dump() if response.usage else {},'response':raw,'attempt':attempt+1})
                return raw
            except (APIConnectionError,APIStatusError,APITimeoutError) as exc:
                if attempt==2 or isinstance(exc,APIStatusError) and exc.status_code not in (408,429,500,502,503,504):raise
                time.sleep(random.uniform(5,10))
    retriever=Retriever(chat,embed=client.embed if args.embedding else None)
    source=RawLogSource()
    report={'timestamp':datetime.now(timezone.utc).isoformat(),'model':args.model,'provider':args.provider,
            'recall':'lexical+embedding+LLM' if args.embedding else 'lexical+LLM',
            'scope':'Training-memory resubstitution tests, not held-out accuracy. No official seed/path/index passed to retriever. Ablations explicitly hide memory layers.', 'tests':[]}
    modes=[('normal',())]+([('without_PM',('PM',)),('SM_only',('PM','EM'))] if args.ablations else [])
    if args.mode:
        modes=[(args.mode,{'normal':(),'without_PM':('PM',),'SM_only':('PM','EM')}[args.mode])]
    from pathlib import Path
    for case_id in args.cases:
        incident,index=case_id.split('_train_')
        files=list(TRAIN.glob(incident+'_qa_*.json'))
        if not files:
            files=list(TRAIN.rglob(incident+'.json'))
        records=json.loads(files[0].read_text())
        row=records[int(index)]
        for mode,disabled in modes:
            # Test all layers on the first case; other scenarios use normal retrieval.
            if mode!='normal' and case_id!=args.cases[0]:continue
            start=len(calls);backend=None
            item={'case':case_id,'mode':mode,'context':row['context'],'question':row['question']}
            print(f'START {case_id} {mode}',flush=True)
            try:
                backend=ReplayBackend(incident,source)
                item['result']=retriever.run(row['context'],row['question'],backend,disabled=disabled)
                # Labels are only used here after the experiment has completed.
                normalize=lambda s:s.strip().casefold().rstrip('/')
                values=item['result'].get('values',[])
                item['evaluation']={'expected':row['answer'], 'exact':values==[row['answer']],
                   'normalized_single_answer':len(values)==1 and normalize(values[0])==normalize(row['answer']),
                   'answer_in_candidates':any(normalize(v)==normalize(row['answer']) for v in values)}
            except Exception as exc:
                item['error']={'type':type(exc).__name__,'message':str(exc)}
            finally:
                if backend:
                    item['sql_queries_including_metadata']=backend.query_count
                    backend.close()
            item['llm_calls']=calls[start:]
            item['tokens']=sum(c['usage'].get('total_tokens',0) for c in calls[start:])
            report['tests'].append(item)
            write_json(Path(args.output),report)
            print(json.dumps({k:v for k,v in item.items() if k not in ('result','llm_calls','context')},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
