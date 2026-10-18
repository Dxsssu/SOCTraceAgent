"""Summarize retained PM experiments without turning partial matches into successes."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
import random
from pathlib import Path


def wilson(successes, count):
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    denominator = 1 + z*z/count
    center = (p + z*z/(2*count)) / denominator
    half = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / denominator
    return [max(0, center-half), min(1, center+half)]


def termination_details(result):
    """Separate a clean workflow exit from actually supplying an answer."""
    workflow = result.get('workflow', {})
    exits = [t.get('payload', {}).get('reason') for t in workflow.get('trace', [])
             if t.get('event_type') == 'investigation_loop_exit_scheduled']
    reason = exits[-1] if exits else None
    verdicts = workflow.get('verifications', [])
    confirmed = bool(verdicts) and verdicts[-1].get('can_answer') == 'yes'
    abstained = result.get('submitted_answer', '').strip() == '无法确认答案：调查结束，尚无充分证据。'
    return {'termination_reason': reason or ('error' if result['status'] == 'failed' else 'unknown'),
            'budget_exhausted': reason == 'query_budget_exhausted',
            'abstained': abstained,
            'answered': result['status'] == 'completed' and confirmed and not abstained}


def confirmation_statistics(cases, manifest):
    expected = manifest['cases']
    lookup = {(c['case_id'], c['mode']): c for c in cases}
    pending = [(case, mode) for case in expected for mode in ('pm', 'baseline') if (case, mode) not in lookup]
    unresolved = [c for c in cases if c['audit_state'] in ('missing', 'stale', 'error', 'disputed')]
    complete = not pending
    groups = {}
    for mode in ('pm', 'baseline'):
        rows = [lookup[c, mode] for c in expected if (c, mode) in lookup]
        correct = sum(c['accepted_correct'] for c in rows)
        tokens = sum(c['investigation_tokens'] for c in rows)
        sql_errors = sum(c['failed_queries'] + c['local_sql_errors'] + c.get('terminal_sql_rejections', 0) for c in rows)
        sql_candidates = sum(c['queries'] + c['local_sql_errors'] + c.get('terminal_sql_rejections', 0) for c in rows)
        groups[mode] = {'expected': len(expected), 'finished': len(rows), 'accepted_correct': correct,
                        'completion_rate': sum(c['status'] == 'completed' for c in rows)/len(expected),
                        'answered_rate': sum(c.get('answered', False) for c in rows)/len(expected),
                        'abstentions': sum(c.get('abstained', False) for c in rows),
                        'budget_exhaustions': sum(c.get('budget_exhausted', False) for c in rows),
                        'accepted_rate': correct/len(expected),
                        'accuracy_wilson95': wilson(correct, len(expected)) if complete else None,
                        'total_tokens_including_failures': tokens,
                        'mean_tokens': tokens/len(expected) if complete else None,
                        'tokens_per_accepted_answer': tokens/correct if correct else None,
                        'invalid_sql_rate': sql_errors/sql_candidates if sql_candidates else None}
    pairs = [(lookup[c, 'pm'], lookup[c, 'baseline']) for c in expected
             if (c, 'pm') in lookup and (c, 'baseline') in lookup]
    ratio = None
    difference_ci = None
    if complete and groups['baseline']['total_tokens_including_failures']:
        ratio = groups['pm']['total_tokens_including_failures']/groups['baseline']['total_tokens_including_failures']
    if complete and pairs:
        blocks = {}
        for case in expected:
            blocks.setdefault(case.rsplit('-q', 1)[0], []).append(
                int(lookup[case, 'pm']['accepted_correct'])-int(lookup[case, 'baseline']['accepted_correct']))
        blocks = list(blocks.values())
        rng = random.Random(0)
        samples = []
        for _ in range(4000):
            selected = rng.choices(blocks, k=len(blocks))
            samples.append(sum(sum(b) for b in selected)/sum(len(b) for b in selected))
        samples.sort()
        difference_ci = [samples[99], samples[3899]]
    per_incident = {}
    for incident in sorted({c.rsplit('-q', 1)[0] for c in expected}):
        ids = [c for c in expected if c.rsplit('-q', 1)[0] == incident]
        per_incident[incident] = {mode: {'expected': len(ids),
            'finished': sum((c, mode) in lookup for c in ids),
            'accepted_correct': sum(lookup[c, mode]['accepted_correct'] for c in ids if (c, mode) in lookup)}
            for mode in ('pm', 'baseline')}
    ready = complete and not unresolved and len(expected) >= 40
    pm = groups['pm']
    return {'status': 'pending_results' if pending else 'pending_audit' if unresolved else 'metrics_ready',
            'pending_pairs': pending, 'unresolved_audits': len(unresolved), 'groups': groups,
            'pm_to_baseline_token_ratio': ratio, 'paired_incident_cluster_accuracy_difference_bootstrap95': difference_ci,
            'per_incident': per_incident,
            'metric_gates': {'accuracy': pm['accepted_rate'] >= .8 if ready else None,
                             'completion': pm['completion_rate'] >= .95 if ready else None,
                             'invalid_sql': pm['invalid_sql_rate'] <= .05 if ready and pm['invalid_sql_rate'] is not None else None,
                             'tokens': ratio <= 1.1 if ready and ratio is not None else None},
            'scope': 'Completion means a clean program exit, including abstentions; answered_rate separately requires a final affirmative Verifier verdict. Missing and disputed results never count as correct. Interim rates use the full frozen denominator. CIs describe this set; grounding and generalization still require separate evidence. No automatic excellence declaration.'}


def summarize(run):
    results = json.loads((run / 'results.json').read_text()) if (run / 'results.json').exists() else []
    cases = []
    for result in results:
        mode, case_id = result['mode'], result['case_id']
        audit_path = run / mode / f'{case_id}-audit.json'
        source = run / mode / f'{case_id}.json'
        audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
        audit_current = bool(audit) and audit.get('source_sha256') == hashlib.sha256(source.read_bytes()).hexdigest()
        accepted = audit_current and audit.get('reference_correct') is True and audit.get('evidence_supported') is True and audit.get('needs_human_review') is False and not audit.get('audit_error')
        queries = [q for q in result['process'] if q.get('action_type') == 'query']
        repeats = Counter(q['action'].strip() for q in queries)
        usage = result.get('usage', {})
        traces = result.get('workflow', {}).get('trace', [])
        terminal_sql_error = result.get('error', '').startswith(('ValueError: SQL', 'SQLSyntaxError:'))
        traced_terminal = any(t.get('event_type') == 'executor_sql_invalid' and t.get('payload', {}).get('error') == result.get('error') for t in traces)
        cases.append({'case_id': case_id, 'mode': mode, 'status': result['status'],
            **termination_details(result),
            'exact_match': result['evaluation']['exact_match'], 'accepted_correct': accepted,
            'audit_state': 'missing' if not audit else 'stale' if not audit_current else 'error' if audit.get('audit_error') else 'disputed' if audit.get('needs_human_review') else 'adjudicated',
            'queries': len(queries), 'failed_queries': sum(q.get('query_success') is False for q in queries),
            'repeated_identical_queries': sum(n-1 for n in repeats.values()),
            'format_errors': sum(t.get('event_type') == 'role_yaml_invalid' for t in traces),
            'local_sql_errors': sum(t.get('event_type') == 'executor_sql_invalid' for t in traces),
            'terminal_sql_rejections': int(terminal_sql_error and not traced_terminal),
            'transport_normalizations': sum(t.get('event_type') == 'role_transport_normalized' for t in traces),
            'investigation_tokens': usage.get('input_tokens', 0)+usage.get('output_tokens', 0),
            'judge_tokens': sum(audit.get('judge_usage', {}).get(k, 0) for k in ('input_tokens', 'output_tokens')),
            'error': result['error']})
    groups = {}
    for mode in sorted({c['mode'] for c in cases}):
        group = [c for c in cases if c['mode'] == mode]
        groups[mode] = {'cases': len(group), 'completed': sum(c['status'] == 'completed' for c in group),
            'answered': sum(c['answered'] for c in group),
            'abstentions': sum(c['abstained'] for c in group),
            'budget_exhaustions': sum(c['budget_exhausted'] for c in group),
            'exact_matches': sum(c['exact_match'] for c in group),
            'accepted_correct': sum(c['accepted_correct'] for c in group),
            'audits': dict(Counter(c['audit_state'] for c in group)),
            **{k: sum(c[k] for c in group) for k in ('queries','failed_queries','local_sql_errors','terminal_sql_rejections','transport_normalizations','repeated_identical_queries','format_errors','investigation_tokens','judge_tokens')}}
    manifest = json.loads((run/'manifest.json').read_text())
    report = {'run': str(run), 'purpose': manifest.get('purpose', 'development'),
              'scope': 'Do not pool different versions as paired effectiveness. Missing/error/disputed audits are not accepted successes. API-reported usage excludes unreported hidden transport retries.', 'groups': groups, 'cases': cases}
    if manifest.get('purpose') == 'confirmation':
        report['confirmation'] = confirmation_statistics(cases, manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    args = parser.parse_args()
    for run in args.runs:
        report = summarize(run)
        (run/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'run':str(run),'groups':report['groups']},ensure_ascii=False))

if __name__ == '__main__':
    main()
