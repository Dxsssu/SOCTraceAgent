"""Paired five-agent experiment, resumable per case; no train/test mixing."""
from __future__ import annotations
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import random
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from dotenv import load_dotenv
from openai import OpenAI
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import (
    PROJECT_ROOT, INCIDENT_PORTS, load_test_cases, MySQLQueryExecutor,
    deterministic_evaluation, llm_judge_evaluation,
)
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from .bridge import Bridge
from .runner import AGENTS, AGENT_ROOT, run_case, write_json

ALL_AGENTS = (*AGENTS, 'paper_baseline', 'ours')
UPSTREAM = PROJECT_ROOT/'data/excytin-bench/github'


def baseline_class():
    sys.path.insert(0, str(UPSTREAM))
    package = types.ModuleType('secgym.agents')
    package.__path__ = [str(UPSTREAM/'secgym/agents')]
    sys.modules['secgym.agents'] = package
    return importlib.import_module('secgym.agents.baseline_agent').BaselineAgent


def select_cases(cases, n=15, seed=20260914):
    chosen = []
    for incident in INCIDENT_PORTS:
        population = [c for c in cases if c.incident == incident]
        chosen.extend(random.Random(f'{seed}:{incident}').sample(population, min(n, len(population))))
    return chosen


def model_call(bridge, messages):
    # Bridge performs the one shared quota reservation; do not double-reserve.
    with OpenAI(api_key=bridge.token, base_url=bridge.url+'/v1', timeout=180, max_retries=0) as client:
        response = client.chat.completions.create(model=bridge.config.model, messages=messages)
    return response.choices[0].message.content or ''


