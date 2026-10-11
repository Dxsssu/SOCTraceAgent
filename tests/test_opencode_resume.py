"""Offline end-to-end checks for persistent OpenCode retries."""
import json
import threading
from types import SimpleNamespace

import pytest

from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.runner import BenchmarkCase
from src.benchmarks.external_agents import resume


def result(case, status='submitted', scored=True):
    row = dict(agent='opencode', **case.manifest_view(), status=status,
               answer='answer', actions=[], usage=[], elapsed_seconds=1)
    if scored:
        row.update(evaluation_status='scored', llm_evaluation={
            'llm_judge_answer_correct': False, 'llm_judge_reward': 0})
    return row


def test_cli_retries_failures_and_missing_but_skips_finished(tmp_path, monkeypatch, capsys):
    cases = [BenchmarkCase('incident_5', i, {'question': str(i)}) for i in range(6)]
    config = LLMConfig('fake', 'https://example.invalid/v1', 'DeepSeek-V4.1-Flash')
    monkeypatch.setattr(resume, 'load_dotenv', lambda *a: None)
    monkeypatch.setattr(resume, 'load_test_cases', lambda: cases)
    monkeypatch.setattr(resume.LLMConfig, 'from_env', lambda: config)
    monkeypatch.setattr(resume, 'judge_config', lambda *a: config)
    monkeypatch.setattr(resume, 'protocol_manifest', lambda *a, **k: {'version': 'test'})
    calls = []
    def run(agent, case, output, **kwargs):
        calls.append(case.qid)
        row = result(case, 'error' if case.qid == 0 else 'submitted', case.qid != 0)
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row
    monkeypatch.setattr(resume, 'run_case', run)
    args = ['--output', str(tmp_path), '--workers', '1']
    assert resume.main(args) == 1
    assert sorted(calls) == list(range(6))
    # Different interrupted conditions after the first invocation.
    for qid, status in [(2, 'skipped'), (3, 'running')]:
        resume.write_json(tmp_path/'cases'/cases[qid].case_id/'result.json', result(cases[qid], status, False))
    (tmp_path/'cases'/cases[4].case_id/'result.json').write_text('{broken')
    (tmp_path/'cases'/cases[5].case_id/'result.json').unlink()
    calls.clear()
    def succeed(agent, case, output, **kwargs):
        calls.append(case.qid)
        row = result(case)
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row
    monkeypatch.setattr(resume, 'run_case', succeed)
    assert resume.main(args) == 0
    assert sorted(calls) == [0, 2, 3, 4, 5]
    assert len(list((tmp_path/'attempts').glob('*/*'))) == 5
    calls.clear()
    assert resume.main(args) == 0
    assert calls == []
    summary = json.loads((tmp_path/'summary.json').read_text())
    assert summary['overall']['opencode']['judged'] == 6
    assert summary['overall']['opencode']['accuracy'] == 0
    assert '6/6' in capsys.readouterr().err


def test_judge_failure_reuses_investigation_and_retains_history(tmp_path, monkeypatch):
    case = BenchmarkCase('incident_5', 0, {'question': 'q'})
    path = tmp_path/'cases'/case.case_id/'result.json'
    row = result(case, scored=False)
    row.update(evaluation_status='judge_error', judge_error='failed', judge_usage=[{'total_tokens': 7}])
    resume.write_json(path, row)
    monkeypatch.setattr(resume, 'run_case', lambda *a, **k: pytest.fail('Must reuse saved answer'))
    def score(case, row, judge):
        row.pop('judge_error', None)
        row.update(evaluation_status='scored', llm_evaluation={
            'llm_judge_answer_correct': False, 'llm_judge_reward': 0})
        row['judge_usage'].append({'total_tokens': 3})
    monkeypatch.setattr(resume, 'score_case', score)
    final = resume.retry_case(case, tmp_path, SimpleNamespace(), None, threading.Event())
    assert resume.completed(final)
    assert sum(u['total_tokens'] for u in final['judge_usage']) == 10
    assert len(list((tmp_path/'attempts').glob('*/*/result.json'))) == 1


def test_lock_and_manifest_prevent_mixing(tmp_path):
    with resume.output_lock(tmp_path):
        with pytest.raises(ValueError, match='运行中的任务'):
            with resume.output_lock(tmp_path):
                pass
    resume.write_json(tmp_path/'manifest.json', {'model': 'old'})
    with pytest.raises(ValueError, match='已有结果不同'):
        resume.execute(None, tmp_path, [], {'model': 'new'}, None)


def test_interrupt_marks_episode_for_retry():
    from src.benchmarks.external_agents.runner import wait_for_episode
    from src.benchmarks.external_agents.bridge import Bridge
    bridge = Bridge(None, LLMConfig('fake', 'https://example.invalid', 'test'))
    stop = threading.Event()
    stop.set()
    assert wait_for_episode(None, bridge, stop_event=stop) == 'interrupted'
    assert not resume.completed({'status': 'interrupted'})


