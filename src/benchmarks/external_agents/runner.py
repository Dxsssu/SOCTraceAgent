"""Run native CLI agents against a shared ExCyTIn MCP/model boundary."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from dotenv import load_dotenv
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import (
    PROJECT_ROOT, MySQLQueryExecutor, load_test_cases, sample_test_cases,
    deterministic_evaluation, llm_judge_evaluation,
)
from .bridge import Bridge

AGENT_ROOT = PROJECT_ROOT / 'external_agents'
BIN = AGENT_ROOT / 'runtime/node_modules'
AGENTS = ('opencode', 'qwen-code', 'cline')
INSTRUCTION = '''Investigate the following ExCyTIn incident. Use only the excytin MCP
execute_sql and submit_answer tools. Discover schemas with SHOW TABLES/DESCRIBE as
needed. Do not use files, shell, network, subagents, or outside knowledge to obtain
case evidence. The supplied context and question are your only initial evidence.
You have {queries} SQL attempts (including errors/empty results), then one final
submission. Call submit_answer with the concise answer, then stop. No ground truth
or solution is available to you.\n\nContext: {context}\n\nQuestion: {question}'''


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def prepare(agent, work, bridge, prompt, timeout):
    model = bridge.config.model
    # Do not inherit provider credentials or project runtime settings.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR', 'SYSTEMROOT') if k in os.environ}
    env.update(CI='1', NO_COLOR='1', OPENAI_API_KEY=bridge.token,
               OPENAI_BASE_URL=bridge.url+'/v1', OPENAI_MODEL=model)
    mcp = {'command': sys.executable,
           'args': [str(Path(__file__).with_name('mcp_server.py'))],
           'env': {'EXCYTIN_BRIDGE_URL': bridge.url, 'EXCYTIN_BRIDGE_TOKEN': bridge.token}}
    if agent == 'opencode':
        config = {
            '$schema': 'https://opencode.ai/config.json',
            'model': 'benchmark/'+model, 'small_model': 'benchmark/'+model,
            'share': 'disabled', 'autoupdate': False,
            'provider': {'benchmark': {'npm': '@ai-sdk/openai-compatible', 'name': 'Benchmark',
                'options': {'baseURL': bridge.url+'/v1', 'apiKey': bridge.token},
                'models': {model: {'name': model, 'limit': {'context': 1000000, 'output': 16384}}}}},
            'mcp': {'excytin': {'type': 'local', 'command': [mcp['command'], *mcp['args']],
                               'environment': mcp['env'], 'enabled': True}},
            'permission': {'*': 'deny', 'excytin_*': 'allow'},
        }
        config_path = work/'opencode.json'
        write_json(config_path, config)
        env.update(OPENCODE_CONFIG=str(config_path), OPENCODE_DISABLE_PROJECT_CONFIG='1',
                   OPENCODE_CONFIG_DIR=str(work/'oc-config'), XDG_CONFIG_HOME=str(work/'config'),
                   XDG_DATA_HOME=str(work/'data'), XDG_CACHE_HOME=str(work/'cache'))
        cmd = [str(BIN/'.bin/opencode'), 'run', '--format', 'json', '--model', 'benchmark/'+model, prompt]
    elif agent == 'qwen-code':
        qwen_home = work/'qwen-home'
        write_json(qwen_home/'settings.json', {
            'security': {'auth': {'selectedType': 'openai'}},
            'mcpServers': {'excytin': {**mcp, 'trust': True}},
            'telemetry': {'enabled': False},
            'tools': {'core': ['__no_builtin_tools__']},
        })
        env['QWEN_HOME'] = str(qwen_home)
        cmd = [str(BIN/'.bin/qwen'), '--auth-type', 'openai', '--model', model,
               '--approval-mode', 'yolo', '--output-format', 'stream-json',
               '--max-session-turns', '100', '-p', prompt]
    elif agent == 'cline':
        data = work/'cline-data'
        write_json(data/'settings/providers.json', {
            'version': 1, 'lastUsedProvider': 'openai-compatible', 'modes': {},
            'providers': {'openai-compatible': {'settings': {'provider': 'openai-compatible', 'apiKey': bridge.token,
                'model': model, 'baseUrl': bridge.url+'/v1', 'contextWindow': 1000000,
                'maxTokens': 16384}, 'updatedAt': datetime.now(UTC).isoformat().replace('+00:00', 'Z'), 'tokenSource': 'manual'}}})
        write_json(data/'settings/cline_mcp_settings.json', {'mcpServers': {'excytin': {
            **mcp, 'disabled': False, 'autoApprove': ['execute_sql', 'submit_answer']}}})
        env.update(CLINE_DATA_DIR=str(data), CLINE_DIR=str(work/'cline'),
                   CLINE_TELEMETRY_DISABLED='1', CLINE_NO_AUTO_UPDATE='1',
                   CLINE_SESSION_BACKEND_MODE='local')
        cmd = [str(BIN/'cline/bin/cline'), '--data-dir', str(data), '--provider', 'openai-compatible',
               '--model', model, '--auto-approve', 'true', '--json', '--timeout', str(timeout), prompt]
    else:
        raise ValueError(f'Unknown agent: {agent}')
    return cmd, env


def run_case(agent, case, output, *, max_steps=25, timeout=600, llm_eval=False):
    case_dir = output/agent/case.case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    config = LLMConfig.from_env()
    executor = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
    started = time.monotonic()
    status, returncode = 'failed', None
    try:
        with Bridge(executor, config, max_steps=max_steps) as bridge, tempfile.TemporaryDirectory(prefix='soctrace-agent-') as tmp:
            work = Path(tmp)
            prompt = INSTRUCTION.format(queries=max_steps-1, context=case.question.get('context', ''),
                                        question=case.question['question'])
            cmd, env = prepare(agent, work, bridge, prompt, timeout)
            with (case_dir/'stdout.jsonl').open('w') as stdout, (case_dir/'stderr.log').open('w') as stderr:
                process = subprocess.Popen(cmd, cwd=work, env=env, stdout=stdout, stderr=stderr,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    process.wait(timeout=timeout)
                    returncode = process.returncode
                    status = 'submitted' if bridge.answer is not None else 'no_submission'
                except subprocess.TimeoutExpired:
                    status = 'submitted' if bridge.answer is not None else 'timeout'
                finally:
                    # Also terminate spawned MCP processes after CLI exit.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            result = dict(agent=agent, **case.manifest_view(), model=config.model,
                          status=status, returncode=returncode, answer=bridge.answer or '',
                          actions=bridge.actions, usage=bridge.usage, llm_calls=bridge.calls,
                          elapsed_seconds=time.monotonic()-started,
                          max_steps=max_steps, evaluation=deterministic_evaluation(
                              case.question.get('answer'), bridge.answer or ''))
    finally:
        executor.close()
    if llm_eval and result['answer']:
        result['llm_evaluation'] = llm_judge_evaluation(case.question, result['answer'])
    write_json(case_dir/'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=AGENTS, default=list(AGENTS))
    parser.add_argument('--incident', default='incident_5')
    parser.add_argument('--qid', type=int)
    parser.add_argument('--sample-size', type=int, default=1)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--max-steps', type=int, default=25)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--llm-eval', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=AGENT_ROOT/'runs')
    args = parser.parse_args(argv)
    if args.workers < 1 or args.timeout < 1 or args.max_steps < 2:
        parser.error('workers/timeout must be positive; max-steps >= 2')
    load_dotenv(PROJECT_ROOT/'.env')
    load_dotenv(PROJECT_ROOT/'docker/excytin-mysql/.env')
    cases = [c for c in load_test_cases() if c.incident == args.incident]
    if args.qid is not None:
        cases = [c for c in cases if c.qid == args.qid]
        if not cases:
            parser.error('No matching case')
    else:
        cases = sample_test_cases(cases, sample_size=args.sample_size, seed=args.seed)
    output = args.output_dir/datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True)
    write_json(output/'manifest.json', {
        'agents': args.agents, 'cases': [c.manifest_view() for c in cases],
        'workers': args.workers, 'max_steps': args.max_steps, 'timeout': args.timeout,
        'model': LLMConfig.from_env().model, 'memory': 'none',
        'versions': json.loads((AGENT_ROOT/'versions.json').read_text()),
        'adaptations': ['native CLI loop', 'MCP-only model tool schemas',
                        'buffered SSE proxy; temperature=0; thinking disabled',
                        'shared process rate limiter', 'local MySQL boundary, not upstream ExcytinEnv']})
    jobs = [(agent, case) for agent in dict.fromkeys(args.agents) for case in cases]
    def run(job):
        agent, case = job
        try:
            result = run_case(agent, case, output, max_steps=args.max_steps,
                              timeout=args.timeout, llm_eval=args.llm_eval)
            print(f'{agent} {case.case_id}: {result["status"]}, exact={result["evaluation"]["exact_match"]}', flush=True)
            return result
        except Exception as exc:
            result = dict(agent=agent, case_id=case.case_id, status='error', error=type(exc).__name__)
            write_json(output/agent/case.case_id/'error.json', result)
            return result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, jobs))
    write_json(output/'summary.json', {'results': results})
    print(f'Artifacts: {output}')
    return 0 if all(r['status'] == 'submitted' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
