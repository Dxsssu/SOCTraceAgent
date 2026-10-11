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
    PROJECT_ROOT, load_test_cases, sample_test_cases,
    deterministic_evaluation,
)
from .bridge import Bridge
from .environment import PaperQueryExecutor as MySQLQueryExecutor
from .evaluation import judge_config, protocol_manifest, score_case

AGENT_ROOT = PROJECT_ROOT / 'external_agents'
BIN = AGENT_ROOT / 'runtime/node_modules'
AGENTS = ('opencode', 'qwen-code', 'cline', 'pi')
INSTRUCTION = '''Investigate the following ExCyTIn incident. Use only the excytin MCP
execute_sql and submit_answer tools. Discover schemas with SHOW TABLES/DESCRIBE as
needed. Do not use files, shell, network, subagents, or outside knowledge to obtain
case evidence. The supplied context and question are your only initial evidence.
You have {max_steps} actions in total. Each SQL attempt (including errors/empty
results) and answer submission counts as one action. The episode ends immediately
on submission or after the last action. Call submit_answer with the concise answer, then stop. No ground truth
or solution is available to you.\n\nContext: {context}\n\nQuestion: {question}'''


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


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
               '-p', prompt]
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
               '--model', model, '--auto-approve', 'true', '--json',
               *(['--timeout', str(timeout)] if timeout else []), prompt]
    elif agent == 'pi':
        data = work/'pi-agent'
        write_json(data/'models.json', {'providers': {'benchmark': {
            'baseUrl': bridge.url+'/v1', 'api': 'openai-completions',
            'apiKey': bridge.token,
            'models': [{'id': model, 'name': model, 'reasoning': False,
                        'input': ['text'], 'contextWindow': 1000000, 'maxTokens': 16384}],
        }}})
        write_json(data/'mcp.json', {'mcpServers': {'excytin': {
            **mcp, 'exposure': 'direct',
        }}})
        env.update(PI_CODING_AGENT_DIR=str(data), PI_OFFLINE='1',
                   PI_SKIP_VERSION_CHECK='1', PI_TELEMETRY='0')
        # Use the pinned local Node runtime; Pi requires Node >= 22.19.
        cmd = [str(BIN/'node/bin/node'), str(BIN/'.bin/pi'),
               '--provider', 'benchmark', '--model', model, '--thinking', 'off',
               '--mode', 'json', '--no-session', '--no-extensions', '-e', 'builtin:mcp',
               '--no-skills', '--no-prompt-templates', '--no-themes', '--no-context-files',
               '--tools', 'mcp__excytin__execute_sql,mcp__excytin__submit_answer', prompt]
    else:
        raise ValueError(f'Unknown agent: {agent}')
    return cmd, env


def stop_process(process):
    # Reap the CLI and its MCP children, even if the parent has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def wait_for_episode(process, bridge, timeout=0, stop_event=None):
    deadline = time.monotonic() + timeout if timeout else None
    while not bridge.finished.wait(0.05):
        if stop_event is not None and stop_event.is_set():
            bridge.finish('interrupted')
            break
        returncode = process.poll()
        if returncode is not None:
            bridge.finish('no_submission' if returncode == 0 else 'error')
            break
        if deadline is not None and time.monotonic() >= deadline:
            bridge.finish('timeout')
            break
    return bridge.terminal_reason


