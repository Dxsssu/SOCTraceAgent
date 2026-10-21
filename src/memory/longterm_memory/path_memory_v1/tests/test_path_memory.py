import json
import pytest
from src.memory.longterm_memory.path_memory_v1.store import MemoryStore, ROOT


def test_structure_and_no_gold():
    m = MemoryStore()
    assert len(m.sm['concepts']) == 5
    assert len(m.episodes) == 3
    assert 'answer' not in json.dumps([m.sm, m.em, m.pm])


@pytest.mark.parametrize('damage', ['kind', 'future', 'binding', 'output'])
def test_invalid_contract(damage):
    m = MemoryStore()
    p = m.pm['procedures'][0]
    if damage == 'kind':
        m.em['episodes'][0]['known']['url']['kind'] = 'sid'
    elif damage == 'future':
        p['steps'][1]['bindings']['sids']['step'] = 'T3'
    elif damage == 'binding':
        del p['steps'][0]['bindings']['title']
    else:
        p['wanted'] = p['known']
    with pytest.raises((AssertionError, KeyError)):
        m.validate()


def test_parameter_binding_and_url_identity():
    m = MemoryStore()
    url = "http://x/' OR 1=1 --"
    sql, params = m.compile('em_url_to_sid', {'url': url, 'title': 't'})
    assert url not in sql and params[0] == url
    assert 'https://' not in params[0]


@pytest.mark.parametrize('device,short', [('MB-WINCLIENT', True), ('MB-WINCLIENT.corp.test', False)])
def test_device_matching(device, short):
    sql, params = MemoryStore().compile('em_device_to_url', {'devices': [device], 'title': 't'})
    assert ('IN (NULL)' not in sql) == short
    assert params.count(device.lower()) == (2 if short else 1)


def test_all_candidates_flow():
    m = MemoryStore()
    inputs = json.loads((ROOT / 'training_example.json').read_text())['inputs']
    calls = []
    def query(sql, params):
        calls.append(params)
        return [[{'AccountSid': 'S1'}, {'AccountSid': 'S2'}, {'AccountSid': 'S1'}],
                [{'DeviceName': 'h1.corp'}, {'DeviceName': 'h2.corp'}],
                [{'RemoteUrl': 'https://result'}]][len(calls)-1]
    result = m.run('pm_url_account_device_url', inputs, query)
    assert result['status'] == 'candidate_ready'
    assert 'S1' in calls[1] and 'S2' in calls[1] and calls[1].count('S1') == 1
    assert 'h1.corp' in calls[2] and 'h2.corp' in calls[2]


@pytest.mark.parametrize('mode', ['empty', 'truncated', 'sql_error', 'invalid_output'])
def test_pause_without_next_query(mode):
    inputs = json.loads((ROOT / 'training_example.json').read_text())['inputs']
    calls = []
    def query(sql, params):
        calls.append(sql)
        if mode == 'sql_error':
            raise RuntimeError('simulated connection error')
        return {'empty': [], 'truncated': [{'AccountSid': 's'}]*101, 'invalid_output': [{}]}[mode]
    result = MemoryStore().run('pm_url_account_device_url', inputs, query)
    assert result['status'] == mode and len(calls) == 1
    assert not result['candidates']
