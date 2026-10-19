"""Transport retries are bounded and independent from role format correction."""
import json
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from src.agent.executor import SQL_EXECUTOR_SYSTEM_PROMPT
from src.agent.llm import LLMConfig
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from test_excytin_benchmark_workflow import INITIAL_OPERATIONS


def api_error(kind):
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    if kind == 'timeout':
        return APITimeoutError(request=request)
    if kind == 'connection':
        return APIConnectionError(request=request)
    if kind == 'httpx_timeout':
        return httpx.ReadTimeout('timeout', request=request)
    return APIStatusError('upstream failure', response=httpx.Response(kind, request=request), body={})


@pytest.fixture
def sleep(monkeypatch):
    mocked = Mock()
    monkeypatch.setattr('src.benchmarks.excytin_bench.workflow.time.sleep', mocked)
    return mocked


@pytest.mark.parametrize('kind', ['timeout', 'connection', 'httpx_timeout', 408, 409, 429, 500, 502, 503, 504])
def test_transport_retries_twice_with_unchanged_payload(kind, sleep):
    model = Mock(side_effect=[api_error(kind), api_error(kind), 'sql: SELECT 1'])
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    assert workflow._call_role_llm(SQL_EXECUTOR_SYSTEM_PROMPT, 'original input') == 'sql: SELECT 1'
    assert model.call_count == 3 and sleep.call_count == 2
    assert all(5 <= c.args[0] <= 10 for c in sleep.call_args_list)
    assert model.call_args_list[0] == model.call_args_list[1] == model.call_args_list[2]
    trace = workflow.get_logging()['trace']
    assert [t['event_type'] for t in trace] == ['llm_api_retry_scheduled'] * 2
    assert all(t['payload']['role'] == 'executor' for t in trace)


def test_exhausted_transport_retries_propagate_last_error(sleep):
    error = api_error(502)
    model = Mock(side_effect=error)
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    with pytest.raises(APIStatusError) as caught:
        workflow._call_role_llm('system', 'input')
    assert caught.value is error
    assert model.call_count == 3 and sleep.call_count == 2
    assert workflow.get_logging()['trace'][-1]['event_type'] == 'llm_api_retries_exhausted'


@pytest.mark.parametrize('error', [api_error(400), api_error(401), api_error(403), api_error(404), ValueError('bad output')])
def test_nontransient_errors_do_not_retry(error, sleep):
    model = Mock(side_effect=error)
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    with pytest.raises(type(error)):
        workflow._call_role_llm('system', 'input')
    assert model.call_count == 1
    sleep.assert_not_called()


def test_default_transport_disables_sdk_retry_stacking(monkeypatch):
    config = LLMConfig(api_key='test', base_url='https://example.invalid', model='test', max_retries=9)
    monkeypatch.setattr('src.benchmarks.excytin_bench.workflow.LLMConfig.from_env', lambda: config)
    client = Mock()
    client.chat.return_value = 'result'
    factory = Mock(return_value=client)
    monkeypatch.setattr('src.benchmarks.excytin_bench.workflow.LLMClient', factory)
    assert ExcytinBenchWorkflow._default_llm('system', 'input') == 'result'
    assert factory.call_args.args[0].max_retries == 0
    client.client.close.assert_called_once()


def test_executor_format_retry_has_separate_transport_budget(sleep):
    responses = iter([api_error(429), api_error(502), 'Not YAML',
                      api_error('timeout'), api_error('connection'), 'sql: SELECT 1'])
    inputs = []
    def model(system, user):
        if '系统中的 Planner' in system:
            return INITIAL_OPERATIONS
        inputs.append(json.loads(user))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    action = workflow.act('Investigate')
    assert action.content == 'SELECT 1;'
    assert len(inputs) == 6 and sleep.call_count == 4
    assert inputs[0] == inputs[1] == inputs[2]
    assert inputs[3] == inputs[4] == inputs[5]
    assert '输出格式修正' in inputs[3]['objective']
    assert '上一响应解析错误' in inputs[3]['objective']
    assert workflow.state.actions_issued == 1
    assert workflow.state.executions == []


def test_sql_error_context_survives_format_correction(sleep):
    inputs = []
    def model(system, user):
        if '系统中的 Planner' in system:
            return INITIAL_OPERATIONS
        payload = json.loads(user)
        inputs.append(payload)
        return 'not yaml' if len(inputs) == 2 else 'sql: SELECT 1'
    workflow = ExcytinBenchWorkflow(semantic_memory_enabled=False, llm=model)
    workflow.act('Investigate')
    workflow.accept_observation('Unknown column x', query_success=False)
    assert workflow.propose_next_action().content == 'SELECT 1;'
    assert 'error' not in inputs[0]
    assert inputs[1]['error'] == inputs[2]['error']
    assert 'Unknown column x' in inputs[2]['error']
    assert '上一响应解析错误' in inputs[2]['objective']
    assert len(workflow.state.executions) == 1
    sleep.assert_not_called()
