"""Replay initial PM retrieval with auditable suitability decisions, no SQL."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import time

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.memory.longterm_memory.procedural_memory.excytin_bench import DEFAULT_KNOWLEDGE_PATH
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import SELECTION_PROMPT
from src.workflow.context import MemoryContextService


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--cases', nargs='*', default=[])
    parser.add_argument('--holdout', type=int, default=0)
    parser.add_argument('--seed', type=int, default=20261013)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new output directory to preserve prior evidence')
    args.output.mkdir(parents=True)
    all_cases = {c.case_id: c for c in load_test_cases()}
    ids = json.loads(args.manifest.read_text())['case_ids'] if args.manifest else args.cases
    unknown = set(ids) - all_cases.keys()
    if unknown:
        parser.error(f'Unknown cases: {sorted(unknown)}')
    rng = random.Random(args.seed)
    heldout = rng.sample(sorted(set(all_cases) - set(ids)), args.holdout)
    ids = list(dict.fromkeys(ids + heldout))
    if not ids:
        parser.error('Supply cases/manifest or --holdout')
    service = MemoryContextService()
    repo = service.repositories['excytin_bench']
    model = LLMConfig.from_env().model
    write(args.output / 'manifest.json', {'case_ids': ids, 'holdout_ids': heldout, 'seed': args.seed,
        'model': model, 'repository': type(repo).__name__,
        'pm_sha256': hashlib.sha256(DEFAULT_KNOWLEDGE_PATH.read_bytes()).hexdigest(),
        'scope': 'PM retrieval only; no planner/SQL/answers; benchmark cases are not written to PM'})

    def run(cid):
        core = {k: str(all_cases[cid].question.get(k) or '').strip() for k in ('context', 'question')}
        llm = TrackedLLM(model)
        record = {'case_id': cid, 'holdout': cid in heldout, 'input': core, 'calls': []}
        path = args.output / 'cases' / (cid + '.json')
        def logged(system, prompt):
            call = {'kind': 'selection' if system == SELECTION_PROMPT else 'role', 'system': system, 'prompt': prompt}
            record['calls'].append(call)
            write(path, record)
            started = time.monotonic()
            try:
                call['response'] = llm(system, prompt)
                return call['response']
            except Exception as exc:
                call['error'] = f'{type(exc).__name__}: {exc}'
                raise
            finally:
                call['seconds'] = round(time.monotonic() - started, 3)
                write(path, record)
        report = {}
        try:
            record['procedures'] = repo.retrieve_procedures(dict(core, executions=[]), selector=logged,
                role='planner', limit=1, diagnostics=report)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            report['status'] = 'request_error'
        record.update(diagnostics=report, usage=dict(llm.usage))
        write(path, record)
        return record

    results = []
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for future in as_completed([pool.submit(run, cid) for cid in ids]):
            result = future.result()
            results.append(result)
            report = result['diagnostics']
            print(f"{len(results)}/{len(ids)} {result['case_id']} {report.get('status')} {[p['name'] for p in result.get('procedures', [])]}", flush=True)
            write(args.output / 'results.json', [{k: v for k, v in r.items() if k != 'calls'} for r in results])
    usage = Counter()
    for r in results:
        usage.update(r['usage'])
    summary = {'cases': len(results), 'admitted': sum(bool(r.get('procedures')) for r in results),
        'statuses': dict(Counter(r['diagnostics'].get('status') for r in results)),
        'validation_errors': dict(Counter(a['validation_error'] for r in results for a in r['diagnostics'].get('assessments',[]) if a['validation_error'])),
        'request_errors': sum('error' in r for r in results), 'usage': dict(usage),
        'seconds': round(time.monotonic() - start, 3)}
    write(args.output / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
