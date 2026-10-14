"""Sample initial tasks and measure PM selection/admission without Planner or SQL."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import hashlib
import json
import random
import time

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import load_test_cases
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import SELECTION_PROMPT, parse_response
from src.memory.longterm_memory.procedural_memory.excytin_bench import DEFAULT_KNOWLEDGE_PATH
from src.schema import Event
from src.workflow.context import MemoryContextService

ROOT = Path('outputs/planner_checks') / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-pm-batch')
ROOT.mkdir(parents=True)

def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def main():
    rng = random.Random(20261012)
    groups = defaultdict(list)
    for case in load_test_cases():
        groups[case.incident].append(case)
    # Round-robin across shuffled incidents, randomly sampling within each incident.
    incidents = sorted(groups)
    rng.shuffle(incidents)
    for group in groups.values():
        rng.shuffle(group)
    cases = []
    while len(cases) < 20:
        for incident in incidents:
            if groups[incident] and len(cases) < 20:
                cases.append(groups[incident].pop())
    service = MemoryContextService()
    model = LLMConfig.from_env().model
    manifest = {'scope': 'initial Planner PM retrieval only; no Planner generation or SQL',
                'model': model, 'seed': 20261012, 'workers': 3,
                'repository': type(service.repositories['excytin_bench']).__name__,
                'case_ids': [case.case_id for case in cases],
                'snapshot_sha256': hashlib.sha256(DEFAULT_KNOWLEDGE_PATH.read_bytes()).hexdigest()}
    write(ROOT / 'manifest.json', manifest)
    print(f'Artifacts: {ROOT}; model={model}; repository={manifest["repository"]}', flush=True)
    def run(case):
        llm = TrackedLLM(model)
        calls = []
        path = ROOT / (case.case_id + '.json')
        core = {k: str(case.question.get(k) or '').strip() for k in ('context', 'question')}
        result = {'case_id': case.case_id, 'incident': case.incident, 'input': core, 'calls': calls}
        def logged(system, prompt):
            record = {'kind': 'selection' if system == SELECTION_PROMPT else 'prerequisites',
                      'system': system, 'prompt': prompt}
            calls.append(record)
            write(path, result)
            started = time.monotonic()
            try:
                response = llm(system, prompt)
                record['response'] = response
                return response
            except Exception as exc:
                record['error'] = f'{type(exc).__name__}: {exc}'
                raise
            finally:
                record['seconds'] = round(time.monotonic() - started, 3)
                write(path, result)
        started = time.monotonic()
        try:
            view = MemoryContextService(repositories=service.repositories, procedure_selector=logged).planner_view()
            event = Event(event_name='PM initialization check', message='\n'.join(core.values()),
                          context={'memory_profile': 'excytin_bench', 'investigation_input': core,
                                   'runtime_info': {'qid': 'RUNTIME_ONLY_' + case.case_id}})
            context = view.build(event)
            result['selected_pm'] = context.get('procedural_memory', {}).get('procedures', [])
            result['candidate_found'] = any(c['kind'] == 'prerequisites' for c in calls)
            result['admitted'] = bool(result['selected_pm'])
            result['candidate'] = None
            result['missing'] = []
            result['reason'] = 'admitted' if result['admitted'] else 'no_candidate'
            checks = [c for c in calls if c['kind'] == 'prerequisites']
            if checks:
                check = checks[-1]
                result['candidate'] = json.loads(check['prompt'])['procedure']['name']
                try:
                    parsed = parse_response(check.get('response', ''))
                    result['missing'] = parsed.get('missing', []) if isinstance(parsed, dict) else []
                except (ValueError, TypeError):
                    parsed = None
                if not result['admitted']:
                    result['reason'] = 'missing_prerequisites' if result['missing'] else 'invalid_bindings_or_response'
            elif calls:
                try:
                    selected = parse_response(calls[0].get('response', ''))
                    if selected != []:
                        result['reason'] = 'invalid_or_unknown_selection'
                except (ValueError, TypeError):
                    result['reason'] = 'invalid_selection_response'
            result['runtime_leaked'] = any('RUNTIME_ONLY_' in c['system'] + c['prompt'] for c in calls)
        except Exception as exc:
            result.update(reason='request_error', error=f'{type(exc).__name__}: {exc}', admitted=False,
                          candidate_found=any(c['kind'] == 'prerequisites' for c in calls))
        result['seconds'] = round(time.monotonic() - started, 3)
        result['usage'] = dict(llm.usage)
        write(path, result)
        return result
    results = []
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(run, case) for case in cases]):
            result = future.result()
            results.append(result)
            compact = [{k: v for k, v in r.items() if k not in ('calls', 'selected_pm')} for r in results]
            write(ROOT / 'results.json', compact)
            print(f'{len(results)}/20 {result["case_id"]}: {result["reason"]}; candidate={result.get("candidate")}; missing={result.get("missing")}', flush=True)
    usage = Counter()
    for result in results:
        usage.update(result['usage'])
    summary = {'cases': len(results), 'candidate_found': sum(r['candidate_found'] for r in results),
               'admitted': sum(r['admitted'] for r in results), 'reasons': dict(Counter(r['reason'] for r in results)),
               'missing_counts': dict(Counter(s for r in results for s in r.get('missing', []))),
               'candidate_counts': dict(Counter(r['candidate'] for r in results if r.get('candidate'))),
               'admitted_counts': dict(Counter(p['name'] for r in results for p in r.get('selected_pm', []))),
               'runtime_leaks': sum(r.get('runtime_leaked', False) for r in results),
               'usage': dict(usage), 'wall_seconds': round(time.monotonic() - start, 3)}
    summary['candidate_rate'] = summary['candidate_found'] / len(results)
    summary['admission_rate'] = summary['admitted'] / len(results)
    summary['admission_given_candidate'] = summary['admitted'] / summary['candidate_found'] if summary['candidate_found'] else None
    write(ROOT / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
