import json
from types import SimpleNamespace
from scripts import run_ltm_wm_batch as batch


def test_resume_preserves_completed_wrong_and_unanswered():
    assert batch.should_run(None)
    assert batch.should_run({'status': 'running'})
    assert batch.should_run({'status': 'failed'})
    wrong = {'status': 'completed', 'answered': True, 'exact_match': False}
    unknown = dict(wrong, answered=False)
    assert not batch.should_run(wrong)
    assert not batch.should_run(unknown)
    assert batch.should_run(wrong, retry_incorrect=True)
    assert not batch.should_run(wrong, retry_unanswered=True)
    assert batch.should_run(unknown, retry_unanswered=True)


def test_independent_attempts_and_usage(tmp_path, monkeypatch):
    clients = []
    class Client:
        def __init__(self, config):
            clients.append(self)
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=self), close=lambda: None)
        def create(self, **kwargs):
            assert 'max_tokens' not in kwargs
            return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {
                'prompt_tokens': 20, 'completion_tokens': 3, 'total_tokens': 23}),
                choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='can_answer: "no"'))])
    class Runner:
        def __init__(self, **kwargs): self.workflow = kwargs['workflow_factory'](12)
        def run_case(self, case, **kwargs):
            assert kwargs['split'] == 'test'
            self.workflow._llm('你是 Verifier', 'evidence')
            return dict(case_id=case.case_id, status='completed', query_count=1,
                workflow={'verifications':[{'can_answer':'no'}]},
                evaluation={'exact_match':False, 'ground_truth':'x'})
    monkeypatch.setattr(batch, 'LLMClient', Client)
    monkeypatch.setattr(batch, 'ExcytinBenchmarkRunner', Runner)
    monkeypatch.setattr(batch, 'audit', lambda w: {'passed': True})
    case = SimpleNamespace(case_id='incident_1-q0000')
    config = SimpleNamespace(model='fake')
    a = batch.run_one(case, tmp_path, config, 12)
    b = batch.run_one(case, tmp_path, config, 12)
    assert len(clients) == 2
    assert a['usage']['total_tokens'] == b['usage']['total_tokens'] == 23
    assert a['attempt_dir'] != b['attempt_dir']
    assert (tmp_path/a['attempt_dir']/'llm_calls.jsonl').exists()
    assert json.loads((tmp_path/'cases'/f'{case.case_id}.json').read_text())['attempt_dir'] == b['attempt_dir']
    summary = batch.report(tmp_path, {case.case_id:b}, 3)
    assert summary['processed'] == 1 and summary['pending'] == 2
    assert summary['total_tokens'] == 23  # latest attempt only
    assert summary['unanswered'] == 1
    assert json.loads((tmp_path/'failure_groups.json').read_text())['unanswered'] == [case.case_id]


def test_missing_usage_and_retries_are_not_zero_cost_claim():
    r = dict(case_id='a',status='failed',workflow={'trace':[
        {'event_type':'llm_api_retry_scheduled'}, {'event_type':'role_yaml_invalid'}]})
    s = batch.summarize(r, [{'role':'memory','error':'timeout'}])
    assert s['usage_missing_calls'] == 1
    assert s['api_retries'] == s['format_errors'] == 1
    assert s['roles']['memory']['failed_calls'] == 1
