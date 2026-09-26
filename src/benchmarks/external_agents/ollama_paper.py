"""Run the upstream paper agent on Ollama, with no thinking and a progress bar."""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import httpx
from openai import OpenAI
from dotenv import load_dotenv

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import (
    PROJECT_ROOT, MySQLQueryExecutor, deterministic_evaluation,
    load_test_cases, sample_test_cases,
)
from .batch import baseline_class
from .bridge import Bridge
from .runner import write_json

UPSTREAM = PROJECT_ROOT / 'data/excytin-bench/github/secgym'


def no_think_messages(messages):
    """Copy history; append the switch to the latest user turn on every call."""
    result = [dict(m) for m in messages]
    for message in reversed(result):
        if message['role'] == 'user':
            message['content'] = message['content'].rstrip() + '\n/no_think'
            return result
    result.append({'role': 'user', 'content': '/no_think'})
    return result


def ollama_call(client, model, messages, context_size):
    payload = dict(model=model, messages=no_think_messages(messages), stream=False,
                   think=False, options={'temperature': 0, 'num_ctx': context_size,
                                         'num_predict': 4096})
    response = client.post('/api/chat', json=payload)
    response.raise_for_status()
    data = response.json()
    if data.get('error'):
        raise RuntimeError(data['error'])
    message = data.get('message', {})
    content = message.get('content') or ''
    if message.get('thinking') or any(part.strip() for part in re.findall(r'<think>(.*?)</think>', content, re.S)):
        raise RuntimeError('Ollama returned thinking despite think=false and /no_think')
    if not content.strip():
        raise RuntimeError('Ollama returned an empty answer')
    if data.get('done_reason') == 'length':
        raise RuntimeError('Ollama response exceeded the 4096 output-token limit')
    return content, {
        'prompt_tokens': data.get('prompt_eval_count', 0),
        'completion_tokens': data.get('eval_count', 0),
        'total_tokens': data.get('prompt_eval_count', 0) + data.get('eval_count', 0),
        'total_duration_ns': data.get('total_duration'),
    }, payload


def paper_evaluator(bridge):
    """Keep upstream judge prompts/reflection/reward; replace provider imports only."""
    source = UPSTREAM / 'evaluator.py'
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and
        n.module in ('autogen', 'secgym.myconfig', 'secgym.utils.utils'))]

    class Transport:
        def __init__(self, **kwargs):
            pass

        def create(self, messages, **kwargs):
            with OpenAI(api_key=bridge.token, base_url=bridge.url + '/v1',
                        timeout=180, max_retries=0) as client:
                return client.chat.completions.create(
                    model=bridge.config.model, messages=messages, **kwargs)

    namespace = dict(OpenAIWrapper=Transport, config_list_4o=[],
                     msging=lambda msg, role='user': {'role': role, 'content': msg},
                     get_full_question=lambda q: f"{q.get('context', '')} {q['question']}".strip())
    exec(compile(tree, str(source), 'exec'), namespace)
    return namespace['LLMEvaluator'](
        config_list=[], cache_seed=41, ans_check_reflection=True,
        sol_check_reflection=True, step_checking=True, strict_check=False)


def validate_judgement(evaluation):
    verdict = evaluation.get('check_ans_reflection', '')
    match = re.search(r'Is_Answer_Correct: (True|False)', verdict)
    if not match:
        raise ValueError('Judge did not return a valid reflected answer verdict')
    if match.group(1) == 'True':
        if evaluation.get('reward') != 1:
            raise ValueError('Correct answer must have reward=1')
        return
    if not evaluation.get('is_json_success') or not evaluation.get('is_reflect_success'):
        raise ValueError('Judge step evaluation/reflection failed; reward is unavailable')
    steps = list(evaluation['check_sol_reflection'].values())
    if any(s.get('is_step_correct') not in ('True', 'False') for s in steps):
        raise ValueError('Invalid step verdict')
    expected = sum(0.4 ** (len(steps) - 1 - i) for i, s in enumerate(steps[:-1])
                   if s['is_step_correct'] == 'True')
    if abs(evaluation['reward'] - min(1, expected)) > 1e-9:
        raise ValueError('Reward does not match the paper discount formula')


