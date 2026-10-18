from scripts.report_procedure_cards import confirmation_statistics, termination_details, wilson


def test_clean_budget_exit_is_not_an_answer_and_last_verdict_controls():
    result = {'status': 'completed', 'submitted_answer': '无法确认答案：调查结束，尚无充分证据。',
              'workflow': {'verifications': [{'can_answer': 'no'}], 'trace': [
                  {'event_type': 'investigation_loop_exit_scheduled',
                   'payload': {'reason': 'query_budget_exhausted'}}]}}
    details = termination_details(result)
    assert details['budget_exhausted'] and details['abstained']
    assert not details['answered']
    result['submitted_answer'] = 'host.example'
    result['workflow']['verifications'] = [{'can_answer': 'no'}, {'can_answer': 'yes'}]
    result['workflow']['trace'] = []
    assert termination_details(result)['answered']
    result['status'] = 'failed'
    assert not termination_details(result)['answered']
    assert not termination_details({'status': 'completed'})['answered']


def row(case, mode, **changes):
    return {'case_id': case, 'mode': mode, 'status': 'completed', 'audit_state': 'adjudicated',
            'accepted_correct': True, 'investigation_tokens': 100, 'queries': 2,
            'failed_queries': 0, 'local_sql_errors': 0, 'terminal_sql_rejections': 0} | changes


def manifest():
    return {'cases': [f'incident_{i//5}-q{i:04d}' for i in range(40)]}


def test_incomplete_confirmation_cannot_pass_on_completed_subset():
    frozen = manifest()
    result = confirmation_statistics([row(frozen['cases'][0], 'pm')], frozen)
    assert result['status'] == 'pending_results'
    assert result['groups']['pm']['accepted_rate'] == 1/40
    assert len(result['pending_pairs']) == 79
    assert all(v is None for v in result['metric_gates'].values())


def test_failed_work_is_included_in_cost_and_reliability_gates():
    frozen = manifest()
    rows = [row(c, m) for c in frozen['cases'] for m in ('pm', 'baseline')]
    rows[0] = row(frozen['cases'][0], 'pm', status='failed', accepted_correct=False,
                  investigation_tokens=10000, terminal_sql_rejections=1)
    result = confirmation_statistics(rows, frozen)
    assert result['groups']['pm']['total_tokens_including_failures'] == 13900
    assert result['groups']['pm']['tokens_per_accepted_answer'] == 13900/39
    assert result['pm_to_baseline_token_ratio'] == 13900/4000
    assert result['metric_gates']['tokens'] is False
    assert result['groups']['pm']['completion_rate'] == 39/40
    assert len(result['per_incident']) == 8


def test_disputed_audit_blocks_metric_pass_and_intervals_are_bounded():
    frozen = manifest()
    rows = [row(c, m) for c in frozen['cases'] for m in ('pm', 'baseline')]
    rows[0].update(audit_state='disputed', accepted_correct=False)
    result = confirmation_statistics(rows, frozen)
    assert result['status'] == 'pending_audit'
    assert all(v is None for v in result['metric_gates'].values())
    for count in (0, 20, 40):
        low, high = wilson(count, 40)
        assert 0 <= low <= count/40 <= high <= 1
