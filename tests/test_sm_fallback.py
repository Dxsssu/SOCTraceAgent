import json
from unittest.mock import patch

from src.memory.longterm_memory.semantic_memory.excytin_bench.fallback import retrieve, SYSTEM_PROMPT, MAX_INPUT_BYTES, MAX_SCHEMA_BYTES
from src.workflow.context import ExcytinBenchSnapshotRepository, MemoryContextService
from src.schema import Event
from src.schema.itt import planner_prompt


def test_one_call_selects_real_schema_with_bounded_input():
    repo = ExcytinBenchSnapshotRepository()
    calls = []
    def model(system, user):
        calls.append(user)
        assert len((system + user).encode()) <= MAX_INPUT_BYTES
        data = json.loads(user)
        assert 'itt' not in data and 'executions' not in data
        return json.dumps([next(i for i,t in enumerate(data['catalog']) if t['name'] == 'DeviceProcessEvents')])
    result = retrieve(repo, {'question': 'Find process', 'context': '中' * 20000, 'itt': 'SECRET'}, model)
    assert len(calls) == 1
    names = {f['name'] for f in result['tables'][0]['field']}
    assert {'FileName', 'ProcessCommandLine', 'InitiatingProcessFileName'} <= names
    assert 'ProcessName' not in names
    assert len(json.dumps(result['tables'], ensure_ascii=False).encode()) <= MAX_SCHEMA_BYTES
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
        assert result['tables'] == [] and len(calls) == 1


def test_pm_miss_caches_sm_and_pm_hit_skips_sm():
    repo = ExcytinBenchSnapshotRepository()
    calls = []
    def model(system, user):
        assert system == SYSTEM_PROMPT
        calls.append(user)
        return '[0]'
    service = MemoryContextService(repositories={'excytin_bench': repo}, procedure_selector=model)
    event = Event(event_id='sm-cache', event_name='test', message='Find process', context={'memory_profile': 'excytin_bench'})
    with patch.object(repo, 'retrieve_procedures', return_value=[]):
        first = service.planner_view().build(event)
        second = service.planner_view().build(event)
    assert len(calls) == 1 and first['semantic_memory']['tables']
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
