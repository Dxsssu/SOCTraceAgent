import json
from pathlib import Path

import pytest

from src.benchmarks.external_agents.single_step import act, parse_first_action


@pytest.mark.parametrize('tail', [
    '\nObservation: fabricated\nThought: done\nAction: submit[wrong]',
    '\nAction: submit[wrong]',
    ' Observation: fabricated Action: submit[wrong]',
])
def test_discards_everything_after_first_action(tail):
    sql, submit, clean, discarded = parse_first_action('Thought: inspect\nAction: execute[SELECT 1]'+tail)
    assert (sql, submit) == ('SELECT 1', False)
    assert 'wrong' not in clean and 'Observation' not in clean
    assert discarded > 0


@pytest.mark.parametrize('sql', [
    "SELECT 'submit[wrong]', 'Observation:', '[x]'",
    "SELECT JSON_EXTRACT(payload, '$.items[0]') FROM t",
    "SELECT `a]b` FROM t WHERE name = 'O''Brien'",
    'SELECT 1\nFROM t',
])
def test_preserves_sql_and_quoted_brackets(sql):
    assert parse_first_action(f'Thought: inspect\n**Action:** execute[{sql}]')[0] == sql


def test_preserves_answer_apostrophe():
    assert parse_first_action("Action: submit[O'Brien]")[0] == "O'Brien"


@pytest.mark.parametrize('text', [
    'Observation: fake\nAction: submit[wrong]',
    'Action: execute[SELECT 1\nObservation: fake\nAction: submit[wrong]',
    'Thought: no action', 'Action: execute[]',
    'Action: nonsense\nAction: submit[wrong]',
])
def test_malformed_first_action_fails_closed(text):
    with pytest.raises(ValueError):
        parse_first_action(text)


class Agent:
    def __init__(self, replies):
        self.messages = [{'role': 'system', 'content': 'original prompt'}]
        self.step_count = 0
        self.replies = iter(replies)
        self.requests = []
    def _add_message(self, content, role):
        self.messages.append({'role': role, 'content': content})
    def _call_llm(self, messages):
        self.requests.append(messages)
        return next(self.replies)


def test_premature_submit_repaired_without_fake_context():
    a = Agent(['Action: submit[hallucinated]', 'Action: execute[SHOW TABLES]'])
    assert act(a, 'real question', can_submit=False) == ('SHOW TABLES', False)
    assert a.step_count == 1
    assert 'hallucinated' not in json.dumps(a.messages)
    assert 'hallucinated' not in json.dumps(a.requests[1])
    assert len(a.boundary_events) == 2


def test_only_one_repair_and_no_submission_on_failure():
    a = Agent(['Action: submit[wrong]', 'Action: submit[wrong again]'])
    with pytest.raises(ValueError, match='after one repair'):
        act(a, 'question', can_submit=False)
    assert a.step_count == 0
    assert len(a.requests) == 2


def test_real_failure_trace_replays_first_sql_only():
    path = Path('outputs/react-base-audit/snapshot.json')
    if not path.exists():
        pytest.skip('Historical audit fixture unavailable')
    pairs = json.loads(path.read_text())['pairs']
    row = next(x for x,y in pairs if x['case_id'] == 'incident_5-q0034')
    reply = row['workflow']['messages'][-1]['content']
    sql, submit, clean, discarded = parse_first_action(reply)
    assert sql.startswith('SELECT * FROM AlertInfo')
    assert not submit and discarded > 0
    assert 'Observation:' not in clean
    assert '4e4f5b6c' not in clean
