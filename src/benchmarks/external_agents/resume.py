"""Run native agents with persistent results, progress and retry-on-next-invocation."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time

from dotenv import load_dotenv
from src.agent.llm import LLMConfig
from .runner import (PROJECT_ROOT, AGENT_ROOT, INSTRUCTION, load_test_cases,
                     sample_test_cases, run_case, write_json)
from .evaluation import judge_config, protocol_manifest, score_case
from .analyze import stats

TERMINAL = {'submitted', 'budget_exhausted', 'no_submission'}


def completed(result):
    # Wrong answers and normal budget exhaustion are completed experiments too.
    return (result.get('status') in TERMINAL
            and result.get('evaluation_status') == 'scored'
            and isinstance(result.get('llm_evaluation'), dict)
            and not result.get('judge_error'))


def read_result(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def score_progress(results):
    scored = [r['llm_evaluation'] for r in results.values() if completed(r)]
    reward = sum(r['llm_judge_reward'] for r in scored)
    return dict(complete=len(scored),
                correct=sum(bool(r['llm_judge_answer_correct']) for r in scored),
                total_reward=reward,
                scored_mean_reward=reward/len(scored) if scored else None)


@contextmanager
def output_lock(output):
    output.mkdir(parents=True, exist_ok=True)
    with (output/'.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'该结果目录已有运行中的任务：{output}') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def retry_case(case, output, args, judge, stop_event):
    agent = getattr(args, 'agent', 'opencode')
    directory = output/'cases'/case.case_id
    path = directory/'result.json'
    previous = read_result(path)
    if completed(previous):
        return previous
    if stop_event.is_set():
        return previous
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    # An intact investigation can be scored again without another paid run.
    if previous.get('status') in TERMINAL:
        write_json(output/'attempts'/case.case_id/stamp/'result.json', previous)
        try:
            score_case(case, previous, judge)
        except Exception as exc:
            previous.update(evaluation_status='judge_error', judge_error=type(exc).__name__)
        write_json(path, previous)
        return previous
    if directory.exists():
        archive = output/'attempts'/case.case_id/stamp
        archive.parent.mkdir(parents=True, exist_ok=True)
        directory.rename(archive)
    try:
        if agent in {'paper_baseline', 'prompt_sauce', 'react', 'expel'}:
            from .batch import run_internal
            result = run_internal(agent, case, output, args.baseline_class,
                                  case_dir=directory, max_steps=args.max_steps,
                                  timeout=args.timeout, max_llm_calls=args.max_llm_calls,
                                  stop_event=stop_event)
            if not stop_event.is_set():
                score_case(case, result, judge)
                write_json(path, result)
            return result
        return run_case(agent, case, output, case_dir=directory,
                        max_steps=args.max_steps, timeout=args.timeout,
                        max_llm_calls=args.max_llm_calls, judge=judge,
                        stop_event=stop_event)
    except Exception as exc:
        # Keep a saved answer even if judging raised unexpectedly.
        result = read_result(path)
        if result.get('status') in TERMINAL:
            result.update(evaluation_status='judge_error', judge_error=type(exc).__name__)
        else:
            result.update(agent=agent, **case.manifest_view(), status='error',
                          evaluation_status='investigation_error', error=type(exc).__name__)
        write_json(path, result)
        return result


def execute(args, output, cases, manifest, judge):
    manifest_path = output/'manifest.json'
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old != manifest:
            raise ValueError('题目、模型、评分协议或 CLI 版本与已有结果不同，请使用另一个 --output 目录。')
    else:
        if (output/'cases').exists():
            raise ValueError('已有 cases 但缺少 manifest，不能安全续跑。请使用另一个 --output 目录。')
        write_json(manifest_path, manifest)
    results = {c.case_id: read_result(output/'cases'/c.case_id/'result.json') for c in cases}
    pending = [c for c in cases if not completed(results[c.case_id])]
    skipped = len(cases)-len(pending)
    print(f'模型：{manifest["model"]}；总题数：{len(cases)}；已完成跳过：{skipped}；待运行/补评：{len(pending)}', flush=True)
    print(f'结果目录：{output}', flush=True)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    stop_event = threading.Event()
    started = time.monotonic()
    processed = failed = 0

    def persist():
        rows = [results[c.case_id] for c in cases if results[c.case_id]]
        write_json(output/'summary.json', {
            'results': rows, 'protocol': manifest['protocol'],
            'overall': {getattr(args, 'agent', 'opencode'): stats(rows, expected=len(cases))}})
        write_json(output/'progress.json', {
            'total': len(cases), **score_progress(results),
            'skipped_complete': skipped, 'processed_this_run': processed,
            'failed_this_run': failed, 'remaining_this_run': len(pending)-processed,
            'interrupted': stop_event.is_set()})

    def progress(final=False):
        done = skipped+processed
        width = 24
        filled = int(width*done/len(cases))
        elapsed = time.monotonic()-started
        eta = elapsed/processed*(len(pending)-processed) if processed else None
        scores = score_progress(results)
        mean = scores['scored_mean_reward']
        mean_text = f'{mean:.4f}' if mean is not None else '--'
        eta_text = f'{eta:.0f}s' if eta is not None else '--'
        text = (f'[{"#"*filled}{"-"*(width-filled)}] 已处理={done}/{len(cases)} '
                f'({100*done/len(cases):.1f}%) '
                f'已完成评分={scores["complete"]}/{len(cases)} 完全正确={scores["correct"]} '
                f'Reward总分={scores["total_reward"]:.4f} 已评分平均Reward={mean_text} '
                f'已完成跳过={skipped} 本轮失败={failed} 用时={elapsed:.0f}s ETA={eta_text}')
        if sys.stderr.isatty():
            print('\r\033[K'+text, end='\n' if final else '', file=sys.stderr, flush=True)
        elif final or processed != progress.last:
            print(text, file=sys.stderr, flush=True)
        progress.last = processed
    progress.last = -1
    persist()
    progress()
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(retry_case, c, output, args, judge, stop_event): c for c in pending}
    try:
        while futures:
            done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                case = futures.pop(future)
                results[case.case_id] = future.result()
                processed += 1
                failed += not completed(results[case.case_id])
            if done:
                persist()
            progress()
    except KeyboardInterrupt:
        stop_event.set()
        for future in futures:
            future.cancel()
        print('\n正在停止任务并保存结果；再次执行相同命令可续跑。', file=sys.stderr, flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=stop_event.is_set())
        # Reload atomic case files, including cases saved during interruption.
        results = {c.case_id: read_result(output/'cases'/c.case_id/'result.json') for c in cases}
        persist()
        progress(final=True)
    count = sum(completed(r) for r in results.values())
    print(f'已完成评分：{count}/{len(cases)}；汇总：{output / "summary.json"}', flush=True)
    return 130 if stop_event.is_set() else (0 if count == len(cases) else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=['opencode', 'qwen-code', 'pi', 'paper_baseline', 'prompt_sauce', 'react', 'expel'], default='opencode')
    parser.add_argument('--provider', choices=['paratera', 'fancybag', 'xty'], help='Override SOCAGENT_LLM_PROVIDER for this run')
    parser.add_argument('--model', default=None, help='Default: selected provider model from .env')
    parser.add_argument('--output', type=Path, help='Exact persistent directory; default results/AGENT/MODEL')
    parser.add_argument('--sample-size', type=int, help='Default: all 589 test questions')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--max-steps', type=int, default=25)
    parser.add_argument('--timeout', type=int, default=0)
    parser.add_argument('--max-llm-calls', type=int, default=0)
    args = parser.parse_args(argv)
    if (args.workers < 1 or args.max_steps < 1 or min(args.timeout, args.max_llm_calls) < 0
            or (args.sample_size is not None and args.sample_size < 1)):
        parser.error('workers/max-steps/sample-size must be positive; timeout/max-llm-calls >= 0')
    load_dotenv(PROJECT_ROOT/'.env')
    load_dotenv(PROJECT_ROOT/'docker/excytin-mysql/.env')
    if args.provider:
        os.environ['SOCAGENT_LLM_PROVIDER'] = args.provider
    provider = os.environ.get('SOCAGENT_LLM_PROVIDER', 'paratera').strip().lower()
    if args.model:
        os.environ[f'{provider.upper()}_MODEL'] = args.model
    config = LLMConfig.from_env()
    args.model = config.model
    judge = judge_config(args.model)
    cases = load_test_cases()
    if args.sample_size is not None:
        cases = sample_test_cases(cases, sample_size=args.sample_size, seed=args.seed)
    safe_model = re.sub(r'[^A-Za-z0-9_.-]', '_', args.model).strip('.')
    if not safe_model:
        parser.error('Invalid model name')
    output = (args.output or PROJECT_ROOT/'results'/args.agent/safe_model).resolve()
    manifest = {
        'format': f'{args.agent}-resume-v1', 'model': config.model, 'base_url': config.base_url,
        'agents': [args.agent], 'cases': [c.manifest_view() for c in cases],
        'protocol': protocol_manifest(cases, max_steps=args.max_steps, judge=judge,
                                     timeout=args.timeout, max_llm_calls=args.max_llm_calls),
        'versions': (json.loads((AGENT_ROOT/'versions.json').read_text())
                     if args.agent not in {'prompt_sauce', 'react', 'expel'} else {}),
        'instruction_sha256': hashlib.sha256(INSTRUCTION.encode()).hexdigest(),
        'retry_policy': 'Retry incomplete cases on next invocation; retain all previous attempts',
    }
    if args.agent == 'paper_baseline':
        from .batch import baseline_class, UPSTREAM
        args.baseline_class = baseline_class()
        manifest.pop('instruction_sha256')
        manifest['baseline_source_sha256'] = hashlib.sha256(
            (UPSTREAM/'secgym/agents/baseline_agent.py').read_bytes()).hexdigest()
        manifest['baseline_initialization'] = 'reset; cache_seed=41; original prompt and action parser'
    elif args.agent == 'prompt_sauce':
        from .batch import prompt_sauce_class, UPSTREAM
        args.baseline_class = prompt_sauce_class()
        manifest.pop('instruction_sha256')
        # This Python agent does not depend on installed external CLI versions.
        manifest.pop('versions')
        manifest['provider'] = provider
        manifest['paper_agent_class'] = 'PromptSauceAgent'
        manifest['paper_agent_sources'] = {
            name: hashlib.sha256((UPSTREAM/'secgym/agents'/name).read_bytes()).hexdigest()
            for name in ('prompt_sauce_agent.py', 'agent_utils.py')
        }
        manifest['paper_agent_initialization'] = 'reset; cache_seed=41; original prompt and action parser'
        manifest['adapter_revision'] = 'prompt-sauce-v1-submit-summary-false'
        manifest['memory'] = 'none'
    elif args.agent == 'react':
        from .batch import react_class, UPSTREAM
        args.baseline_class = react_class()
        manifest.pop('instruction_sha256')
        manifest.pop('versions')
        manifest['provider'] = provider
        manifest['paper_agent_class'] = 'ReActAgent'
        root = UPSTREAM/'secgym/agents'
        sources = [root/'react_agent.py', root/'agent_utils.py']
        examples = list((root/'react_examples').iterdir())
        sources.extend(examples)
        manifest['paper_agent_sources'] = {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        }
        manifest['example_order'] = [p.name for p in examples]
        manifest['paper_agent_initialization'] = 'reset; cache_seed=41; original prompt and three demonstrations; local single-step parser'
        manifest['adapter_revision'] = 'react-v2-single-step'
        manifest['memory'] = 'fixed upstream demonstrations; no project SM/PM/EM'
    elif args.agent == 'expel':
        from .batch import UPSTREAM
        from .expel import prepare_assets, configured_class
        insights, experiences, metadata = prepare_assets(
            UPSTREAM, PROJECT_ROOT/'runtime/expel',
            PROJECT_ROOT/'data/excytin-bench/huggingface/questions/train')
        manifest.pop('instruction_sha256')
        manifest.pop('versions')
        manifest.update(provider=provider, paper_agent_class='ExpelAgent',
                        adapter_revision='expel-v2-single-step-aligned-train',
                        memory=metadata)
        manifest['adapter_sha256'] = hashlib.sha256(
            Path(__file__).with_name('expel.py').read_bytes()).hexdigest()
        print(f"ExpeL: loading {metadata['trajectory_count']} training experiences and embedding model...", flush=True)
        args.baseline_class = configured_class(insights, experiences)
    if args.agent in {'react', 'expel'}:
        from .single_step import REVISION
        manifest['action_protocol'] = {
            'revision': REVISION,
            'sha256': hashlib.sha256(Path(__file__).with_name('single_step.py').read_bytes()).hexdigest(),
            'transport_sha256': hashlib.sha256(Path(__file__).with_name('batch.py').read_bytes()).hexdigest(),
            'repair_attempts': 1,
            'require_successful_sql_before_submit': True,
        }
    try:
        with output_lock(output):
            return execute(args, output, cases, manifest, judge)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
