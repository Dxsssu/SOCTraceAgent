"""Run two reviewed rounds against real MySQL and the production role workflow.

Only context/question enter the investigation. This is a bounded diagnostic,
not a benchmark score; the harness stops before planning a third round.
"""
from __future__ import annotations

import argparse
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv
import yaml

from src.agent.verifier import validate_verification
from src.agent.llm import parse_yaml_response
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor, load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import TrackedLLM
from src.schema.planner_operations import parse_planner_response
from src.schema.reviewer_output import parse_review
from src.schema.ttt_updates import walk
from src.workflow.context import MemoryContextService


class RoundLimitReached(Exception):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', default='incident_5-q0000')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', type=Path, help='Replay completed calls/observations from a failed diagnostic before continuing')
    args = parser.parse_args()
    load_dotenv('docker/excytin-mysql/.env')
    case = next(c for c in load_test_cases() if c.case_id == args.case)
    out = args.output or Path('runtime/two_round_workflow') / datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True, exist_ok=False)
    model = TrackedLLM(None)
    prior = json.loads(args.resume.read_text()) if args.resume else {}
    if prior and prior['case_id'] != case.case_id:
        raise ValueError('Resume case must match')
    replay_calls = []
    for entry in prior.get('calls', []):
        if 'output' not in entry:
            break
        replay_calls.append(entry)
    model.usage.update(prior.get('usage', {}))
    calls, checks, queries = [], [], []
    workflow = None
    report = {'case_id': case.case_id, 'model': model.client.config.model,
              'provider': os.environ.get('SOCAGENT_LLM_PROVIDER', 'paratera'),
              'ltm_enabled': False, 'scope': 'two reviewed rounds; real model and real MySQL; no answer scoring',
              'stop_reason': 'running', 'resumed_from': str(args.resume) if args.resume else None}

    def save():
        report.update(calls=calls, checks=checks, queries=queries, usage=dict(model.usage))
        if workflow and workflow.state:
            report['completed_reviews'] = len(workflow.state.reviews)
            report['workflow'] = workflow.get_logging()
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def check(name, valid):
        checks.append({'name': name, 'passed': bool(valid)})
        if not valid:
            raise AssertionError(name)

    def is_tree(itt):
        return isinstance(itt, list) and all('children' in g and all('children' in e for e in g['children']) for g in itt)

    def llm(system, user):
        role = next(r for r, marker in [('planner', '系统中的 Planner'), ('executor', 'SQL 查询生成器'),
                     ('reviewer', '你是 Reviewer'), ('verifier', '你是 Verifier')] if marker in system)
        if role == 'planner' and workflow.state and len(workflow.state.reviews) >= 2:
            raise RoundLimitReached()
        # Retry feedback is appended after the original input by the production runtime.
        base = user.split('\n上一次输出未通过校验。', 1)[0]
        if role == 'planner' and base.startswith('下面是此次事件'):
            payload = json.loads(base.split('\n', 1)[1])
        else:
            payload = yaml.safe_load(base)
        if role == 'executor':
            check('executor_only_id_objective', set(payload) == {'id', 'objective'})
        elif role == 'reviewer':
            check('reviewer_only_current_task_and_execution', set(payload) == {'task', 'executor_result'}
                  and set(payload['task']) == {'id', 'objective'}
                  and payload['task']['id'] == payload['executor_result']['node_id'])
        elif role == 'verifier':
            check('verifier_nested_tree_no_duplicate_review', set(payload) == {'context', 'question', 'itt'} and is_tree(payload['itt']))
        elif 'itt' in payload:
            check('planner_nested_tree', is_tree(payload['itt']))
        entry = {'index': len(calls)+1, 'role': role, 'system': system, 'input': user}
        calls.append(entry)
        before = dict(model.usage)
        started = time.monotonic()
        save()
        print(f"call {entry['index']} {role} started", flush=True)
        try:
            if entry['index'] <= len(replay_calls):
                saved = replay_calls[entry['index']-1]
                if saved['role'] != role or saved['input'] != user or saved['system'][7:] != system[7:]:
                    raise ValueError('Replay request differs from original checkpoint')
                response = saved['output']
                entry['replayed'] = True
            else:
                response = model(system, user)
            entry['output'] = response
            # Record conformance without bypassing the production retry logic.
            try:
                if role == 'planner': parse_planner_response(response)
                elif role == 'reviewer': parse_review(parse_yaml_response(response))
                elif role == 'verifier': validate_verification(parse_yaml_response(response))
                entry['format_valid'] = True
            except Exception as exc:
                entry.update(format_valid=False, format_error=str(exc))
            return response
        finally:
            entry['seconds'] = round(time.monotonic()-started, 3)
            entry['usage'] = {k: v-before.get(k, 0) for k, v in model.usage.items()}
            save()
            print(f"call {entry['index']} {role} finished {entry['seconds']}s", flush=True)

    workflow = ExcytinBenchWorkflow(llm=llm, memory_contexts=MemoryContextService(enabled=False))
    db = MySQLQueryExecutor(case.incident, password=os.environ['MYSQL_ROOT_PASSWORD'])
    try:
        workflow.start({k: case.question[k] for k in ('context', 'question')})
        while len(workflow.state.reviews) < 2:
            action = workflow.propose_next_action()
            if action.submit:
                report['stop_reason'] = 'verifier_confirmed' if workflow.state.ready_to_submit else 'workflow_budget'
                break
            node = next(n for n, _ in walk(workflow.state.ttt) if n.node_id == action.node_id)
            code = node.metadata['execution_code']
            check('program_writes_sql_before_execution', code == action.content or json.loads(code)[-1] == action.content)
            if len(queries) < len(prior.get('queries', [])):
                saved = prior['queries'][len(queries)]
                if saved['sql'] != action.content:
                    raise ValueError('Replay SQL differs from original checkpoint')
                observation, success, rows = saved['observation'], saved['success'], saved['rows']
            else:
                observation, success, rows = db.execute(action.content)
            queries.append({'node_id': action.node_id, 'sql': action.content, 'success': success,
                            'rows': rows, 'observation': observation,
                            'replayed': len(queries) < len(prior.get('queries', []))})
            print(f"query {len(queries)} success={success} rows={rows}", flush=True)
            try:
                workflow.accept_observation(observation, query_success=success)
            except RoundLimitReached:
                report['stop_reason'] = 'two_round_limit_before_third_planner'
                break
            if workflow.state.ready_to_submit:
                workflow.propose_next_action()
                report['stop_reason'] = 'verifier_confirmed'
                break
        for node, depth in walk(workflow.state.ttt):
            result = node.metadata.get('execution_result')
            if depth == 3 and isinstance(result, dict):
                check('program_persists_structured_evidence', set(result) == {'summary', 'facts', 'limitations', 'source'}
                      and result['source']['execution_id'] in node.evidence_refs)
        check('two_reviewed_rounds_observed', len(workflow.state.reviews) == 2)
    except Exception as exc:
        report.update(stop_reason='error', error=f'{type(exc).__name__}: {exc}')
    finally:
        db.close()
        report['checks_passed'] = all(c['passed'] for c in checks) and report['stop_reason'] != 'error'
        save()
        print(json.dumps({'output': str(out), 'stop_reason': report['stop_reason'],
                          'checks_passed': report['checks_passed'], 'usage': dict(model.usage)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
