"""PromptSauce shares SQL budgets, provider selection, scoring and resume policy."""
import json
from unittest.mock import MagicMock

import pytest

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import BenchmarkCase
from src.benchmarks.external_agents import batch, resume


def test_fancybag_prompt_sauce_runs_scores_and_resumes(tmp_path, monkeypatch):
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
    for name in ['prompt_sauce_agent.py', 'agent_utils.py']:
        p = tmp_path/'secgym/agents'/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('# source fixture')
    monkeypatch.setattr(batch, 'UPSTREAM', tmp_path)
    monkeypatch.setattr(batch, 'prompt_sauce_class', lambda: object)
    calls = []

    def run(agent, current_case, output, cls, **kwargs):
        config = LLMConfig.from_env()
        assert agent == 'prompt_sauce' and cls is object
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
    args = ['--agent', 'prompt_sauce', '--provider', 'fancybag',
            '--model', 'deepseek-v4.1-flash', '--workers', '1']
    assert resume.main(args) == 0
    assert resume.main(args) == 0
    assert calls == [case.case_id]
    root = tmp_path/'results/prompt_sauce/deepseek-v4.1-flash'
    manifest = json.loads((root/'manifest.json').read_text())
    assert manifest['memory'] == 'none'
    assert manifest['provider'] == 'fancybag'
    assert manifest['paper_agent_sources']
    assert 'offline-test-key' not in json.dumps(manifest)
    # Agent source changes must not silently mix with completed results.
    (tmp_path/'secgym/agents/prompt_sauce_agent.py').write_text('# changed')
    with pytest.raises(SystemExit):
        resume.main(args)


@pytest.mark.parametrize('submit', [True, False])
def test_real_prompt_sauce_last_action_and_budget(tmp_path, monkeypatch, submit):
    pytest.importorskip('autogen')
    if not (batch.UPSTREAM/'secgym/agents/prompt_sauce_agent.py').exists():
        pytest.skip('Upstream PromptSauce source not installed')
    cls = batch.prompt_sauce_class()
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
    replies = iter(['Thought: Inspect.\nAction: execute[SHOW TABLES]',
                    'Thought: Done.\nAction: submit[example]' if submit
                    else 'Thought: Inspect more.\nAction: execute[SHOW TABLES]'])

    def model_call(bridge, messages):
        assert 'start from the alert(s)' in messages[0]['content']
        return next(replies)

    monkeypatch.setattr(batch, 'model_call', model_call)
    case = BenchmarkCase('incident_5', 0, {'question': 'Find alert', 'answer': 'example'})
    row = batch.run_internal('prompt_sauce', case, tmp_path, cls, max_steps=2)
    assert row['status'] == ('submitted' if submit else 'budget_exhausted')
    assert len(row['actions']) == 2
    assert not row['error']
    assert len(queries) == (1 if submit else 2)
    assert 'workflow_memory_context' not in json.dumps(row['workflow'])
