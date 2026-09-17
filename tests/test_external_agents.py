import json
from types import SimpleNamespace
from pathlib import Path
from src.agent.llm import LLMConfig
from src.benchmarks.external_agents.bridge import Bridge
from src.benchmarks.external_agents.runner import prepare, AGENTS


class Executor:
    def __init__(self):
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        return '[(1,)]', True, 1


def test_shared_budget_rejects_writes_and_stops_after_submission():
    db = Executor()
    bridge = Bridge(db, LLMConfig('secret', 'https://example.invalid/v1', 'test'), max_steps=3)
    client = bridge.app.test_client()
    headers = {'Authorization': 'Bearer '+bridge.token}
    assert client.post('/execute_sql', json={'sql': 'SELECT 1'}).status_code == 401
    assert not client.post('/execute_sql', headers=headers, json={'sql': 'DROP TABLE a'}).json['success']
    assert client.post('/execute_sql', headers=headers, json={'sql': 'SELECT 1'}).json['success']
    assert 'budget' in client.post('/execute_sql', headers=headers, json={'sql': 'SELECT 2'}).json['error']
    assert db.queries == ['SELECT 1;']
    assert client.post('/submit_answer', headers=headers, json={'answer': '1'}).json['submitted']
    assert 'error' in client.post('/submit_answer', headers=headers, json={'answer': '2'}).json
    assert 'error' in client.post('/execute_sql', headers=headers, json={'sql': 'SELECT 3'}).json
    assert bridge.answer == '1'
    assert len(bridge.actions) == 3


def test_agent_config_does_not_contain_real_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('PARATERA_API_KEY', 'real-secret')
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'db-secret')
    bridge = SimpleNamespace(config=LLMConfig('real-secret', 'https://private/v1', 'test-model'),
                             url='http://127.0.0.1:1234', token='local-token')
    for agent in AGENTS:
        work = tmp_path/agent
        work.mkdir()
        cmd, env = prepare(agent, work, bridge, 'question without ground truth', 60)
        assert Path(cmd[0]).is_absolute()
        assert 'PARATERA_API_KEY' not in env
        assert 'MYSQL_ROOT_PASSWORD' not in env
        config = '\n'.join(p.read_text() for p in work.rglob('*.json'))
        assert 'real-secret' not in config
        assert 'db-secret' not in config
        assert 'execute_sql' in config or 'excytin' in config
        if agent == 'cline':
            providers = json.loads((work/'cline-data/settings/providers.json').read_text())
            entry = providers['providers']['openai-compatible']
            assert entry['updatedAt'].endswith('Z')
            assert entry['settings']['baseUrl'] == bridge.url+'/v1'


def test_proxy_forces_model_and_preserves_tool_calls(monkeypatch):
    bridge = Bridge(Executor(), LLMConfig('secret', 'https://private/v1', 'target'))
    captured = {}
    result = {'id': 'c1', 'created': 1, 'model': 'target', 'choices': [{
        'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'excytin_execute_sql', 'arguments': '{"sql":"SELECT 1"}'}}]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            captured.update(kwargs['json'])
            return SimpleNamespace(status_code=200, json=lambda: result)
    monkeypatch.setattr('src.benchmarks.external_agents.bridge.httpx.Client', Client)
    response = bridge.app.test_client().post('/v1/chat/completions',
        headers={'Authorization': 'Bearer '+bridge.token}, json={
        'model': 'other', 'stream': True, 'messages': [], 'tools': [
            {'type': 'function', 'function': {'name': 'shell'}},
            {'type': 'function', 'function': {'name': 'excytin_execute_sql'}}]})
    assert captured['model'] == 'target'
    assert len(captured['tools']) == 1
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
    assert events[0]['choices'][0]['delta']['tool_calls'][0]['index'] == 0
    assert events[-1]['usage']['total_tokens'] == 15
    assert response.text.endswith('data: [DONE]\n\n')


def test_paired_sampling_uses_test_questions_without_replacement():
    from src.benchmarks.external_agents.batch import select_cases
    from src.benchmarks.excytin_bench.runner import BenchmarkCase
    population = [BenchmarkCase(incident, i, {'question': str(i)})
                  for incident, count in [('incident_5', 30), ('incident_38', 11)]
                  for i in range(count)]
    chosen = select_cases(population)
    assert len(chosen) == 26
    assert len({c.case_id for c in chosen}) == 26
    assert chosen == select_cases(population)
    assert sum(c.incident == 'incident_38' for c in chosen) == 11


def test_analysis_separates_schema_queries_and_cached_cost():
    from src.benchmarks.external_agents.analyze import stats
    row = {'status': 'submitted', 'answer': 'x', 'elapsed_seconds': 1,
           'llm_evaluation': {'llm_judge_answer_correct': True, 'llm_judge_reward': 1},
           'actions': [
               {'type': 'execute_sql', 'sql': 'SHOW TABLES', 'success': True, 'row_count': 3},
               {'type': 'execute_sql', 'sql': 'DESCRIBE DeviceInfo', 'success': True, 'row_count': 4},
               {'type': 'execute_sql', 'sql': 'SELECT * FROM DeviceInfo', 'success': True, 'row_count': 1}],
           'usage': [{'prompt_tokens': 1000, 'completion_tokens': 100,
                      'prompt_tokens_details': {'cached_tokens': 500}}]}
    result = stats([row])
    assert result['mean_schema_queries'] == 2
    assert result['mean_data_queries'] == 1
    assert abs(result['total_cost_cny'] - 0.00182) < 1e-10
    assert result['accuracy'] == 1
