"""Score a fixed batch snapshot with the official ExCyTIn evaluator; resumable."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.benchmarks.external_agents.evaluation import score_case, judge_config, validate_judgement, UPSTREAM
from scripts.run_procedural_workflow_fancybag import save, read_json


def metrics(results, total):
    scored = [r for r in results if r.get('evaluation_status') == 'scored']
    correct = sum(r['llm_evaluation']['llm_judge_answer_correct'] for r in scored)
    reward = sum(r['paper_evaluation']['reward'] for r in scored)
    investigation_errors = sum(r.get('evaluation_status') == 'investigation_error' for r in results)
    judge_errors = sum(r.get('evaluation_status') == 'judge_error' for r in results)
    finished = len(results) == total
    return dict(total=total, processed=len(results), scored=len(scored), correct=correct,
        total_reward=reward, investigation_errors=investigation_errors, judge_errors=judge_errors,
        scored_subset_acc=correct/len(scored) if scored else None,
        scored_subset_mean_reward=reward/len(scored) if scored else None,
        full_acc_investigation_failures_zero=correct/total if finished and not judge_errors else None,
        full_mean_reward_investigation_failures_zero=reward/total if finished and not judge_errors else None,
        lower_bound_acc=correct/total if total else None,
        lower_bound_mean_reward=reward/total if total else None,
        scoring_complete=finished and not judge_errors)


def score_one(row, case, output, config, retries=2):
    record = dict(case_id=row['case_id'], source_status=row['status'],
        status='submitted' if row['status']=='completed' else 'investigation_error',
        answer=row.get('submitted_answer', ''), attempts=[])
    for attempt in range(retries + 1):
        try:
            score_case(case, record, config)
        except Exception as exc:
            record.update(evaluation_status='judge_error', judge_error=f'{type(exc).__name__}: {exc}')
        record['attempts'].append(dict(attempt=attempt + 1,
            evaluation_status=record.get('evaluation_status'),
            error=record.get('judge_error'), evaluation=record.get('paper_evaluation')))
        save(output / (row['case_id'] + '.json'), record)
        if record.get('evaluation_status') != 'judge_error':
            break
        if attempt < retries:
            time.sleep(10)
    return record


def reusable(record, row, case):
    if not isinstance(record, dict) or record.get('answer') != row.get('submitted_answer', ''):
        return False
    if record.get('case_id') != row['case_id'] or record.get('evaluation_status') != 'scored':
        return False
    try:
        validate_judgement(record['paper_evaluation'], case.question)
        ev = record['llm_evaluation']
        return (ev['llm_judge_reward'] == record['paper_evaluation']['reward']
                and ev['llm_judge_answer_correct'] == (ev['llm_judge_reward'] == 1))
    except (KeyError, ValueError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='batch summary.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reuse', type=Path, help='previous official scoring directory from this experiment')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--retries', type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1 or args.retries < 0:
        parser.error('workers must be positive and retries nonnegative')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    load_dotenv(ROOT / 'docker/excytin-mysql/.env')
    os.environ['SOCAGENT_LLM_PROVIDER'] = 'fancybag'
    config = replace(judge_config(model='deepseek-v4.1-flash'), request_timeout_seconds=120)
    cases = {c.case_id: c for c in load_test_cases()}
    args.output.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (args.output / '.scoring.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    snapshot = args.output / 'snapshot.json'
    rows = read_json(snapshot)
    if rows is None:
        rows = json.loads(args.input.read_text())
        save(snapshot, rows)
    fingerprint = hashlib.sha256((UPSTREAM / 'evaluator.py').read_bytes()).hexdigest()
    manifest = dict(judge_model=config.model, judge_base_url=config.base_url, evaluator_sha256=fingerprint,
        snapshot_cases=len(rows), input=str(args.input.resolve()), workers=args.workers,
        failure_policy='Investigation errors get zero in end-to-end metrics; judge errors remain unscored.',
        created_at=datetime.now().astimezone().isoformat())
    old = read_json(args.output / 'manifest.json')
    if old and any(old.get(k) != manifest[k] for k in ('judge_model','judge_base_url','evaluator_sha256')):
        parser.error('Judge configuration changed; use a new output directory')
    save(args.output / 'manifest.json', manifest)
    if args.reuse:
        reuse_manifest = read_json(args.reuse / 'manifest.json') or {}
        if any(reuse_manifest.get(k) != manifest[k] for k in ('judge_model','judge_base_url')):
            parser.error('Reuse directory uses a different judge')
    results, pending = [], []
    for row in rows:
        case = cases[row['case_id']]
        cached = read_json(args.output / (case.case_id + '.json'))
        if not reusable(cached, row, case) and args.reuse:
            cached = read_json(args.reuse / (case.case_id + '.json'))
            if reusable(cached, row, case):
                cached['reused_from'] = str(args.reuse.resolve())
        if reusable(cached, row, case):
            save(args.output / (case.case_id + '.json'), cached)
            results.append(cached)
        else:
            pending.append(row)
    print(f'SNAPSHOT {len(rows)} REUSED {len(results)} PENDING {len(pending)} OUTPUT {args.output}', flush=True)
    save(args.output / 'metrics.json', metrics(results, len(rows)))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(score_one, r, cases[r['case_id']], args.output, config, args.retries) for r in pending]
        for future in as_completed(jobs):
            results.append(future.result())
            report = metrics(results, len(rows))
            save(args.output / 'metrics.json', report)
            print(f"PROGRESS {len(results)}/{len(rows)} scored={report['scored']} judge_errors={report['judge_errors']}", flush=True)
    save(args.output / 'results.json', sorted(results, key=lambda r:r['case_id']))
    print('FINAL', json.dumps(metrics(results, len(rows))), flush=True)


if __name__ == '__main__':
    main()
