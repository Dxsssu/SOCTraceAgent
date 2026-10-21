"""Opt-in LTM integration test. It never invokes or edits the workflow."""
import argparse
import json
import os
import random
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError
from src.agent.llm import LLMClient, LLMConfig
from .store import ROOT, MemoryStore
from .retrieval import MemoryRetriever


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', default='fancybag', choices=['fancybag','paratera','xty'])
    parser.add_argument('--model', default=None)
    parser.add_argument('--replay', action='store_true', help='Execute the matched PM against local incident_55 MySQL')
    args = parser.parse_args()
    os.environ['SOCAGENT_LLM_PROVIDER'] = args.provider
    config = LLMConfig.from_env()
    config = replace(config, request_timeout_seconds=60, max_retries=0, model=args.model or config.model)
    client = LLMClient(config)
    calls = []
    def chat(*, system_prompt, user_prompt):
        for attempt in range(3):
            try:
                response = client.client.chat.completions.create(
                    model=config.model, messages=[{'role':'system','content':system_prompt},{'role':'user','content':user_prompt}],
                    max_tokens=2000, extra_body={'thinking':{'type':'disabled'}})
                raw = response.choices[0].message.content or ''
                calls.append({'stage':len(calls)+1,'attempt':attempt+1,'usage':response.usage.model_dump() if response.usage else None,'response':raw})
                return raw
            except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
                retryable = isinstance(exc,(APIConnectionError,APITimeoutError)) or getattr(exc,'status_code',0) in (408,429,500,502,503,504)
                if not retryable or attempt==2:
                    raise
                print(f'Transient {type(exc).__name__}; retry {attempt+1}/2',flush=True)
                time.sleep(random.uniform(5,10))
    fixture = json.loads((ROOT/'training_example.json').read_text())
    # Retrieve the original train input, not the prepared execution inputs or official path.
    source = json.loads(Path(fixture['source_file']).read_text())[fixture['index']]
    result = {'provider':args.provider,'model':config.model,'utc':datetime.now(timezone.utc).isoformat()}
    try:
        result['retrieval'] = MemoryRetriever(MemoryStore(),chat).match(source['context'],source['question'])
        match = result['retrieval']
        if args.replay and match['status']=='matched':
            from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
            load_dotenv('docker/excytin-mysql/.env')
            db = MySQLQueryExecutor('incident_55',password=os.environ['MYSQL_ROOT_PASSWORD'])
            try:
                def query(sql,params):
                    db.cursor.execute(sql,params)
                    fields=[c[0] for c in db.cursor.description]
                    return [dict(zip(fields,row)) for row in db.cursor.fetchall()]
                result['execution'] = MemoryStore().run(match['selected_pm'],match['execution_inputs'],query)
            finally:
                db.close()
            candidates=result['execution']['candidates']
            result['evaluation']={
                'exact_match':candidates==[{'RemoteUrl':source['answer']}],
                'match_ignoring_trailing_slash':len(candidates)==1 and candidates[0]['RemoteUrl'].rstrip('/')==source['answer'].rstrip('/'),
                'scope':'单训练样例的LTM检索与执行；不是workflow评估，未自动验证全部postconditions'}
    except Exception as exc:
        result['error']={'type':type(exc).__name__,'message':str(exc)}
        raise
    finally:
        result['llm_calls']=calls
        result['total_tokens']=sum((c['usage'] or {}).get('total_tokens',0) for c in calls)
        (ROOT/'llm_test_result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in ('llm_calls','execution')},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