def test_live_scores_include_resumed_partial_credit_and_exclude_failures(tmp_path, monkeypatch, capsys):
    cases = [BenchmarkCase('incident_5', i, {'question': str(i)}) for i in range(3)]
    manifest = {'model': 'test', 'protocol': {}}
    resume.write_json(tmp_path/'manifest.json', manifest)
    previous = result(cases[0])
    previous['llm_evaluation'] = {'llm_judge_answer_correct': True, 'llm_judge_reward': 1}
    resume.write_json(tmp_path/'cases'/cases[0].case_id/'result.json', previous)
    def run(agent, case, output, **kwargs):
        row = result(case, 'submitted' if case.qid == 1 else 'error', case.qid == 1)
        if case.qid == 1:
            row['llm_evaluation']['llm_judge_reward'] = .4
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row
    monkeypatch.setattr(resume, 'run_case', run)
    args = SimpleNamespace(workers=1, max_steps=25, timeout=0, max_llm_calls=0)
    assert resume.execute(args, tmp_path, cases, manifest, None) == 1
    text = capsys.readouterr().err
    assert '已完成评分=1/3 完全正确=1 Reward总分=1.0000 已评分平均Reward=1.0000' in text
    assert '已完成评分=2/3 完全正确=1 Reward总分=1.4000 已评分平均Reward=0.7000' in text
    progress = json.loads((tmp_path/'progress.json').read_text())
    assert progress['complete'] == 2
    assert progress['correct'] == 1
    assert progress['total_reward'] == pytest.approx(1.4)
    assert progress['scored_mean_reward'] == pytest.approx(.7)
    assert resume.score_progress({})['scored_mean_reward'] is None


def test_pi_cli_default_path_and_resume(tmp_path, monkeypatch):
    case = BenchmarkCase('incident_5', 0, {'question': 'q'})
    config = LLMConfig('fake', 'https://example.invalid/v1', 'DeepSeek-V4.1-Flash')
    monkeypatch.setattr(resume, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(resume, 'load_dotenv', lambda *a: None)
    monkeypatch.setattr(resume, 'load_test_cases', lambda: [case])
    monkeypatch.setattr(resume.LLMConfig, 'from_env', lambda: config)
    monkeypatch.setattr(resume, 'judge_config', lambda *a: config)
    monkeypatch.setattr(resume, 'protocol_manifest', lambda *a, **k: {'version': 'test'})
    calls = []
    def run(agent, case, output, **kwargs):
        calls.append(agent)
        row = result(case)
        row['agent'] = agent
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row
    monkeypatch.setattr(resume, 'run_case', run)
    assert resume.main(['--agent', 'pi']) == 0
    root = tmp_path/'results'/'pi'/config.model
    assert json.loads((root/'manifest.json').read_text())['agents'] == ['pi']
    assert set(json.loads((root/'summary.json').read_text())['overall']) == {'pi'}
    assert resume.main(['--agent', 'pi']) == 0
    assert calls == ['pi']
    assert not (tmp_path/'results'/'opencode').exists()
    with pytest.raises(SystemExit):
        resume.main(['--agent', 'opencode', '--output', str(root)])
    assert calls == ['pi']


def test_paper_baseline_runs_scores_and_resumes(tmp_path, monkeypatch):
    from src.benchmarks.external_agents import batch
    case = BenchmarkCase('incident_5', 0, {'question': 'q'})
    config = LLMConfig('fake', 'https://example.invalid/v1', 'DeepSeek-V4.1-Flash')
    monkeypatch.setattr(resume, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(resume, 'load_dotenv', lambda *a: None)
    monkeypatch.setattr(resume, 'load_test_cases', lambda: [case])
    monkeypatch.setattr(resume.LLMConfig, 'from_env', lambda: config)
    monkeypatch.setattr(resume, 'judge_config', lambda *a: config)
    monkeypatch.setattr(resume, 'protocol_manifest', lambda *a, **k: {'version': 'test'})
    source = tmp_path/'secgym/agents/baseline_agent.py'
    source.parent.mkdir(parents=True)
    source.write_text('# official source fixture')
    monkeypatch.setattr(batch, 'UPSTREAM', tmp_path)
    monkeypatch.setattr(batch, 'baseline_class', lambda: object)
    calls = []
    def run(agent, case, output, Baseline, **kwargs):
        assert agent == 'paper_baseline' and Baseline is object
        calls.append(case.case_id)
        row = result(case, scored=False)
        row['agent'] = agent
        resume.write_json(kwargs['case_dir']/'result.json', row)
        return row
    monkeypatch.setattr(batch, 'run_internal', run)
    def score(case, row, judge):
        row.update(evaluation_status='scored', llm_evaluation={
            'llm_judge_answer_correct': True, 'llm_judge_reward': 1})
    monkeypatch.setattr(resume, 'score_case', score)
    assert resume.main(['--agent', 'paper_baseline']) == 0
    assert resume.main(['--agent', 'paper_baseline']) == 0
    assert calls == [case.case_id]
    root = tmp_path/'results/paper_baseline'/config.model
    manifest = json.loads((root/'manifest.json').read_text())
    assert manifest['baseline_source_sha256']
    assert 'instruction_sha256' not in manifest
    assert json.loads((root/'summary.json').read_text())['overall']['paper_baseline']['correct'] == 1