def investigate(case, args, Baseline, progress):
    path = args.output / 'cases' / case.case_id
    path.mkdir(parents=True, exist_ok=True)
    result = dict(**case.manifest_view(), agent='paper_baseline', model=args.model,
                  status='running', answer='', actions=[], usage=[], llm_calls=0)
    # Preserve an interrupted investigation instead of silently repeating it on resume.
    write_json(path / 'result.json', result)
    started = time.monotonic()
    executor = None
    try:
        executor = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
        with httpx.Client(base_url=args.base_url.rstrip('/'), trust_env=False,
                          timeout=httpx.Timeout(args.request_timeout, connect=10)) as client:
            class OllamaBaseline(Baseline):
                def _call_llm(self, messages):
                    if time.monotonic() - started > args.case_timeout:
                        raise TimeoutError('Case investigation time budget exceeded')
                    progress(f'{case.case_id} | LLM {result["llm_calls"] + 1}')
                    result['llm_calls'] += 1
                    content, usage, payload = ollama_call(client, args.model, messages, args.num_ctx)
                    result['usage'].append(usage)
                    with (path / 'llm.jsonl').open('a') as stream:
                        stream.write(json.dumps({'request': payload, 'response': content,
                                                 'usage': usage}, ensure_ascii=False) + '\n')
                    return content

            agent = OllamaBaseline(config_list=[dict(model=args.model, api_key='ollama',
                base_url=args.base_url.rstrip('/') + '/v1', api_type='openai')],
                cache_seed=41, max_steps=args.max_steps, temperature=0, retry_num=1)
            # Match upstream run_exp.py: reset before the first action of each case.
            # reset() increments cache_seed and selects O1_PROMPT for Qwen3.
            agent.reset()
            result["initialization"] = "constructor_then_reset_v1"
            result["system_prompt"] = agent.messages[0]["content"]
            observation = f"{case.question.get('context', '')} {case.question['question']}"
            from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
            for step in range(args.max_steps):
                progress(f'{case.case_id} | action {step + 1}/{args.max_steps}')
                content, submit = agent.act(observation)
                if submit:
                    if not content.strip():
                        raise ValueError('Empty submission')
                    result['answer'] = content.strip()
                    result['actions'].append({'type': 'submit_answer', 'answer': result['answer']})
                    result['status'] = 'submitted'
                    break
                if len(result['actions']) >= args.max_steps - 1:
                    observation = 'Query budget exhausted. Submit your best answer now.'
                    continue
                begin = time.monotonic()
                try:
                    sql = ReadOnlySQLValidator.validate(content)
                    observation, success, rows = executor.execute(sql)
                except Exception as exc:
                    observation, success, rows = str(exc), False, 0
                result['actions'].append(dict(type='execute_sql', sql=content,
                    observation=observation, success=success, row_count=rows,
                    elapsed_seconds=time.monotonic() - begin))
                result['workflow'] = agent.get_logging()
                write_json(path / 'result.json', result)
            else:
                result['status'] = 'no_submission'
            result['workflow'] = agent.get_logging()
    except Exception as exc:
        result.update(status='error', error=f'{type(exc).__name__}: {exc}')
    finally:
        if executor:
            executor.close()
    result['elapsed_seconds'] = time.monotonic() - started
    result['evaluation'] = deterministic_evaluation(case.question.get('answer'), result['answer'])
    write_json(path / 'result.json', result)
    return result


def score(case, result, config):
    if result['status'] == 'no_submission':
        result['paper_evaluation'] = {'reward': 0, 'reason': 'Action budget exhausted without submission'}
    elif result['status'] == 'submitted' and 'paper_evaluation' not in result:
        try:
            with Bridge(None, config, max_llm_calls=40) as bridge:
                evaluation = paper_evaluator(bridge).checking(case.question, result['answer'])
                result['judge_usage'] = bridge.usage
                validate_judgement(evaluation)
                result['paper_evaluation'] = evaluation
                result.pop('judge_error', None)
        except Exception as exc:
            result['judge_error'] = f'{type(exc).__name__}: {exc}'


