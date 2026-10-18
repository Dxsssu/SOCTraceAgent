"""Paired multi-agent experiment, resumable per case; no train/test mixing."""
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
    PROJECT_ROOT, INCIDENT_PORTS, load_test_cases,
    deterministic_evaluation,
)
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from .bridge import Bridge
from .environment import PaperQueryExecutor as MySQLQueryExecutor
from .evaluation import judge_config, protocol_manifest, score_case
from .runner import AGENTS, AGENT_ROOT, run_case, write_json

ALL_AGENTS = (*AGENTS, 'paper_baseline', 'ours')
UPSTREAM = PROJECT_ROOT/'data/excytin-bench/github'


def paper_agent_class(module_name, class_name):
    sys.path.insert(0, str(UPSTREAM))
    package = types.ModuleType('secgym.agents')
    package.__path__ = [str(UPSTREAM/'secgym/agents')]
    sys.modules['secgym.agents'] = package
    module = importlib.import_module(f'secgym.agents.{module_name}')
    # Silence only upstream diagnostic prints. Do not redirect process stdout:
    # concurrent cases and the progress display share it. Full messages remain
    # available through get_logging() in per-case progress/result files.
    module.print = lambda *args, **kwargs: None
    return getattr(module, class_name)


def baseline_class():
    return paper_agent_class('baseline_agent', 'BaselineAgent')


def prompt_sauce_class():
    return paper_agent_class('prompt_sauce_agent', 'PromptSauceAgent')


def react_class():
    # Upstream imports an untracked Azure credential module even for OpenAI.
    # Our transport uses LLMConfig; no Azure client is instantiated.
    if 'secgym.config_key' not in sys.modules and not (UPSTREAM/'secgym/config_key.py').exists():
        stub = types.ModuleType('secgym.config_key')
        stub.api_key = ''
        sys.modules['secgym.config_key'] = stub
    return paper_agent_class('react_agent', 'ReActAgent')


def select_cases(cases, n=None, seed=20260914):
    if n is None:
        return list(cases)
    chosen = []
    for incident in INCIDENT_PORTS:
        population = [c for c in cases if c.incident == incident]
        chosen.extend(random.Random(f'{seed}:{incident}').sample(population, min(n, len(population))))
    return chosen


def model_call(bridge, messages, *, stop=None):
    # Bridge performs the one shared quota reservation; do not double-reserve.
    from src.agent.request_limits import memory_output_limit
    limit = next((memory_output_limit(m.get('content')) for m in messages if m.get('role') == 'system' and memory_output_limit(m.get('content'))), None)
    limits = {"max_tokens": limit, "extra_body": {"thinking": {"type": "disabled"}}} if limit else {}
    if stop is not None:
        limits["stop"] = stop
    with OpenAI(api_key=bridge.token, base_url=bridge.url+'/v1', timeout=180, max_retries=0) as client:
        response = client.chat.completions.create(model=bridge.config.model, messages=messages, **limits)
    return response.choices[0].message.content or ''


