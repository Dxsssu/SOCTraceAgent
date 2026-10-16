import json
from unittest.mock import patch

from src.memory.longterm_memory.semantic_memory.excytin_bench.fallback import retrieve, SYSTEM_PROMPT
from src.workflow.context import ExcytinBenchSnapshotRepository, MemoryContextService
from src.schema import Event
from src.schema.itt import planner_prompt


def test_one_call_selects_real_schema_with_bounded_input():
    repo = ExcytinBenchSnapshotRepository()
    calls = []
    def model(system, user):
        calls.append(user)
        data = json.loads(user)
        assert 'itt' not in data and 'executions' not in data
        assert len(data['log_sources']) == 58
        assert all(set(t) == {'table', 'desc', 'entities'} for t in data['log_sources'])
        return json.dumps('DeviceProcessEvents')
    result = retrieve(repo, {'question': 'Find process', 'context': '中' * 20000, 'itt': 'SECRET'}, model)
    assert len(calls) == 1
    names = {f['name'] for f in result['table_cards'][0]['core_columns']}
    assert {'FileName', 'ProcessCommandLine', 'InitiatingProcessFileName'} <= names
    assert 'ProcessName' not in names
    assert all(set(f) == {'name', 'type', 'desc'} for f in result['table_cards'][0]['core_columns'])
    assert 'FileName' in planner_prompt('system', {'semantic_memory': result})


def test_bad_selection_does_not_retry_or_invent_schema():
    repo = ExcytinBenchSnapshotRepository()
    for raw in ['not json', '[true]', '[9999]', '[0,0]', '[0,1,2]', '{"table":"Invented"}']:
        with patch('builtins.print'):
            calls = []
            def model(*args):
                calls.append(args)
                return raw
            result = retrieve(repo, {'question': 'Find process'}, model)
        assert result['status'] == 'invalid_response'
        assert result['table_cards'] == [] and len(calls) == 1


def test_pm_miss_caches_sm_and_pm_hit_skips_sm():
    repo = ExcytinBenchSnapshotRepository()
    calls = []
    def model(system, user):
        assert system == SYSTEM_PROMPT
        calls.append(user)
        return '"DeviceProcessEvents"'
    service = MemoryContextService(repositories={'excytin_bench': repo}, procedure_selector=model)
    event = Event(event_id='sm-cache', event_name='test', message='Find process', context={'memory_profile': 'excytin_bench'})
    with patch.object(repo, 'retrieve_procedures', return_value=[]):
        first = service.planner_view().build(event)
        second = service.planner_view().build(event)
    assert len(calls) == 1 and first['semantic_memory']['table_cards']
    assert second['semantic_memory']['reuse'] == 'unchanged_query'
    service = MemoryContextService(repositories={'excytin_bench': repo}, procedure_selector=model)
    with patch.object(repo, 'retrieve_procedures', return_value=[{'name': 'pm'}]):
        assert 'semantic_memory' not in service.planner_view().build(event)
    assert len(calls) == 1


def test_sm_model_request_has_output_cap_and_disables_thinking():
    from src.agent.llm import LLMClient, LLMConfig
    from src.memory.longterm_memory.semantic_memory.excytin_bench.fallback import MAX_OUTPUT_TOKENS
    config = LLMConfig(api_key='test', base_url='https://example.invalid', model='test', thinking_enabled=True)
    with patch('src.agent.llm.OpenAI') as api, patch('src.agent.llm.get_rate_limiter'):
        api.return_value.chat.completions.create.return_value.usage = None
        LLMClient(config).chat(system_prompt=SYSTEM_PROMPT, user_prompt='{}')
    request = api.return_value.chat.completions.create.call_args.kwargs
    assert request['max_tokens'] == MAX_OUTPUT_TOKENS
    assert request['extra_body']['thinking'] == {'type': 'disabled'}


def test_online_selection_opens_only_the_selected_l2():
    from pathlib import Path
    original = Path.read_text
    opened = []
    def track(path, *args, **kwargs):
        if path.parent.name == 'l2':
            opened.append(path.name)
        return original(path, *args, **kwargs)
    with patch.object(Path, 'read_text', track):
        repo = ExcytinBenchSnapshotRepository()
        assert opened == []
        result = retrieve(repo, {'question': 'Find process'}, lambda *_: '"DeviceProcessEvents"')
    assert opened == ['DeviceProcessEvents.yaml']
    assert repo._semantic is None
    card = result['table_cards'][0]
    assert card == repo.semantic_store.read_table('DeviceProcessEvents')
    prompt = planner_prompt('system', {'semantic_memory': result})
    assert all(f['name'] in prompt and f['desc'] in prompt for f in card['core_columns'])


def test_exact_lookup_rejects_unknown_names_and_path_traversal():
    import pytest
    repo = ExcytinBenchSnapshotRepository()
    for name in ['../l1', 'deviceprocessevents', 'Invented', None]:
        with pytest.raises(ValueError):
            repo.semantic_store.read_table(name)


def test_l1_keeps_last_table_and_l2_is_not_truncated():
    repo = ExcytinBenchSnapshotRepository()
    seen = []
    def select(_system, user):
        catalog = json.loads(user)['log_sources']
        seen.extend(t['table'] for t in catalog)
        return '"OfficeActivity"'
    result = retrieve(repo, {'question': 'Office activity'}, select)
    assert 'Usage' in seen and len(seen) == 58
    card = result['table_cards'][0]
    assert len(card['core_columns']) > 100
    assert card == repo.semantic_store.read_table('OfficeActivity')