def run_internal(name, case, output, Baseline):
    path = output/name/case.case_id
    path.mkdir(parents=True, exist_ok=False)
    executor = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
    config = LLMConfig.from_env()
    started = time.monotonic()
    error = ''
    workflow = None
    try:
        with Bridge(executor, config, max_steps=25) as bridge:
            def llm(system, user):
                if time.monotonic()-started > 600:
                    raise TimeoutError('Investigation exceeded 600 seconds')
                value = model_call(bridge, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
                write_json(path/'usage.json', {'usage': bridge.usage})
                return value
            if name == 'ours':
                workflow = ExcytinBenchWorkflow(max_steps=25, llm=llm)
            else:
                class TransportBaseline(Baseline):
                    def _call_llm(self, messages):
                        if time.monotonic()-started > 600:
                            raise TimeoutError('Investigation exceeded 600 seconds')
                        value = model_call(bridge, messages)
                        write_json(path/'usage.json', {'usage': bridge.usage})
                        return value
                workflow = TransportBaseline(config_list=[{'model': config.model, 'api_key': bridge.token,
                    'base_url': bridge.url+'/v1', 'api_type': 'openai'}], cache_seed=None,
                    max_steps=25, temperature=0, retry_num=1)
            client = bridge.app.test_client()
            headers = {'Authorization': 'Bearer '+bridge.token}
            try:
                if name == 'ours':
                    workflow.start({'context': case.question.get('context', ''), 'question': case.question['question']},
                                   runtime_info={'attack': case.incident, 'qid': case.qid, 'split': 'test'})
                observation = f"{case.question.get('context', '')} {case.question['question']}"
                for _ in range(25):
                    if time.monotonic()-started > 600:
                        raise TimeoutError('Investigation exceeded 600 seconds')
                    if name == 'ours':
                        action = workflow.propose_next_action()
                        content, submit = action.content, action.submit
                    else:
                        content, submit = workflow.act(observation)
                    if submit:
                        client.post('/submit_answer', headers=headers, json={'answer': content})
                        break
                    response = client.post('/execute_sql', headers=headers, json={'sql': content}).json
                    observation = response.get('observation', response.get('error', ''))
                    if name == 'ours':
                        workflow.accept_observation(observation, query_success=response.get('success', False))
                    write_json(path/'progress.json', {'actions': bridge.actions, 'workflow': workflow.get_logging()})
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            result = dict(agent=name, **case.manifest_view(), model=config.model,
                          status='submitted' if bridge.answer is not None else 'failed', error=error,
                          answer=bridge.answer or '', actions=bridge.actions, usage=bridge.usage,
                          llm_calls=bridge.calls, elapsed_seconds=time.monotonic()-started,
                          evaluation=deterministic_evaluation(case.question.get('answer'), bridge.answer or ''),
                          workflow=workflow.get_logging())
    finally:
        executor.close()
    write_json(path/'result.json', result)
    return result


def judge(case, result):
    if not result.get('answer'):
        result['llm_evaluation'] = {'llm_judge_answer_correct': False, 'llm_judge_reward': 0,
                                  'llm_judge_answer_analysis': 'No submitted answer.'}
        return
    with Bridge(None, LLMConfig.from_env(), max_llm_calls=3) as bridge:
        def llm(system, user):
            return model_call(bridge, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
        try:
            result['llm_evaluation'] = llm_judge_evaluation(case.question, result['answer'], llm=llm)
        except Exception as exc:
            result['judge_error'] = f'{type(exc).__name__}: {exc}'
        result['judge_usage'] = bridge.usage


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--per-incident', type=int, default=15)
    p.add_argument('--seed', type=int, default=20260914)
    p.add_argument('--workers', type=int, default=3)
    args = p.parse_args(argv)
    if args.per_incident < 1 or args.workers < 1:
        p.error('Counts must be positive')
    load_dotenv(PROJECT_ROOT/'.env')
    load_dotenv(PROJECT_ROOT/'docker/excytin-mysql/.env')
    load_dotenv(PROJECT_ROOT/'docker/neo4j/.env')
    os.environ['SOCAGENT_LTM_BACKEND'] = 'neo4j'
    Baseline = baseline_class()
    population = load_test_cases()
    manifest_path = args.output/'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        lookup = {c.case_id: c for c in population}
        cases = [lookup[c['case_id']] for c in manifest['cases']]
        if manifest['model'] != LLMConfig.from_env().model:
            raise ValueError('Cannot resume with another model')
    else:
        cases = select_cases(population, args.per_incident, args.seed)
        manifest = dict(created_at=datetime.now(UTC).isoformat(), seed=args.seed, split='test',
            requested_per_incident=args.per_incident, agents=list(ALL_AGENTS), workers=args.workers,
            model=LLMConfig.from_env().model, max_steps=25, timeout=600, max_llm_calls=100,
            cases=[c.manifest_view() for c in cases], versions=json.loads((AGENT_ROOT/'versions.json').read_text()),
            baseline_source_sha256=hashlib.sha256((UPSTREAM/'secgym/agents/baseline_agent.py').read_bytes()).hexdigest(),
            adaptations=['Native BaselineAgent with transport override; native CLI loops; current ours with Neo4j SM/PM/conditional EM',
                         'Common SQL boundary; 24 SQL attempts plus submission; local judge, not official-paper results',
                         'All model calls share proxy/temperature=0/thinking disabled and process quota limiter',
                         'incident_38 has only 11 test questions; no duplication or train supplementation',
                         'CLI hard timeout 600 seconds; Python agents check deadline between model/actions (inflight call may overrun)'])
        write_json(manifest_path, manifest)
    jobs = [(agent, case) for case in cases for agent in ALL_AGENTS]
    def run(job):
        agent, case = job
        path = args.output/agent/case.case_id/'result.json'
        if path.exists():
            result = json.loads(path.read_text())
            if 'llm_evaluation' in result:
                return result
        else:
            try:
                if agent in AGENTS:
                    result = run_case(agent, case, args.output, timeout=600)
                else:
                    result = run_internal(agent, case, args.output, Baseline)
            except Exception as exc:
                result = dict(agent=agent, **case.manifest_view(), status='error',
                              error=f'{type(exc).__name__}: {exc}', answer='', actions=[], usage=[])
                write_json(path, result)
        judge(case, result)
        write_json(path, result)
        return result
    completed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            completed.append(result)
            progress = {'completed': len(completed), 'total': len(jobs), 'updated_at': datetime.now(UTC).isoformat(),
                        'last': {k: result.get(k) for k in ('agent', 'case_id', 'status', 'error')}}
            write_json(args.output/'progress.json', progress)
            print('PROGRESS', json.dumps(progress), flush=True)
    write_json(args.output/'complete.json', {'completed': len(completed), 'total': len(jobs)})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
