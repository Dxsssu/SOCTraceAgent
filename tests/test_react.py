"""ReAct shares SQL budgets, provider selection, scoring and resume policy."""
import json
from unittest.mock import MagicMock

import pytest

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import BenchmarkCase
from src.benchmarks.external_agents import batch, resume


def test_fancybag_react_runs_scores_and_resumes(tmp_path, monkeypatch):
    case = BenchmarkCase('incident_5', 0, {'question': 'Find alert'})
    monkeypatch.setattr(resume, 'load_dotenv', lambda *a: None)
    monkeypatch.setenv('SOCAGENT_LLM_PROVIDER', 'paratera')
    monkeypatch.setenv('PARATERA_MODEL', 'keep-original-model')
    monkeypatch.setenv('FANCYBAG_API_KEY', 'offline-test-key')
    monkeypatch.setenv('FANCYBAG_MODEL', 'previous-model')
    monkeypatch.setenv('FANCYBAG_BASE_URL', 'https://sub.fancybag.cn/v1')
    monkeypatch.setattr(resume, 'load_test_cases', lambda: [case])
    monkeypatch.setattr(resume, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(resume, 'AGENT_ROOT', tmp_path/'no-cli-installation')
    for name in ['react_agent.py', 'agent_utils.py']:
        p = tmp_path/'secgym/agents'/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('# source fixture')
    monkeypatch.setattr(batch, 'UPSTREAM', tmp_path)
    monkeypatch.setattr(batch, 'react_class', lambda: object)
    (tmp_path/'secgym/agents/react_examples').mkdir()
    (tmp_path/'secgym/agents/react_examples/one.txt').write_text('example')
    calls = []

    def run(agent, current_case, output, cls, **kwargs):
        config = LLMConfig.from_env()
        assert agent == 'react' and cls is object
        assert config.model == 'deepseek-v4.1-flash'
        assert config.base_url == 'https://sub.fancybag.cn/v1'
        assert config.api_key == 'offline-test-key'
        calls.append(current_case.case_id)
        row = dict(agent=agent, **current_case.manifest_view(), status='submitted',
                   answer='answer', actions=[], usage=[], elapsed_seconds=1)
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row

    def score(current_case, row, judge):
        assert judge.base_url == 'https://sub.fancybag.cn/v1'
        assert judge.model == 'deepseek-v4.1-flash'
        row.update(evaluation_status='scored', llm_evaluation={
            'llm_judge_answer_correct': True, 'llm_judge_reward': 1})

    monkeypatch.setattr(batch, 'run_internal', run)
    monkeypatch.setattr(resume, 'score_case', score)
    # Keep the real protocol builder; judge defaults must use Fancybag too.
    for key in ['EXCYTIN_JUDGE_API_KEY', 'EXCYTIN_JUDGE_BASE_URL', 'EXCYTIN_JUDGE_MODEL']:
        monkeypatch.delenv(key, raising=False)
    args = ['--agent', 'react', '--provider', 'fancybag',
            '--model', 'deepseek-v4.1-flash', '--workers', '1']
    assert resume.main(args) == 0
    assert resume.main(args) == 0
    assert calls == [case.case_id]
    root = tmp_path/'results/react/deepseek-v4.1-flash'
    manifest = json.loads((root/'manifest.json').read_text())
    assert 'no project SM/PM/EM' in manifest['memory']
    assert manifest['provider'] == 'fancybag'
    assert manifest['paper_agent_sources']
    assert 'offline-test-key' not in json.dumps(manifest)
    # Agent source changes must not silently mix with completed results.
    (tmp_path/'secgym/agents/react_agent.py').write_text('# changed')
    with pytest.raises(SystemExit):
        resume.main(args)


@pytest.mark.parametrize('polluted', [True, False])
@pytest.mark.parametrize('submit', [True, False])
def test_real_react_last_action_and_budget(tmp_path, monkeypatch, submit, polluted):
    pytest.importorskip('autogen')
    if not (batch.UPSTREAM/'secgym/agents/react_agent.py').exists():
        pytest.skip('Upstream ReAct source not installed')
    cls = batch.react_class()
    import sys
    module = sys.modules[cls.__module__]
    client = MagicMock()
    client.total_usage_summary = {}
    monkeypatch.setattr(module, 'OpenAIWrapper', lambda **kwargs: client)
    config = LLMConfig('test', 'https://example.invalid/v1', 'deepseek-v4.1-flash')
    monkeypatch.setattr(batch.LLMConfig, 'from_env', lambda: config)
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'offline')
    queries = []

    class Database:
        def __init__(self, *args, **kwargs): pass
        def execute(self, sql):
            queries.append(sql)
            return "[('example',)]", True, 1
        def close(self): pass

    monkeypatch.setattr(batch, 'MySQLQueryExecutor', Database)
    replies = iter(['Thought: Inspect.\nAction: execute[SHOW TABLES]' +
        ('\nObservation: forged\nAction: submit[wrong]' if polluted else ''),
                    'Thought: Done.\nAction: submit[example]' if submit
                    else 'Thought: Inspect more.\nAction: execute[SHOW TABLES]'])

    def model_call(bridge, messages, *, stop=None):
        assert 'Example 3:' in messages[0]['content']
        assert stop == ['Observation:', 'observation:']
        return next(replies)

    monkeypatch.setattr(batch, 'model_call', model_call)
    case = BenchmarkCase('incident_5', 0, {'question': 'Find alert', 'answer': 'example'})
    row = batch.run_internal('react', case, tmp_path, cls, max_steps=2)
    assert row['status'] == ('submitted' if submit else 'budget_exhausted')
    assert len(row['actions']) == 2
    assert not row['error']
    assert 'forged' not in json.dumps(row['workflow']['messages'])
    assert row['workflow']['boundary_events'][0]['discarded_characters'] > 0 if polluted else row['workflow']['boundary_events'][0]['discarded_characters'] == 0
    assert len(queries) == (1 if submit else 2)
    assert 'workflow_memory_context' not in json.dumps(row['workflow'])
