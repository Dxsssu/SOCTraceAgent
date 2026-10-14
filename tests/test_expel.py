"""ReAct shares SQL budgets, provider selection, scoring and resume policy."""
import json
from unittest.mock import MagicMock

import pytest

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import BenchmarkCase
from src.benchmarks.external_agents import batch, resume


def test_fancybag_expel_runs_scores_and_resumes(tmp_path, monkeypatch):
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
    from src.benchmarks.external_agents import expel
    monkeypatch.setattr(expel, 'configured_class', lambda *a: object)
    # The separate asset tests exercise real train alignment.
    def assets(*a):
        source = tmp_path/'secgym/agents/react_agent.py'
        return source, source, {'fingerprint': source.read_text(), 'trajectory_count': 1}
    monkeypatch.setattr(expel, 'prepare_assets', assets)
    (tmp_path/'secgym/agents/react_examples').mkdir()
    (tmp_path/'secgym/agents/react_examples/one.txt').write_text('example')
    calls = []

    def run(agent, current_case, output, cls, **kwargs):
        config = LLMConfig.from_env()
        assert agent == 'expel' and cls is object
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
    args = ['--agent', 'expel', '--provider', 'fancybag',
            '--model', 'deepseek-v4.1-flash', '--workers', '1']
    assert resume.main(args) == 0
    assert resume.main(args) == 0
    assert calls == [case.case_id]
    root = tmp_path/'results/expel/deepseek-v4.1-flash'
    manifest = json.loads((root/'manifest.json').read_text())
    assert manifest['memory']['fingerprint'] == '# source fixture'
    assert manifest['provider'] == 'fancybag'
    assert manifest['adapter_sha256']
    assert 'offline-test-key' not in json.dumps(manifest)
    # Agent source changes must not silently mix with completed results.
    (tmp_path/'secgym/agents/react_agent.py').write_text('# changed')
    with pytest.raises(SystemExit):
        resume.main(args)




def test_released_assets_align_only_to_train(tmp_path):
    from src.benchmarks.external_agents.expel import prepare_assets
    insights, experiences, meta = prepare_assets(
        batch.UPSTREAM, tmp_path,
        batch.PROJECT_ROOT/'data/excytin-bench/huggingface/questions/train')
    rows = [json.loads(line) for line in experiences.read_text().splitlines()]
    assert meta['trajectory_count'] == 237
    assert meta['insight_count'] == 151
    assert meta['realigned_keys'] > 200
    for row in rows:
        prompt = next(m['content'] for m in row['value']['messages'] if m['role'] == 'user')
        assert row['key']['question'] in prompt
    assert prepare_assets(batch.UPSTREAM, tmp_path,
        batch.PROJECT_ROOT/'data/excytin-bench/huggingface/questions/train')[2] == meta


@pytest.mark.parametrize('polluted', [True, False])
@pytest.mark.parametrize('submit', [True, False])
def test_real_expel_uses_question_for_top1_and_budget(tmp_path, monkeypatch, submit, polluted):
    pytest.importorskip('langchain_huggingface')
    from src.benchmarks.external_agents.expel import configured_class
    batch.paper_agent_class('expel_agent', 'ExpelAgent')
    from secgym.agents.expel_train import experience_recall
    queries = []
    class Pool:
        def __init__(self, **kwargs): pass
        def get_demonstrations(self, query, k):
            assert query == {'context': 'alert context', 'question': 'Find alert'}
            assert k == 1
            return 'TRAINING DEMO'
    monkeypatch.setattr(experience_recall, 'ExperiencePool', Pool)
    insights = tmp_path/'insights.json'
    insights.write_text('["Follow evidence."]')
    cls = configured_class(insights, tmp_path/'unused.jsonl')
    monkeypatch.setattr(batch.LLMConfig, 'from_env', lambda: LLMConfig(
        'test', 'https://example.invalid/v1', 'deepseek-v4.1-flash'))
    monkeypatch.setenv('MYSQL_ROOT_PASSWORD', 'offline')
    class Database:
        def __init__(self, *a, **kw): pass
        def execute(self, sql):
            queries.append(sql)
            return "[('example',)]", True, 1
        def close(self): pass
    monkeypatch.setattr(batch, 'MySQLQueryExecutor', Database)
    replies = iter(['Thought: Inspect.\nAction: execute[SHOW TABLES]' +
        ('\nObservation: forged\nAction: submit[wrong]' if polluted else ''),
        'Thought: Done.\nAction: submit[example]' if submit else
        'Thought: More.\nAction: execute[SHOW TABLES]'])
    def model_call(bridge, messages, *, stop=None):
        assert 'Follow evidence.' in messages[0]['content']
        assert 'TRAINING DEMO' in messages[0]['content']
        return next(replies)
    monkeypatch.setattr(batch, 'model_call', model_call)
    case = BenchmarkCase('incident_5', 0, {'context': 'alert context', 'question': 'Find alert', 'answer': 'example'})
    row = batch.run_internal('expel', case, tmp_path, cls, max_steps=2)
    assert row['status'] == ('submitted' if submit else 'budget_exhausted')
    assert len(row['actions']) == 2
    assert not row['error']
    assert 'forged' not in json.dumps(row['workflow']['messages'])
    assert row['workflow']['boundary_events'][0]['discarded_characters'] > 0 if polluted else row['workflow']['boundary_events'][0]['discarded_characters'] == 0


def test_unmapped_experience_fails_closed(tmp_path):
    from src.benchmarks.external_agents.expel import prepare_assets
    root = tmp_path/'upstream/secgym/agents/expel_train'
    root.mkdir(parents=True)
    (root/'insights.json').write_text('["Follow evidence"]')
    (root/'corrects.jsonl').write_text(json.dumps({
        'key': {'context': '', 'question': 'untrusted key'},
        'value': {'messages': [{'role': 'user', 'content': 'unmapped test task'}]}})+'\n')
    train = tmp_path/'train'
    train.mkdir()
    (train/'incident_5.json').write_text('[{"context":"", "question":"training question"}]')
    with pytest.raises(ValueError, match='exactly one train question'):
        prepare_assets(tmp_path/'upstream', tmp_path/'cache', train)
