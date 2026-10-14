import hashlib
import json
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from flask import request, has_request_context
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.benchmarks.external_agents import batch, runner
from src.benchmarks.external_agents.bridge import Bridge

ROOT = Path(__file__).resolve().parent
load_dotenv('.env')
load_dotenv('docker/excytin-mysql/.env')
os.environ['SOCAGENT_LTM_BACKEND'] = 'snapshot'
case = next(c for c in load_test_cases() if c.case_id == 'incident_5-q0028')
config = LLMConfig.from_env()
manifest = dict(case_id=case.case_id, context=case.question.get('context',''), question=case.question['question'],
                model=config.model, provider=os.getenv('SOCAGENT_LLM_PROVIDER','paratera'), max_steps=25,
                timeout_seconds=900, memory_backend='snapshot', scoring_llm=False,
                usage_method='sum actual upstream usage for all requests, including repeated context; post-terminal usage separate',
                source_hashes={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [
                    'src/agent/reviewer.py','src/agent/planner.py','src/benchmarks/excytin_bench/workflow.py',
                    'src/benchmarks/external_agents/bridge.py','src/benchmarks/external_agents/runner.py']})
runner.write_json(ROOT/'manifest.json',manifest)

class LoggedBridge(Bridge):
    label = ''
    def record_usage(self, usage):
        entry = dict(usage)
        if has_request_context():
            body = request.get_json(silent=True) or {}
            messages = body.get('messages',[])
            system = '\n'.join(str(m.get('content','')) for m in messages if m.get('role')=='system')
            kind = 'native_opencode'
            if self.label == 'ours':
                if '你是 SQL 查询生成器' in system: kind='executor'
                elif '你是 Reviewer' in system: kind='reviewer'
                elif '最终答案整理器' in system: kind='answer'
                elif '系统中的 Planner' in system: kind='planner'
                else: kind='pm_retrieval'
            entry['_request'] = dict(kind=kind, messages=len(messages),
                message_chars=len(json.dumps(messages,ensure_ascii=False)),
                tool_schema_chars=len(json.dumps(body.get('tools',[]),ensure_ascii=False)))
        super().record_usage(entry)
        runner.write_json(ROOT/(self.label+'-usage-live.json'),dict(usage=self.usage,post_terminal_usage=self.post_terminal_usage))
        print(json.dumps(dict(agent=self.label,call=len(self.usage),kind=entry.get('_request',{}).get('kind'),
            prompt_tokens=entry.get('prompt_tokens'),completion_tokens=entry.get('completion_tokens')),ensure_ascii=False),flush=True)

class OursBridge(LoggedBridge): label='ours'
class OpenCodeBridge(LoggedBridge): label='opencode'
batch.Bridge=OursBridge
runner.Bridge=OpenCodeBridge

def run(name):
    if name=='ours': return batch.run_internal(name,case,ROOT,None,max_steps=25,timeout=900)
    return runner.run_case(name,case,ROOT,max_steps=25,timeout=900,llm_eval=False)

with ThreadPoolExecutor(max_workers=2) as pool:
    futures={pool.submit(run,n):n for n in ('ours','opencode')}
    for future in as_completed(futures):
        name=futures[future]
        try:
            row=future.result()
            print(json.dumps(dict(agent=name,status=row['status'],llm_calls=row.get('llm_calls'),actions=len(row.get('actions',[])),error=row.get('error','')),ensure_ascii=False),flush=True)
        except Exception as exc:
            print(json.dumps(dict(agent=name,error_type=type(exc).__name__),ensure_ascii=False),flush=True)
            raise
