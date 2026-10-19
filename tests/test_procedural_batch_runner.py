import json
import threading
import time
from types import SimpleNamespace

from scripts.run_procedural_workflow_fancybag import run_batch, save, previous_result


def cases(n):
    return [SimpleNamespace(case_id=f'case-{i}', question={'question': 'test'}) for i in range(n)]


def success(case):
    # A wrong answer is still a completed run, and must not be retried silently.
    return {'case_id': case.case_id, 'status': 'completed', 'error': '',
            'submitted_answer': 'answer', 'evaluation': {'exact_match': False}}


def test_ten_parallel_workers_isolated_attempts_and_resume(tmp_path):
    selected = cases(13)
    barrier = threading.Barrier(10)
    lock = threading.Lock()
    current = peak = 0
    seen = []

    def worker(case, directory, config, max_steps, stop):
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
            seen.append(case.case_id)
        try:
            if int(case.case_id.split('-')[1]) < 10:
                barrier.wait(timeout=5)
            save(directory / 'calls.json', [{'case_id': case.case_id}])
            time.sleep(.01)
            return success(case)
        finally:
            with lock:
                current -= 1

    assert run_batch(selected, tmp_path, None, worker=worker) == 0
    assert peak == 10
    assert sorted(seen) == sorted(c.case_id for c in selected)
    for case in selected:
        paths = list((tmp_path / case.case_id / 'attempts').glob('*/calls.json'))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text()) == [{'case_id': case.case_id}]
    seen.clear()
    assert run_batch(selected, tmp_path, None, worker=worker) == 0
    assert not seen
    assert json.loads((tmp_path / 'progress.json').read_text())['skipped'] == 13


def test_failed_running_corrupt_missing_retried_completed_skipped(tmp_path):
    selected = cases(5)
    for i, record in [(0, success(selected[0])), (1, {'status': 'failed'}), (2, {'status': 'running'})]:
        directory = tmp_path / selected[i].case_id
        directory.mkdir()
        save(directory / 'summary.json', record)
    directory = tmp_path / selected[3].case_id
    directory.mkdir()
    (directory / 'summary.json').write_text('{broken')
    seen = []

    def worker(case, *args):
        seen.append(case.case_id)
        if case.case_id == 'case-1':
            raise RuntimeError('simulated API failure')
        return success(case)

    assert run_batch(selected, tmp_path, None, worker=worker) == 1
    assert set(seen) == {'case-1', 'case-2', 'case-3', 'case-4'}
    seen.clear()

    def repaired(case, *args):
        seen.append(case.case_id)
        return success(case)

    assert run_batch(selected, tmp_path, None, worker=repaired) == 0
    assert seen == ['case-1']
    attempts = list((tmp_path / 'case-1/attempts').glob('*/summary.json'))
    assert len(attempts) == 2
    assert {json.loads(p.read_text())['status'] for p in attempts} == {'completed', 'failed'}


def test_legacy_resume_requires_final_record(tmp_path):
    case = cases(1)[0]
    directory = tmp_path / case.case_id
    directory.mkdir()
    record = success(case)
    save(directory / f'{case.case_id}.json', record)
    assert previous_result(tmp_path, case) is None
    record['finished_at'] = '2026-10-19'
    save(directory / f'{case.case_id}.json', record)
    assert previous_result(tmp_path, case)['status'] == 'completed'


def test_partial_completion_checkpoint_survives_next_run(tmp_path):
    selected = cases(2)
    def worker(case, *args):
        if case.case_id == 'case-1':
            assert previous_result(tmp_path, selected[0]) is not None
        return success(case)
    assert run_batch(selected, tmp_path, None, workers=1, worker=worker) == 0
    assert len(json.loads((tmp_path / 'summary.json').read_text())) == 2
