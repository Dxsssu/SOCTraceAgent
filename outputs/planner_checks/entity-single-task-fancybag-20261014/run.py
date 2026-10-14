import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from src.agent.llm import LLMClient
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow

load_dotenv('.env')
os.environ['SOCAGENT_LTM_BACKEND'] = 'snapshot'
os.environ['SOCAGENT_LLM_PROVIDER'] = 'fancybag'
root = Path(__file__).resolve().parent
case = next(c for c in load_test_cases() if c.case_id == 'incident_39-q0088')
initial = {k: case.question.get(k, '') for k in ('context', 'question')}
client = LLMClient()
calls = []
def write(name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
def logged(system, user):
    role = 'planner' if '中的 Planner' in system else 'memory_selector'
    record = {'role': role, 'system': system, 'input': user}
    calls.append(record)
    start = time.monotonic()
    try:
        response = client.chat(system_prompt=system, user_prompt=user)
        record['response'] = response
        if role == 'planner':
            (root / f'planner-response-{sum(c["role"] == "planner" for c in calls)}.yaml').write_text(response)
        return response
    finally:
        record['elapsed_seconds'] = round(time.monotonic() - start, 3)
        write('calls.json', calls)
        print(role, record['elapsed_seconds'], flush=True)
write('input.json', {'case_id': case.case_id, **initial})
print(json.dumps({'case': case.case_id, 'provider': os.environ.get('SOCAGENT_LLM_PROVIDER', 'paratera'), 'model': client.config.model, 'input': initial}, ensure_ascii=False), flush=True)
w = ExcytinBenchWorkflow(llm=logged)
try:
    tree = w.start(initial)
    result = {'status': 'passed', 'model': client.config.model, 'next_task_id': tree.next_task_id, 'tree': tree.to_dict(), 'memory_retrievals': w.state.retrieval_log}
except Exception as exc:
    result = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
write('result.json', result)
print(json.dumps({k: v for k, v in result.items() if k not in ('tree', 'memory_retrievals')}, ensure_ascii=False), flush=True)
