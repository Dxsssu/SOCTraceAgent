from types import SimpleNamespace
from scripts import score_procedural_workflow as scoring


def test_metrics_do_not_turn_judge_errors_into_wrong_answers():
    rows = [dict(evaluation_status='scored', llm_evaluation={'llm_judge_answer_correct': True}, paper_evaluation={'reward':1}),
            dict(evaluation_status='scored', llm_evaluation={'llm_judge_answer_correct': False}, paper_evaluation={'reward':.4}),
            dict(evaluation_status='investigation_error')]
    report = scoring.metrics(rows, 3)
    assert report['scored_subset_acc'] == .5
    assert report['scored_subset_mean_reward'] == .7
    assert report['full_acc_investigation_failures_zero'] == 1/3
    rows.append(dict(evaluation_status='judge_error'))
    report = scoring.metrics(rows, 4)
    assert report['full_acc_investigation_failures_zero'] is None
    assert not report['scoring_complete']


def test_scoring_retries_keep_attempts(tmp_path, monkeypatch):
    calls = []
    def fake(case, result, config):
        calls.append(1)
        if len(calls) == 1:
            result.update(evaluation_status='judge_error', judge_error='429')
        else:
            result.pop('judge_error',None)
            result.update(evaluation_status='scored', paper_evaluation={'reward':0})
    monkeypatch.setattr(scoring, 'score_case', fake)
    monkeypatch.setattr(scoring.time, 'sleep', lambda _: None)
    result = scoring.score_one({'case_id':'x','status':'completed','submitted_answer':'a'}, None, tmp_path, None)
    assert len(calls) == 2
    assert result['attempts'][0]['error'] == '429'
    assert result['evaluation_status'] == 'scored'


def test_changed_answer_is_not_reused():
    assert not scoring.reusable({'case_id':'x', 'answer':'old', 'evaluation_status':'scored'},
                                {'case_id':'x', 'submitted_answer':'new'}, SimpleNamespace(question={}))
