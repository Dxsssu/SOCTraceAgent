"""Regression checks for paper action, judging, and reporting semantics."""
import json
from types import SimpleNamespace

import pytest

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import BenchmarkCase
from src.benchmarks.external_agents.bridge import Bridge
from src.benchmarks.external_agents import evaluation


CONFIG = LLMConfig('test-secret', 'https://example.invalid/v1', 'DeepSeek-V4.1-Flash')


@pytest.mark.parametrize('force_thinking', [True, False])
def test_glm_reasoning_parameters_reach_provider(monkeypatch, force_thinking):
    config = LLMConfig('test-secret', 'https://example.invalid/v1', 'GLM-5.3-Flash',
                       reasoning_effort='low')
    captured = {}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            captured.update(kwargs['json'])
            return SimpleNamespace(status_code=200, json=lambda: {
                'choices': [{'message': {'content': 'ok'}}], 'usage': {}})
    monkeypatch.setattr('src.benchmarks.external_agents.bridge.httpx.Client', Client)
    bridge = Bridge(None, config, force_thinking=force_thinking)
    response = bridge.app.test_client().post('/v1/chat/completions',
        headers={'Authorization': 'Bearer '+bridge.token},
        json={'messages': [], 'thinking': {'type': 'disabled'}})
    assert response.status_code == 200
    assert captured['thinking'] == {'type': 'enabled'}
    assert captured['reasoning_effort'] == 'low'


class Database:
    def __init__(self, *args, **kwargs):
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        return '[]', True, 0

    def close(self):
        pass


@pytest.mark.parametrize('last_action', ['query', 'submit'])
def test_last_action_is_executed_and_then_terminal(last_action):
    db = Database()
    bridge = Bridge(db, CONFIG, max_steps=3)
    client = bridge.app.test_client()
    headers = {'Authorization': 'Bearer ' + bridge.token}
    for sql in ['SELECT 1', 'DROP TABLE logs']:
        client.post('/execute_sql', headers=headers, json={'sql': sql})
    assert len(bridge.actions) == 2
    assert not bridge.finished.is_set()
    if last_action == 'query':
        client.post('/execute_sql', headers=headers, json={'sql': 'SELECT 2'})
        assert bridge.terminal_reason == 'budget_exhausted'
        assert db.queries == ['SELECT 1;', 'SELECT 2;']
    else:
        client.post('/submit_answer', headers=headers, json={'answer': 'test'})
        assert bridge.terminal_reason == 'submitted'
    ended = bridge.ended_at
    assert bridge.finished.is_set()
    for route, body in [('/execute_sql', {'sql': 'SELECT 3'}),
                        ('/submit_answer', {'answer': 'late'}),
                        ('/v1/chat/completions', {'messages': []})]:
        assert client.post(route, headers=headers, json=body).status_code == 409
    assert len(bridge.actions) == 3
    assert bridge.calls == 0
    assert bridge.ended_at == ended


def test_full_official_judging_flow_and_partial_reward(monkeypatch):
    if not (evaluation.UPSTREAM/'evaluator.py').exists():
        pytest.skip('Local official ExCyTIn source required')
    # The initial verdict is corrected by reflection. Step reflection then
    # overrides initial step judgments. Only the reflected steps earn reward.
    replies = iter([
        'Analysis: preliminary\nIs_Answer_Correct: True',
        'Analysis: corrected\nIs_Answer_Correct: False',
        json.dumps({str(i): {'is_step_correct': 'False'} for i in range(3)}),
        json.dumps({'0': {'is_step_correct': 'True'},
                    '1': {'is_step_correct': 'True'},
                    '2': {'is_step_correct': 'False'}}),
    ])
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))])

    monkeypatch.setattr(evaluation, 'OpenAI', Client)
    bridge = SimpleNamespace(token='local', url='http://unused', config=CONFIG)
    question = {'context': 'context', 'question': 'question', 'answer': 'gold',
                'solution': ['first', 'second', 'final']}
    result = evaluation.paper_evaluator(bridge).checking(question, 'partial answer')
    evaluation.validate_judgement(result, question)
    assert result['reward'] == pytest.approx(0.56)
    assert len(calls) == 4
    assert all(c['model'] == CONFIG.model for c in calls)
    assert all(c['response_format'] == {'type': 'json_object'} for c in calls[2:])
    assert 'Is_Answer_Correct: True' in calls[1]['messages'][-1]['content']
    assert 'is_step_correct' in calls[3]['messages'][-1]['content']
    with pytest.raises(ValueError, match='incomplete'):
        evaluation.validate_judgement(result, {**question, 'solution': ['one', 'two']})


