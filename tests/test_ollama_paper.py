import httpx
import pytest

from src.benchmarks.external_agents.ollama_paper import (
    no_think_messages, ollama_call, summarize, validate_judgement,
)


def test_ollama_disables_thinking_and_preserves_history():
    messages = [{'role': 'system', 'content': 'paper prompt'},
                {'role': 'user', 'content': 'question'},
                {'role': 'assistant', 'content': 'Action: execute[SHOW TABLES]'},
                {'role': 'user', 'content': 'SQL observation'}]
    def respond(request):
        import json
        body = json.loads(request.content)
        assert request.url.path == '/api/chat'
        assert body['think'] is False
        assert body['messages'][-1]['content'] == 'SQL observation\n/no_think'
        assert body['messages'][:-1] == messages[:-1]
        assert body['options']['num_ctx'] == 32768
        return httpx.Response(200, json={'message': {'content': 'Action: submit[answer]'},
                                        'prompt_eval_count': 12, 'eval_count': 3})
    with httpx.Client(base_url='http://ollama.test', transport=httpx.MockTransport(respond)) as client:
        content, usage, _ = ollama_call(client, 'qwen3:32b', messages, 32768)
    assert content == 'Action: submit[answer]'
    assert usage['total_tokens'] == 15
    assert messages[-1]['content'] == 'SQL observation'
    assert no_think_messages([{'role': 'system', 'content': 'x'}])[-1]['content'] == '/no_think'


@pytest.mark.parametrize('body', [
    {'message': {'content': 'answer', 'thinking': 'hidden reasoning'}},
    {'message': {'content': ''}},
    {'message': {'content': 'answer'}, 'done_reason': 'length'},
])
def test_invalid_model_output_is_not_silently_scored(body):
    with httpx.Client(base_url='http://ollama.test', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(RuntimeError):
            ollama_call(client, 'qwen3:32b', [{'role': 'user', 'content': 'question'}], 32768)


def test_partial_paper_reward_and_judge_failure():
    evaluation = {'check_ans_reflection': 'Is_Answer_Correct: False',
                  'is_json_success': True, 'is_reflect_success': True,
                  'check_sol_reflection': {
                      '0': {'is_step_correct': 'True'},
                      '1': {'is_step_correct': 'True'},
                      '2': {'is_step_correct': 'False'}}, 'reward': 0.56}
    validate_judgement(evaluation)
    with pytest.raises(ValueError):
        validate_judgement({**evaluation, 'reward': 0})
    with pytest.raises(ValueError):
        validate_judgement({**evaluation, 'is_reflect_success': False})


def test_infrastructure_failure_does_not_become_zero_reward(tmp_path):
    results = [{'case_id': 'a', 'status': 'submitted', 'paper_evaluation': {'reward': 1}},
               {'case_id': 'b', 'status': 'error'}]
    report = summarize(results, tmp_path)
    assert report['mean_reward'] is None
    assert report['total_reward'] is None
    assert report['scored_cases'] == 1
    results[1] = {'case_id': 'b', 'status': 'no_submission', 'paper_evaluation': {'reward': 0}}
    report = summarize(results, tmp_path)
    assert report['mean_reward'] == 0.5


def test_investigate_resets_actual_upstream_agent_before_first_request(tmp_path, monkeypatch):
    """Exercise real upstream reset and act (including its Qwen3 format fallback)."""
    from types import SimpleNamespace
    import sys
    from src.benchmarks.external_agents import ollama_paper as runner
    Baseline = runner.baseline_class()
    original = sys.modules[Baseline.__module__]
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'test')

    class Database:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, sql):
            assert sql == 'SHOW TABLES;'
            return "[('SecurityAlert',)]", True, 1

        def close(self):
            pass

    monkeypatch.setattr(runner, 'MySQLQueryExecutor', Database)
    calls = []
    replies = iter(['Thought: inspect\nAction: execute[SHOW TABLES]',
                    'execute[SHOW TABLES]',
                    'Thought: done\nAction: submit[test answer]',
                    'submit[test answer]'])

    def fake_call(client, model, messages, context_size):
        assert messages[0]['content'] == original.O1_PROMPT
        assert messages[0]['content'] != original.R1_PROMPT
        calls.append(messages)
        return next(replies), {'total_tokens': 1}, {'messages': runner.no_think_messages(messages)}

    monkeypatch.setattr(runner, 'ollama_call', fake_call)
    case = SimpleNamespace(case_id='test-case', incident='incident_5',
                           question={'context': 'context', 'question': 'question', 'answer': 'test answer'},
                           manifest_view=lambda: {'case_id': 'test-case'})
    args = SimpleNamespace(output=tmp_path, base_url='http://ollama.invalid', model='qwen3:32b',
                           request_timeout=1, case_timeout=60, num_ctx=32768, max_steps=25)
    result = runner.investigate(case, args, Baseline, lambda stage: None)
    assert result['status'] == 'submitted', result.get('error')
    assert result['answer'] == 'test answer'
    assert result['system_prompt'] == original.O1_PROMPT
    assert result['initialization'] == 'constructor_then_reset_v1'
    assert len(result['actions']) == 2
    assert len(calls) == 4  # Official Qwen3 parser's format-repair calls are retained.
