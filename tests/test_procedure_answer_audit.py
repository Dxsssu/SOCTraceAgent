from scripts.audit_procedure_card_answers import deterministic_nonanswer


def test_program_abstention_is_not_a_completed_answer_for_judging():
    result = deterministic_nonanswer({'status': 'completed',
        'submitted_answer': '无法确认答案：调查结束，尚无充分证据。'})
    assert result['reference_correct'] is False
    assert result['evidence_supported'] is False
    assert result['judge_usage'] == {}
    assert deterministic_nonanswer({'status': 'completed', 'submitted_answer': 'host'}) is None
    assert deterministic_nonanswer({'status': 'failed', 'submitted_answer': 'host'})['reference_correct'] is False