def test_judge_failure_is_unscored_and_retry_preserves_usage(monkeypatch):
    case = BenchmarkCase('incident_5', 0, {'question': 'q', 'answer': 'gold', 'solution': ['gold']})
    result = {'status': 'submitted', 'answer': 'answer', 'judge_usage': [{'total_tokens': 9}]}

    class FakeBridge:
        def __init__(self, *args, **kwargs):
            assert kwargs['force_thinking'] is False
            self.usage = [{'total_tokens': 4}]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(evaluation, 'Bridge', FakeBridge)
    monkeypatch.setattr(evaluation, 'paper_evaluator', lambda _: SimpleNamespace(
        checking=lambda *args: {'reward': 0}))
    evaluation.score_case(case, result, CONFIG)
    assert result['evaluation_status'] == 'judge_error'
    assert 'paper_evaluation' not in result and 'llm_evaluation' not in result
    assert len(result['judge_usage']) == 2
    monkeypatch.setattr(evaluation, 'paper_evaluator', lambda _: SimpleNamespace(
        checking=lambda *args: {'reward': 1, 'check_ans_reflection': 'Is_Answer_Correct: True'}))
    evaluation.score_case(case, result, CONFIG)
    assert result['evaluation_status'] == 'scored'
    assert result['paper_evaluation']['reward'] == 1
    assert 'judge_error' not in result
    assert len(result['judge_usage']) == 3


@pytest.mark.parametrize('status,scored', [('budget_exhausted', True), ('no_submission', True),
                                         ('timeout', False), ('error', False), ('interrupted', False)])
def test_only_normal_non_submission_is_zero(status, scored):
    result = {'status': status, 'answer': ''}
    evaluation.score_case(None, result, CONFIG)
    assert ('paper_evaluation' in result) == scored
    if scored:
        assert result['paper_evaluation']['reward'] == 0


def test_missing_or_failed_judgements_do_not_produce_complete_scores():
    from src.benchmarks.external_agents.analyze import stats
    good = {'status': 'submitted', 'llm_evaluation': {
        'llm_judge_reward': 1, 'llm_judge_answer_correct': True}}
    for rows in [[good], [good, {'status': 'submitted', 'judge_error': 'bad response'}]]:
        summary = stats(rows, expected=2)
        assert summary['accuracy'] is None and summary['mean_reward'] is None
        assert summary['scored_subset_mean_reward'] == 1
        assert not summary['scoring_complete']
    assert stats([good], expected=1)['mean_reward'] == 1


@pytest.mark.parametrize('submit_last', [True, False])
def test_paper_baseline_reset_and_same_action_boundary(tmp_path, monkeypatch, submit_last):
    from src.benchmarks.external_agents import batch
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'fake')
    monkeypatch.setattr(batch, 'MySQLQueryExecutor', Database)
    monkeypatch.setattr(batch.LLMConfig, 'from_env', lambda: CONFIG)

    class Baseline:
        def __init__(self, **kwargs):
            self.was_reset = False
            self.calls = 0

        def reset(self):
            self.was_reset = True

        def act(self, observation):
            assert self.was_reset
            self.calls += 1
            if self.calls == 3 and submit_last:
                return 'answer', True
            return 'SELECT 1', False

        def get_logging(self):
            return {'calls': self.calls}

    case = BenchmarkCase('incident_5', 0, {'context': 'c', 'question': 'q', 'answer': 'answer'})
    result = batch.run_internal('paper_baseline', case, tmp_path, Baseline, max_steps=3)
    assert len(result['actions']) == result['workflow']['calls'] == 3
    assert result['status'] == ('submitted' if submit_last else 'budget_exhausted')
    assert result['actions'][-1]['type'] == ('submit_answer' if submit_last else 'execute_sql')


def test_judge_defaults_to_investigation_config_and_allows_explicit_override(monkeypatch):
    monkeypatch.setattr(evaluation.LLMConfig, 'from_env', lambda: CONFIG)
    for name in ('MODEL', 'API_KEY', 'BASE_URL', 'TIMEOUT'):
        monkeypatch.delenv('EXCYTIN_JUDGE_' + name, raising=False)
    assert evaluation.judge_config() == CONFIG
    changed = LLMConfig('changed-key', 'https://changed.invalid/v1', 'another-deepseek')
    monkeypatch.setattr(evaluation.LLMConfig, 'from_env', lambda: changed)
    assert evaluation.judge_config() == changed
    monkeypatch.setenv('EXCYTIN_JUDGE_API_KEY', 'judge-secret')
    monkeypatch.setenv('EXCYTIN_JUDGE_BASE_URL', 'https://judge.invalid/v1')
    config = evaluation.judge_config()
    assert config.model == changed.model
    assert config.api_key == 'judge-secret'
    assert config.base_url == 'https://judge.invalid/v1'
    assert evaluation.judge_config('custom-judge').model == 'custom-judge'
    monkeypatch.setenv('EXCYTIN_JUDGE_MODEL', 'explicit-env-model')
    assert evaluation.judge_config().model == 'explicit-env-model'
    assert evaluation.judge_config('custom-judge').model == 'custom-judge'