def summarize(results, output):
    scored = [r for r in results if 'paper_evaluation' in r]
    total = sum(r['paper_evaluation']['reward'] for r in scored)
    complete = len(scored) == len(results)
    correct = sum(r['paper_evaluation']['reward'] == 1 for r in scored)
    queries = [a for r in results for a in r.get('actions', []) if a['type'] == 'execute_sql']
    usage = {kind: {key: sum(u.get(key, 0) for r in results for u in r.get(kind, []))
                    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
             for kind in ('usage', 'judge_usage')}
    summary = dict(total_cases=len(results), scored_cases=len(scored), token_usage=usage,
        total_reward=total if complete else None, mean_reward=total / len(results) if complete else None,
        scored_subset_reward=total, correct=correct,
        answer_accuracy=correct / len(results) if complete else None,
        submitted=sum(r['status'] == 'submitted' for r in results),
        sql_attempts=len(queries), sql_successes=sum(a['success'] for a in queries),
        elapsed_investigation_seconds=sum(r.get('elapsed_seconds', 0) for r in results),
        results=results)
    write_json(output / 'summary.json', summary)
    lines = ['# Ollama /no_think 论文 Agent 评测', '',
        f'- 已评分：{len(scored)}/{len(results)}；成功提交：{summary["submitted"]}/{len(results)}。',
        f'- 总 reward：{summary["total_reward"]}；平均 reward：{summary["mean_reward"]}；正确率：{summary["answer_accuracy"]}。',
        f'- SQL 成功：{summary["sql_successes"]}/{len(queries)}；调查总耗时：{summary["elapsed_investigation_seconds"]:.1f} 秒。',
        f'- 调查 tokens：{usage["usage"]["total_tokens"]}；裁判 tokens：{usage["judge_usage"]["total_tokens"]}。',
        '- API/数据库异常或裁判失败不记作模型 0 分，未评分齐全时整体 reward 为 null。',
        '- 论文原版 Agent（每题创建后调用 reset，对齐官方初始化顺序）；每次调用的最新用户消息追加 /no_think，Ollama think=false。',
        '- 使用论文原 evaluator 提示词、答案/步骤反思和 reward 公式；裁判默认 DeepSeek-V4.1-Flash，论文使用 GPT-4o。',
        '- 答案正确得 1，否则标准中间步骤倒序按 0.4、0.16、0.064… 累加，再对全部题目取平均。',
        '', '| Case | Status | Reward |', '|---|---|---:|']
    for r in results:
        lines.append(f'| {r["case_id"]} | {r["status"]} | {r.get("paper_evaluation", {}).get("reward", "未评分")} |')
    (output / 'summary.md').write_text('\n'.join(lines) + '\n')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen3:32b')
    parser.add_argument('--sample-size', type=int, default=10)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--max-steps', type=int, default=25)
    parser.add_argument('--num-ctx', type=int, default=32768)
    parser.add_argument('--request-timeout', type=float, default=180)
    parser.add_argument('--case-timeout', type=float, default=600)
    parser.add_argument('--judge-model', default='DeepSeek-V4.1-Flash')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if min(args.sample_size, args.num_ctx, args.request_timeout, args.case_timeout) <= 0 or args.max_steps < 2:
        parser.error('Counts/timeouts must be positive; max-steps must be >=2')
    load_dotenv(PROJECT_ROOT / '.env')
    load_dotenv(PROJECT_ROOT / 'docker/excytin-mysql/.env')
    config = replace(LLMConfig.from_env(), model=args.judge_model)
    cases = sample_test_cases(load_test_cases(), sample_size=args.sample_size, seed=args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    settings = {**vars(args), 'output': str(args.output), 'judge_base_url': config.base_url,
                'thinking': False, 'prompt_suffix': '/no_think', 'split': 'test',
                'initialization': 'constructor_then_reset_v1', 'initial_cache_seed': 41,
                'cases': [c.manifest_view() for c in cases],
                'source_sha256': {name: hashlib.sha256((UPSTREAM / name).read_bytes()).hexdigest()
                                  for name in ['agents/baseline_agent.py', 'evaluator.py']}}
    manifest = args.output / 'manifest.json'
    if manifest.exists():
        if json.loads(manifest.read_text())['settings'] != settings:
            parser.error('Existing output has different settings; choose another --output directory')
    else:
        write_json(manifest, {'created_at': datetime.now(UTC).isoformat(), 'settings': settings})
    # Metadata-only preflight: no model generation before the visible case loop.
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(args.base_url.rstrip('/') + '/api/tags')
        response.raise_for_status()
        if args.model not in [m['name'] for m in response.json()['models']]:
            parser.error(f'{args.model} is not installed on this Ollama endpoint')
    Baseline = baseline_class()
    results = []
    terminal = sys.stderr
    def progress(stage):
        filled = int(24 * len(results) / len(cases))
        print(f'\r[{"#" * filled}{"-" * (24 - filled)}] {len(results)}/{len(cases)} {stage:<75}',
              end='', file=terminal, flush=True)
    # Upstream print statements go to disk, never corrupt the terminal progress bar.
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with (args.output / 'run.log').open('a') as log, redirect_stdout(log):
        for case in cases:
            path = args.output / 'cases' / case.case_id / 'result.json'
            result = json.loads(path.read_text()) if path.exists() else investigate(case, args, Baseline, progress)
            if result['status'] == 'running':
                result.update(status='interrupted', error='Previous run interrupted; preserved without automatic rerun')
            progress(f'{case.case_id} | judge ({args.judge_model})')
            score(case, result, config)
            write_json(path, result)
            results.append(result)
            progress(f'{case.case_id} | {result["status"]} reward={result.get("paper_evaluation", {}).get("reward", "N/A")}')
            write_json(args.output / 'progress.json', {'completed': len(results), 'total': len(cases)})
    print(file=terminal)
    summary = summarize(results, args.output)
    print(f'Mean reward: {summary["mean_reward"]}; total reward: {summary["total_reward"]}; '
          f'correct: {summary["correct"]}/{len(cases)}; scored: {summary["scored_cases"]}/{len(cases)}')
    print(f'Report: {args.output / "summary.md"}')
    return 0 if summary['scored_cases'] == len(cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