def run_case(agent, case, output, *, max_steps=25, timeout=0, llm_eval=True,
             judge=None, max_llm_calls=0, case_dir=None, stop_event=None):
    case_dir = case_dir if case_dir is not None else output/agent/case.case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    config = LLMConfig.from_env()
    result = dict(agent=agent, **case.manifest_view(), model=config.model,
                  status='running', answer='', actions=[], usage=[], llm_calls=0,
                  max_steps=max_steps)
    write_json(case_dir/'result.json', result)
    started = time.monotonic()
    executor = None
    try:
        executor = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
        with Bridge(executor, config, max_steps=max_steps, max_llm_calls=max_llm_calls) as bridge, tempfile.TemporaryDirectory(prefix='soctrace-agent-') as tmp:
            work = Path(tmp)
            prompt = INSTRUCTION.format(max_steps=max_steps, context=case.question.get('context', ''),
                                        question=case.question['question'])
            cmd, env = prepare(agent, work, bridge, prompt, timeout)
            with (case_dir/'stdout.jsonl').open('w') as stdout, (case_dir/'stderr.log').open('w') as stderr:
                process = subprocess.Popen(cmd, cwd=work, env=env, stdout=stdout, stderr=stderr,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    status = wait_for_episode(process, bridge, timeout, stop_event)
                finally:
                    stop_process(process)
            result.update(status=status, returncode=process.returncode, answer=bridge.answer or '',
                          actions=bridge.actions, usage=bridge.usage, llm_calls=bridge.calls,
                          post_terminal_usage=bridge.post_terminal_usage,
                          elapsed_seconds=bridge.ended_at-started,
                          process_elapsed_seconds=time.monotonic()-started)
    except Exception as exc:
        result.update(status='error', error=f'{type(exc).__name__}: {exc}')
    finally:
        if executor is not None:
            executor.close()
    result.setdefault('elapsed_seconds', time.monotonic()-started)
    result['evaluation'] = deterministic_evaluation(case.question.get('answer'), result['answer'])
    # Persist the investigation before scoring so judge failures never rerun it.
    write_json(case_dir/'result.json', result)
    if llm_eval and not (stop_event is not None and stop_event.is_set()):
        score_case(case, result, judge or judge_config())
        write_json(case_dir/'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=AGENTS, default=list(AGENTS))
    parser.add_argument('--incident', help='Omit to run all eight incidents')
    parser.add_argument('--qid', type=int)
    parser.add_argument('--sample-size', type=int, help='Omit to run the full selected test set')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--max-steps', type=int, default=25)
    parser.add_argument('--timeout', type=int, default=0, help='Optional wall limit; 0 disables it')
    parser.add_argument('--max-llm-calls', type=int, default=0, help='Optional protection; 0 disables it')
    parser.add_argument('--judge-model', default=None, help='Default: same model as investigation; EXCYTIN_JUDGE_MODEL overrides it')
    parser.add_argument('--llm-eval', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--output-dir', type=Path, default=AGENT_ROOT/'runs')
    args = parser.parse_args(argv)
    if args.workers < 1 or args.timeout < 0 or args.max_steps < 1 or args.max_llm_calls < 0:
        parser.error('workers/max-steps must be positive; timeout/max-llm-calls >= 0')
    load_dotenv(PROJECT_ROOT/'.env')
    load_dotenv(PROJECT_ROOT/'docker/excytin-mysql/.env')
    cases = [c for c in load_test_cases() if args.incident is None or c.incident == args.incident]
    if args.qid is not None and args.incident is None:
        parser.error('--qid requires --incident')
    if not cases:
        parser.error('No matching cases')
    if args.qid is not None:
        cases = [c for c in cases if c.qid == args.qid]
        if not cases:
            parser.error('No matching case')
    elif args.sample_size is not None:
        cases = sample_test_cases(cases, sample_size=args.sample_size, seed=args.seed)
    judge = judge_config(args.judge_model) if args.llm_eval else None
    protocol = protocol_manifest(cases, max_steps=args.max_steps, judge=judge,
                                 timeout=args.timeout, max_llm_calls=args.max_llm_calls)
    output = args.output_dir/datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True)
    write_json(output/'manifest.json', {
        'protocol': protocol, 'agents': args.agents, 'cases': [c.manifest_view() for c in cases],
        'workers': args.workers, 'max_steps': args.max_steps, 'timeout': args.timeout,
        'model': LLMConfig.from_env().model, 'memory': 'none',
        'versions': json.loads((AGENT_ROOT/'versions.json').read_text()),
        'adaptations': ['native CLI loop', 'MCP-only model tool schemas',
                        'buffered SSE proxy; temperature=0; thinking settings recorded in protocol',
                        'shared process rate limiter', 'local MySQL boundary, not upstream ExcytinEnv']})
    jobs = [(agent, case) for agent in dict.fromkeys(args.agents) for case in cases]
    def run(job):
        agent, case = job
        try:
            result = run_case(agent, case, output, max_steps=args.max_steps,
                              timeout=args.timeout, llm_eval=args.llm_eval, judge=judge,
                              max_llm_calls=args.max_llm_calls)
            print(f'{agent} {case.case_id}: {result["status"]}, exact={result["evaluation"]["exact_match"]}', flush=True)
            return result
        except Exception as exc:
            result = dict(agent=agent, **case.manifest_view(), status='error', error=type(exc).__name__)
            write_json(output/agent/case.case_id/'result.json', result)
            return result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, jobs))
    from .analyze import stats
    write_json(output/'summary.json', {'results': results, 'protocol': protocol,
        'overall': {a: stats([r for r in results if r['agent'] == a], expected=len(cases))
                    for a in dict.fromkeys(args.agents)}})
    print(f'Artifacts: {output}')
    return 0 if all(r.get('evaluation_status') == 'scored' if args.llm_eval else
                    r['status'] in {'submitted', 'budget_exhausted', 'no_submission'}
                    for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