def test_resume_rejects_legacy_and_changed_protocol(tmp_path, monkeypatch):
    from src.benchmarks.external_agents import batch
    case = BenchmarkCase('incident_5', 0, {'question': 'q'})
    monkeypatch.setattr(batch, 'load_test_cases', lambda: [case])
    monkeypatch.setattr(batch, 'judge_config', lambda model=None: CONFIG)
    monkeypatch.setattr(batch.LLMConfig, 'from_env', lambda: CONFIG)
    monkeypatch.setattr(batch, 'load_dotenv', lambda *args: None)
    manifest = {'agents': ['pi'], 'model': CONFIG.model, 'cases': [case.manifest_view()]}
    batch.write_json(tmp_path/'manifest.json', manifest)
    with pytest.raises(ValueError, match='Legacy'):
        batch.main(['--output', str(tmp_path)])
    manifest['protocol'] = evaluation.protocol_manifest([case], max_steps=15, judge=CONFIG)
    batch.write_json(tmp_path/'manifest.json', manifest)
    with pytest.raises(ValueError, match='settings/source/questions changed'):
        batch.main(['--output', str(tmp_path)])


def test_sql_observations_match_official_environment():
    """Execute the actual upstream step method with a fake cursor, no MySQL."""
    import ast
    from src.benchmarks.external_agents.environment import PaperQueryExecutor
    source = evaluation.UPSTREAM/'excytin_env.py'
    if not source.exists():
        pytest.skip('Local official ExCyTIn source required')
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ExcytinEnv')
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'step']
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)

    class Cursor:
        def execute(self, sql):
            if sql == 'bad':
                raise ValueError('database error')

        def fetchall(self):
            return [('one',), ('two',), ('three',)]

    official = namespace['ExcytinEnv']()
    official.curr_question = {'question': 'q'}
    official.cursor = Cursor()
    official.step_count, official.max_steps = 0, 25
    official.max_entry_return, official.max_str_len = 2, 5
    official.curr_trajectory = []
    local = PaperQueryExecutor.__new__(PaperQueryExecutor)
    local.cursor = Cursor()
    local.max_entry_return, local.max_str_len = 2, 5
    for sql in ['SELECT value FROM logs', 'bad']:
        expected, _, _, info = official.step(sql)
        observed, success, _ = local.execute(sql)
        assert observed == expected
        assert success == info['query_success']


def test_native_runner_stops_at_submission_without_waiting_for_cli_exit(tmp_path, monkeypatch):
    from src.benchmarks.external_agents import runner
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'fake')
    monkeypatch.setattr(runner.LLMConfig, 'from_env', lambda: CONFIG)
    monkeypatch.setattr(runner, 'MySQLQueryExecutor', Database)
    captured = {}

    def prepare(agent, work, bridge, prompt, timeout):
        captured['bridge'] = bridge
        return ['dummy'], {}

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            bridge = captured['bridge']
            client = bridge.app.test_client()
            client.post('/submit_answer', headers={'Authorization': 'Bearer '+bridge.token},
                        json={'answer': 'answer'})

        def poll(self):
            pytest.fail('An already submitted episode must not wait for the CLI')

    def stop(process):
        captured['stopped'] = True
        process.returncode = -15

    monkeypatch.setattr(runner, 'prepare', prepare)
    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    monkeypatch.setattr(runner, 'stop_process', stop)
    case = BenchmarkCase('incident_5', 0, {'question': 'q', 'answer': 'answer'})
    result = runner.run_case('pi', case, tmp_path, llm_eval=False)
    assert result['status'] == 'submitted'
    assert captured['stopped']
    assert result['process_elapsed_seconds'] >= result['elapsed_seconds']
    assert len(result['actions']) == 1


def test_post_terminal_usage_is_separate():
    bridge = Bridge(Database(), CONFIG)
    bridge.record_usage({'total_tokens': 10})
    bridge.finish('submitted')
    bridge.record_usage({'total_tokens': 20})
    assert bridge.usage == [{'total_tokens': 10}]
    assert bridge.post_terminal_usage == [{'total_tokens': 20}]


def test_paper_agent_prints_are_quiet_but_actions_and_history_remain(capsys):
    from src.benchmarks.external_agents.batch import baseline_class, UPSTREAM
    if not (UPSTREAM/'secgym/agents/baseline_agent.py').exists():
        pytest.skip('Local official source required')
    pytest.importorskip('autogen')
    pytest.importorskip('azure.ai.inference')
    Baseline = baseline_class()
    agent = Baseline.__new__(Baseline)
    agent.config_list = [{'model': 'DeepSeek-V4.1-Flash'}]
    agent.messages = []
    agent.step_count = 0
    agent.max_steps = 25
    agent.submit_summary = False
    agent.totoal_usage = {}
    replies = iter(['Thought: inspect\nAction: execute[SELECT 1;]',
                    'Thought: answer', 'submit[1]'])
    agent._call_llm = lambda messages: next(replies)
    query, submitted = agent.act('question')
    assert 'SELECT 1' in query and not submitted
    answer, submitted = agent.act('[(1,)]')
    assert answer == '1' and submitted
    assert 'SELECT 1' in str(agent.get_logging())
    assert capsys.readouterr().out == ''
    print('progress remains visible')
    assert 'progress remains visible' in capsys.readouterr().out