def run_internal(name, case, output, Baseline, *, max_steps=25, timeout=0, max_llm_calls=0,
                 case_dir=None, stop_event=None):
    path = case_dir if case_dir is not None else output/name/case.case_id
    path.mkdir(parents=True, exist_ok=False)
    write_json(path/'result.json', dict(agent=name, **case.manifest_view(),
               status='running', answer='', actions=[], usage=[]))
    executor = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
    config = LLMConfig.from_env()
    started = time.monotonic()
    error = ''
    workflow = None
    try:
        with Bridge(executor, config, max_steps=max_steps, max_llm_calls=max_llm_calls) as bridge:
            def llm(system, user):
                if timeout and time.monotonic()-started > timeout:
                    raise TimeoutError('Investigation exceeded configured timeout')
                value = model_call(bridge, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
                write_json(path/'usage.json', {'usage': bridge.usage})
                return value
            if name == 'ours':
                workflow = ExcytinBenchWorkflow(max_steps=max_steps, llm=llm)
            else:
                class TransportBaseline(Baseline):
                    def _call_llm(self, messages):
                        if stop_event is not None and stop_event.is_set():
                            raise InterruptedError('Investigation interrupted')
                        if timeout and time.monotonic()-started > timeout:
                            raise TimeoutError('Investigation exceeded configured timeout')
                        value = (model_call(bridge, messages, stop=["Observation:", "observation:"])
                                 if name in {"react", "expel"} else model_call(bridge, messages))
                        write_json(path/'usage.json', {'usage': bridge.usage})
                        return value
                    def act(self, observation):
                        if name not in {'react', 'expel'}:
                            return super().act(observation)
                        from .single_step import act
                        return act(self, observation, can_submit=any(
                            a.get('type') == 'execute_sql' and a.get('success')
                            for a in bridge.actions))

                    def get_logging(self):
                        result = super().get_logging()
                        if name in {'react', 'expel'}:
                            result['boundary_events'] = getattr(self, 'boundary_events', [])
                        return result
                workflow = TransportBaseline(config_list=[{'model': config.model, 'api_key': bridge.token,
                    'base_url': bridge.url+'/v1', 'api_type': 'openai'}], cache_seed=41,
                    max_steps=max_steps, temperature=0, retry_num=1)
                # Upstream PromptSauce references this attribute at the final step
                # but never initializes it. Disable forced summaries explicitly;
                # preserve its prompt/parser and the shared action budget.
                if name == 'prompt_sauce':
                    workflow.submit_summary = False
                if name == "expel":
                    workflow.reset(question_dict=case.question)
                else:
                    workflow.reset()
            client = bridge.app.test_client()
            headers = {'Authorization': 'Bearer '+bridge.token}
            try:
                if name == 'ours':
                    workflow.start({'context': case.question.get('context', ''), 'question': case.question['question']},
                                   runtime_info={'attack': case.incident, 'qid': case.qid, 'split': 'test'})
                observation = f"{case.question.get('context', '')} {case.question['question']}"
                for _ in range(max_steps):
                    if stop_event is not None and stop_event.is_set():
                        bridge.finish('interrupted')
                        break
                    if timeout and time.monotonic()-started > timeout:
                        raise TimeoutError('Investigation exceeded configured timeout')
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
                        if not bridge.finished.is_set():
                            workflow.accept_observation(observation, query_success=bridge.actions[-1]['success'])
                    write_json(path/'progress.json', {'actions': bridge.actions, 'workflow': workflow.get_logging()})
                    if bridge.finished.is_set():
                        break
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            if not bridge.finished.is_set():
                bridge.finish('interrupted' if stop_event is not None and stop_event.is_set()
                              else ('error' if error else 'no_submission'))
            result = dict(agent=name, **case.manifest_view(), model=config.model,
                          status=bridge.terminal_reason, error=error,
                          answer=bridge.answer or '', actions=bridge.actions, usage=bridge.usage,
                          llm_calls=bridge.calls, elapsed_seconds=bridge.ended_at-started,
                          max_steps=max_steps, post_terminal_usage=bridge.post_terminal_usage,
                          evaluation=deterministic_evaluation(case.question.get('answer'), bridge.answer or ''),
                          workflow=workflow.get_logging())
    finally:
        executor.close()
    write_json(path/'result.json', result)
    return result


def judge(case, result, config=None):
    score_case(case, result, config or judge_config())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--agents', nargs='+', choices=ALL_AGENTS,
                   help='Defaults to all agents for a new run, or the saved manifest when resuming')
    p.add_argument('--per-incident', type=int, help='Optional stratified subset; default is all 589 test questions')
    p.add_argument('--max-steps', type=int, default=25)
    p.add_argument('--timeout', type=int, default=0)
    p.add_argument('--max-llm-calls', type=int, default=0)
    p.add_argument('--judge-model', default=None, help='Default: same model as investigation')
    p.add_argument('--seed', type=int, default=20260914)
    p.add_argument('--workers', type=int, default=3)
    args = p.parse_args(argv)
    if (args.per_incident is not None and args.per_incident < 1) or args.workers < 1 or args.max_steps < 2 or min(args.timeout, args.max_llm_calls) < 0:
        p.error('Counts must be positive; max-steps >= 2; protection limits >= 0')
    load_dotenv(PROJECT_ROOT/'.env')
    load_dotenv(PROJECT_ROOT/'docker/excytin-mysql/.env')
    load_dotenv(PROJECT_ROOT/'docker/neo4j/.env')
    population = load_test_cases()
    scoring_config = judge_config(args.judge_model)
    manifest_path = args.output/'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if 'protocol' not in manifest:
            raise ValueError('Legacy evaluation protocol; use a new output directory')
        lookup = {c.case_id: c for c in population}
        cases = [lookup[c['case_id']] for c in manifest['cases']]
        if args.agents is not None and list(dict.fromkeys(args.agents)) != manifest['agents']:
            raise ValueError('Cannot resume with another agent list; use a new output directory')
        expected_protocol = protocol_manifest(cases, max_steps=args.max_steps, judge=scoring_config,
                                              timeout=args.timeout, max_llm_calls=args.max_llm_calls)
        if manifest['protocol'] != expected_protocol:
            raise ValueError('Evaluation settings/source/questions changed; use a new output directory')
        if manifest['model'] != LLMConfig.from_env().model:
            raise ValueError('Cannot resume with another model')
        if manifest.get('versions') != json.loads((AGENT_ROOT/'versions.json').read_text()):
            raise ValueError('Agent versions changed; use a new output directory')
        if 'paper_baseline' in manifest['agents']:
            baseline_hash = hashlib.sha256((UPSTREAM/'secgym/agents/baseline_agent.py').read_bytes()).hexdigest()
            if manifest.get('baseline_source_sha256') != baseline_hash:
                raise ValueError('Official BaselineAgent source changed; use a new output directory')
    else:
        cases = select_cases(population, args.per_incident, args.seed)
        selected_agents = list(dict.fromkeys(args.agents or ALL_AGENTS))
        manifest = dict(created_at=datetime.now(UTC).isoformat(), seed=args.seed, split='test',
            requested_per_incident=args.per_incident, agents=selected_agents, workers=args.workers,
            model=LLMConfig.from_env().model, max_steps=args.max_steps, timeout=args.timeout,
            max_llm_calls=args.max_llm_calls,
            protocol=protocol_manifest(cases, max_steps=args.max_steps, judge=scoring_config,
                                       timeout=args.timeout, max_llm_calls=args.max_llm_calls),
            cases=[c.manifest_view() for c in cases], versions=json.loads((AGENT_ROOT/'versions.json').read_text()),
            baseline_source_sha256=(hashlib.sha256((UPSTREAM/'secgym/agents/baseline_agent.py').read_bytes()).hexdigest()
                                    if 'paper_baseline' in selected_agents else None),
            adaptations=['Native BaselineAgent with reset and transport override; native CLI loops; ours without LTM (detached for reconstruction)',
                         'Shared total action budget, terminal on submission or Nth query; official evaluator with independent judge',
                         'All model calls share temperature=0 and process quota limiter; thinking settings recorded in protocol',
                         'Full test set by default; optional per-incident subsampling; no train supplementation',
                         'Optional non-paper time/LLM limits disabled by default; read-only SQL safety restriction retained'])
        write_json(manifest_path, manifest)
    agents = manifest['agents']
    if not agents or any(a not in ALL_AGENTS for a in agents):
        raise ValueError('Manifest contains an empty or unsupported agent list')
    Baseline = baseline_class() if 'paper_baseline' in agents else None
    jobs = [(agent, case) for case in cases for agent in agents]
    def run(job):
        agent, case = job
        path = args.output/agent/case.case_id/'result.json'
        if path.exists():
            result = json.loads(path.read_text())
            if result.get('status') == 'running':
                result.update(status='interrupted', error='Previous investigation interrupted; not rerun')
            if result.get('evaluation_status') == 'scored':
                return result
        else:
            try:
                if agent in AGENTS:
                    result = run_case(agent, case, args.output, max_steps=args.max_steps,
                                      timeout=args.timeout, max_llm_calls=args.max_llm_calls, llm_eval=False)
                else:
                    result = run_internal(agent, case, args.output, Baseline, max_steps=args.max_steps,
                                          timeout=args.timeout, max_llm_calls=args.max_llm_calls)
            except Exception as exc:
                result = dict(agent=agent, **case.manifest_view(), status='error',
                              error=f'{type(exc).__name__}: {exc}', answer='', actions=[], usage=[])
                write_json(path, result)
        judge(case, result, scoring_config)
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
    return 0 if all(r.get('evaluation_status') == 'scored' for r in completed) else 1


if __name__ == '__main__':
    raise SystemExit(main())
