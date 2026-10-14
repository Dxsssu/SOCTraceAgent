"""Run one real PM retrieval and Planner initialization; no SQL execution."""
import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from src.agent.llm import LLMConfig, call_llm
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import SELECTION_PROMPT, PREREQUISITE_PROMPT
from src.schema.itt import itt_payload
from src.workflow.context import MemoryContextService

root = Path('outputs/planner_checks') / datetime.now().strftime('%Y%m%d-%H%M%S')
root.mkdir(parents=True)
def write(name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
question = json.loads(Path('data/excytin-bench/huggingface/questions/test/incident_5_qa_incident_o1-ga_c42.json').read_text())[1]
core = {k: question[k] for k in ('context', 'question')}
write('input.json', core)
calls = []
def logged(system, prompt):
    kind = 'selection' if system == SELECTION_PROMPT else 'prerequisites' if system == PREREQUISITE_PROMPT else 'planner'
    item = {'kind': kind, 'system': system, 'prompt': prompt}
    calls.append(item)
    write('calls.json', calls)
    started = time.monotonic()
    print(f'Start {kind}', flush=True)
    try:
        item['response'] = call_llm(system, prompt)
        return item['response']
    except Exception as exc:
        item['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        item['seconds'] = round(time.monotonic() - started, 3)
        write('calls.json', calls)
        print(f'End {kind}: {item["seconds"]}s', flush=True)
service = MemoryContextService(procedure_selector=logged)
repo = service.repositories['excytin_bench']
write('manifest.json', {'case_id': 'incident_5-q0001', 'model': LLMConfig.from_env().model, 'repository': type(repo).__name__, 'scope': 'initialization only'})
print(f'Artifacts: {root}; repository: {type(repo).__name__}', flush=True)
workflow = ExcytinBenchWorkflow(llm=logged, memory_contexts=service)
try:
    tree = workflow.start(core, runtime_info={'qid': 'RUNTIME_ONLY_q0001', 'attack': 'RUNTIME_ONLY_incident_5'})
    payload = itt_payload(tree)
    (root / 'itt.yaml').write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
    planner_calls = [c for c in calls if c['kind'] == 'planner']
    section = planner_calls[-1]['system'].split('# 适用的 Procedural Memory\n', 1)[1]
    selected = json.loads(section) if section.strip() else []
    write('selected_pm.json', selected)
    report = {'success': True, 'case_id': 'incident_5-q0001', 'model': LLMConfig.from_env().model,
              'repository': type(repo).__name__, 'pm_names': [p['name'] for p in selected],
              'call_count': len(calls), 'planner_attempts': len(planner_calls),
              'runtime_leaked': any('RUNTIME_ONLY_' in c['system'] + c['prompt'] for c in calls),
              'planner_input_keys': list(json.loads(planner_calls[0]['prompt'])),
              'goal_count': len(payload), 'next_task_id': tree.next_task_id}
    write('report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
finally:
    write('workflow_log.json', workflow.get_logging())
