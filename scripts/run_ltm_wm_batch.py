"""Resumable test-set evaluation of the current LTM/WM workflow."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from src.agent.llm import LLMClient, LLMConfig
from src.benchmarks.excytin_bench.runner import load_test_cases, ExcytinBenchmarkRunner
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from scripts.check_ltm_wm_workflow import audit

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def should_run(record, retry_unanswered=False, retry_incorrect=False):
    if not record or record.get('status') != 'completed':
        return True
    if retry_unanswered and not record.get('answered', False):
        return True
    return retry_incorrect and not record.get('exact_match', False)


def summarize(result, calls):
    w = result.get('workflow', {})
    verdicts = w.get('verifications', [])
    trace = w.get('trace', [])
    usage = Counter()
    roles = {}
    for call in calls:
        u = call.get('usage') or {}
        role = roles.setdefault(call['role'], Counter())
        role['calls'] += 1
        role['failed_calls'] += bool(call.get('error'))
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            usage[key] += u.get(key) or 0
            role[key] += u.get(key) or 0
    selected = Counter(x.get('layer', 'unknown') for x in w.get('memory_retrievals', []) if x.get('selected'))
    return {
        'case_id': result['case_id'], 'incident': result.get('incident'),
        'status': result['status'], 'error': result.get('error', ''),
        'answered': bool(verdicts and verdicts[-1].get('can_answer') == 'yes'),
        'submitted_answer': result.get('submitted_answer', ''),
        'reference': result.get('evaluation', {}).get('ground_truth', ''),
        'exact_match': bool(result.get('evaluation', {}).get('exact_match')),
        'containment_match': bool(result.get('evaluation', {}).get('containment_match')),
        'query_count': result.get('query_count', 0),
        'metadata_queries': w.get('memory_metadata_queries', 0),
        'latency_seconds': result.get('latency_seconds', 0),
        'usage': dict(usage), 'roles': roles, 'selected_memory_layers': dict(selected),
        'format_errors': sum(t['event_type'] == 'role_yaml_invalid' for t in trace),
        'api_retries': sum(t['event_type'] == 'llm_api_retry_scheduled' for t in trace),
        'wm_audit': result.get('wm_audit'),
        'usage_missing_calls': sum(not c.get('usage') for c in calls),
    }


def run_one(case, output, config, max_steps):
    attempt_root = output / 'attempts' / case.case_id
    attempt_root.mkdir(parents=True, exist_ok=True)
    attempt = attempt_root / datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    attempt.mkdir()
    calls = []
    client = None
    started = time.monotonic()
    def llm(system, user):
        role = next((name for name in ('Verifier', 'Reviewer', 'Executor', 'Planner')
                     if '你是 ' + name in system), 'memory').lower()
        entry = {'role': role, 'started_at': datetime.now(UTC).isoformat()}
        tick = time.monotonic()
        try:
            response = client.client.chat.completions.create(
                model=config.model, messages=[{'role': 'system', 'content': system},
                                             {'role': 'user', 'content': user}],
                extra_body={'thinking': {'type': 'disabled'}})
            entry.update(usage=response.usage.model_dump() if response.usage else {},
                         finish_reason=response.choices[0].finish_reason,
                         response=response.choices[0].message.content,
                         reasoning_content=getattr(response.choices[0].message, 'reasoning_content', None))
            return response.choices[0].message.content or ''
        except Exception as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            entry['latency_seconds'] = time.monotonic() - tick
            calls.append(entry)
            with (attempt / 'llm_calls.jsonl').open('a') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    try:
        client = LLMClient(config)
        runner = ExcytinBenchmarkRunner(ltm_enabled=True, llm_eval=False, max_steps=max_steps,
            workflow_factory=lambda limit: ExcytinBenchWorkflow(max_steps=limit, llm=llm),
            output_dir=attempt)
        result = runner.run_case(case, case_dir=attempt, split='test')
        try:
            result['wm_audit'] = audit(result['workflow'])
        except Exception as exc:
            result['wm_audit'] = {'passed': False, 'error': str(exc)}
    except Exception as exc:
        result = {**case.manifest_view(), 'status': 'failed',
                  'error': f'{type(exc).__name__}: {exc}',
                  'latency_seconds': time.monotonic() - started, 'workflow': {},
                  'evaluation': {'ground_truth': case.question.get('answer', '')}}
    finally:
        if client is not None:
            client.client.close()
    result['llm_calls'] = calls
    summary = summarize(result, calls)
    summary['attempt_dir'] = str(attempt.relative_to(output))
    write_json(attempt / 'result.json', result)
    # Latest pointer is published only after the entire attempt has been saved.
    write_json(output / 'cases' / (case.case_id + '.json'), summary)
    return summary


def report(output, records, total):
    rows = sorted(records.values(), key=lambda r: r['case_id'])
    processed = len(rows)
    completed = sum(r['status'] == 'completed' for r in rows)
    correct = sum(r['exact_match'] for r in rows)
    tokens = sum(r['usage'].get('total_tokens', 0) for r in rows)
    summary = dict(expected=total, processed=processed, pending=total-processed,
        completed=completed, failed=processed-completed,
        answered=sum(r['answered'] for r in rows),
        unanswered=sum(r['status'] == 'completed' and not r['answered'] for r in rows),
        exact_match_count=correct, accuracy_all=correct/total if total else 0,
        accuracy_processed=correct/processed if processed else 0,
        total_tokens=tokens, average_tokens=tokens/processed if processed else 0,
        sql_queries=sum(r['query_count'] for r in rows),
        format_errors=sum(r['format_errors'] for r in rows),
        api_retries=sum(r['api_retries'] for r in rows),
        usage_missing_calls=sum(r['usage_missing_calls'] for r in rows),
        roles={}, selected_memory_layers={},
        wm_audit_failures=sum(not (r.get('wm_audit') or {}).get('passed', False) for r in rows),
        by_incident={})
    for row in rows:
        incident = summary['by_incident'].setdefault(row.get('incident'), {'processed': 0, 'correct': 0, 'tokens': 0})
        incident['processed'] += 1
        incident['correct'] += row['exact_match']
        incident['tokens'] += row['usage'].get('total_tokens', 0)
        for layer, count in row['selected_memory_layers'].items():
            summary['selected_memory_layers'][layer] = summary['selected_memory_layers'].get(layer, 0) + count
        for name, stats in row['roles'].items():
            target = summary['roles'].setdefault(name, {})
            for key, value in stats.items(): target[key] = target.get(key, 0) + value
    write_json(output / 'summary.json', summary)
    write_json(output / 'results.json', rows)
    groups = {'runtime_failed': [], 'unanswered': [], 'wrong_answer': []}
    for row in rows:
        category = ('runtime_failed' if row['status'] != 'completed' else
                    'unanswered' if not row['answered'] else
                    'wrong_answer' if not row['exact_match'] else None)
        if category: groups[category].append(row['case_id'])
    write_json(output / 'failure_groups.json', groups)
    lines = ['# 全量测试进度', '',
        f'已处理 {processed}/{total}；运行报错 {processed-completed}；未确认答案 {summary["unanswered"]}。',
        f'严格正确 {correct}；全量分母准确率 {summary["accuracy_all"]:.2%}；已处理分母准确率 {summary["accuracy_processed"]:.2%}。',
        f'当前各题最新一次运行共 {tokens:,} token，平均 {summary["average_tokens"]:,.1f}；调查 SQL {summary["sql_queries"]} 次。',
        '', '评分为规范化字符串精确匹配，不是 LLM reward。错误答案未自动重跑。历史尝试保留在 attempts/，汇总只计算每题最新一次；未返回 usage 的失败调用可能产生未统计费用。',
        '', '| 题目 | 状态 | 已确认 | 正确 | SQL | Token |', '|---|---|---|---|---:|---:|']
    for r in rows:
        lines.append(f'| {r["case_id"]} | {r["status"]} | {r["answered"]} | {r["exact_match"]} | {r["query_count"]} | {r["usage"].get("total_tokens",0)} |')
    (output / 'summary.md').write_text('\n'.join(lines)+'\n')
    return summary


def progress(done, total, summary):
    filled = int(30*done/total) if total else 30
    print(f'\r[{"#"*filled}{"-"*(30-filled)}] {done}/{total} '
          f'correct={summary["exact_match_count"]} failed={summary["failed"]} '
          f'tokens={summary["total_tokens"]:,}', end='', file=sys.stderr, flush=True)


def fingerprint(cases):
    h = hashlib.sha256(json.dumps([c.question for c in cases], sort_keys=True).encode())
    # Snapshot source and memory definitions; never include environment secrets.
    files = sorted(p for p in (ROOT/'src').rglob('*') if p.suffix in {'.py', '.json'} and '__pycache__' not in p.parts)
    files += [Path(__file__), ROOT/'scripts/check_ltm_wm_workflow.py']
    for p in files:
        h.update(str(p.relative_to(ROOT)).encode()); h.update(p.read_bytes())
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workers', type=int, default=20)
    p.add_argument('--output', type=Path, default=Path('runtime/excytin_bench/ltm-wm-full-test'))
    p.add_argument('--provider', choices=['fancybag','paratera','xty'], default='fancybag')
    p.add_argument('--model', default='deepseek-v4.1-flash')
    p.add_argument('--max-steps', type=int, default=12, help='Includes final submit; matches recent smoke tests')
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--retry-unanswered', action='store_true')
    p.add_argument('--retry-incorrect', action='store_true')
    p.add_argument('--report-only', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.workers < 1 or args.max_steps < 2 or args.timeout <= 0: p.error('Invalid workers, max-steps or timeout')
    os.chdir(ROOT)
    cases = load_test_cases()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    with (output / '.lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: p.error('This output directory is already in use')
        records = {c.case_id: r for c in cases if (r := read_json(output/'cases'/(c.case_id+'.json')))}
        if args.report_only:
            print(json.dumps(report(output, records, len(cases)), ensure_ascii=False, indent=2)); return
        signature = dict(split='test', model=args.model, provider=args.provider,
                         max_steps=args.max_steps, timeout=args.timeout, fingerprint=fingerprint(cases))
        previous = read_json(output/'manifest.json')
        if previous and previous['signature'] != signature:
            p.error('Code, memory, dataset or configuration changed. Use a new --output directory for comparable results.')
        pending = [c for c in cases if should_run(records.get(c.case_id), args.retry_unanswered, args.retry_incorrect)]
        print(f'Test cases={len(cases)}, skip={len(cases)-len(pending)}, scheduled={len(pending)}, workers={args.workers}')
        if args.dry_run: return
        os.environ['SOCAGENT_LLM_PROVIDER'] = args.provider
        config = replace(LLMConfig.from_env(), model=args.model, max_retries=0, request_timeout_seconds=args.timeout)
        # Warm the shared read-only CSV cache once, before 20 workers enter it.
        if pending:
            from src.workflow.ltm_backend import raw_source
            raw_source()
        if not previous:
            write_json(output/'manifest.json', dict(signature=signature, workers=args.workers,
                created_at=datetime.now(UTC).isoformat(), cases=[c.manifest_view() for c in cases]))
        summary = report(output, records, len(cases))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_one, c, output, config, args.max_steps): c for c in pending}
            progress(0, len(pending), summary)
            for n, future in enumerate(as_completed(futures), 1):
                result = future.result()
                records[result['case_id']] = result
                summary = report(output, records, len(cases))
                progress(n, len(pending), summary)
        print('\nReport: '+str(output/'summary.md'))


if __name__ == '__main__':
    main()
