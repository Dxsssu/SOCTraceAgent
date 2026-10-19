"""Behavioral checks for conservative retrieval, safe binding and schema drift."""
from copy import deepcopy
import json

import pytest

from src.memory.longterm_memory.procedural_memory.snippets import SnippetMemory
from src.memory.longterm_memory.procedural_memory.snippets.store import PACKAGE, compile_query, validate_template


@pytest.fixture
def memory():
    return SnippetMemory.load()


def sender_template(memory):
    return next(t for t in memory.snippets['proc_mail_sender']['steps'][0]['executor_sql_templates']
                if t['inputs']['value']['operator'] == 'EQ')


def test_kind_is_required_and_ids_cannot_be_interchanged(memory):
    result = memory.retrieve(trigger_entity='邮件ID', intent='发件人')
    assert result['route'] == 'semantic'
    assert {r['expected_kind'] for r in result['blocked']} == {'network_message_id', 'internet_message_id'}
    result = memory.retrieve(trigger_entity='邮件ID', intent='发件人', kind='network_message_id')
    assert [r['id'] for r in result['candidates']] == ['proc_mail_sender']
    assert result['candidates'][0]['readiness'] == 'needs_live_schema'
    assert result['candidates'][0]['outputs']['SenderFromAddress']['entity_kind'] == 'header_from'
    result = memory.retrieve(trigger_entity='URL', intent='邮件标识')
    assert result['candidates'][0]['outputs']['NetworkMessageId']['entity_kind'] == 'network_message_id'


def test_no_keyword_overreach_for_uncovered_intent(memory):
    result = memory.retrieve(trigger_entity='邮件ID', kind='network_message_id', intent='查询 DLP 违规敏感词分类与发件人')
    assert result['route'] == 'semantic'
    assert not result['candidates']


def test_ip_alias_does_not_force_firewall_role(memory):
    result = memory.retrieve(trigger_entity='Source_IP', intent='认证时间', schema=memory.document['schema_snapshot'])
    assert [r['id'] for r in result['candidates']] == ['proc_ip_signins']
    assert result['candidates'][0]['trigger_entity']['kind'] == 'login_ip'
    assert result['candidates'][0]['readiness'] == 'schema_checked'


def test_empty_or_changed_live_schema_forces_fallback(memory):
    for schema in ({}, {'EmailEvents': {'NetworkMessageId': 'text'}}):
        result = memory.retrieve(trigger_entity='Email_ID', kind='network_message_id', intent='发件人', schema=schema)
        assert result['route'] == 'semantic'


def test_environment_mismatch(memory):
    assert memory.retrieve(trigger_entity='Email_ID', intent='发件人', environment='other')['reason'] == 'environment_mismatch'


def test_candidate_requires_explicit_opt_in(memory):
    args = dict(trigger_entity='Email_ID', kind='internet_message_id', intent='发件人')
    assert memory.retrieve(**args)['route'] == 'semantic'
    assert memory.retrieve(**args, include_candidates=True)['route'] == 'procedural'
    s = memory.snippets['proc_header_mail_sender']
    t = s['steps'][0]['executor_sql_templates'][0]
    with pytest.raises(ValueError, match='eligible'):
        memory.prepare(snippet_id=s['id'], template_id=t['id'], kind='internet_message_id',
                       bindings={'value': ['<example@invalid>']}, schema=memory.document['schema_snapshot'])


def test_values_never_become_sql_code(memory):
    t = sender_template(memory)
    malicious = "x' OR 1=1; DROP TABLE EmailEvents; --"
    sql, values = compile_query(t, {'value': malicious}, memory.document['schema_snapshot'])
    assert '%s' in sql and malicious not in sql
    assert values == (malicious,)
    assert 'DROP TABLE' not in sql


def test_array_binding_and_pattern_are_explicit(memory):
    t = memory.snippets['proc_header_mail_sender']['steps'][0]['executor_sql_templates'][0]
    sql, values = compile_query(t, {'value': ['one', 'two']}, memory.document['schema_snapshot'])
    assert 'IN (%s, %s)' in sql
    assert values == ('one', 'two')
    with pytest.raises(ValueError):
        compile_query(t, {'value': 'one'}, memory.document['schema_snapshot'])
    t = memory.snippets['proc_command_processes']['steps'][0]['executor_sql_templates'][0]
    _, values = compile_query(t, {'value': '%command%'}, memory.document['schema_snapshot'])
    assert values == ('%command%',)
    _, values = compile_query(t, {'value': 'command'}, memory.document['schema_snapshot'])
    assert values == ('command',)  # No implicit wildcards or query broadening.


@pytest.mark.parametrize('bindings', [{}, {'value': 'x', 'extra': 'y'}, {'value': ''}, {'value': None}, {'value': ['x']}, {'value': 3}])
def test_invalid_scalar_bindings_fail_closed(memory, bindings):
    with pytest.raises(ValueError):
        compile_query(sender_template(memory), bindings, memory.document['schema_snapshot'])


def test_prepare_uses_canonical_template_and_live_schema(memory):
    t = sender_template(memory)
    args = dict(snippet_id='proc_mail_sender', template_id=t['id'], bindings={'value': 'example'}, kind='network_message_id')
    for schema in (None, {}, {'EmailEvents': {'SenderFromAddress': 'text'}}):
        with pytest.raises(ValueError):
            memory.prepare(**args, schema=schema)
    with pytest.raises(ValueError, match='Identifier kind'):
        memory.prepare(**{**args, 'kind': 'internet_message_id'}, schema=memory.document['schema_snapshot'])
    result = memory.prepare(**args, schema=memory.document['schema_snapshot'])
    assert result['params'] == ('example',)
    assert result['on_empty'] == 'semantic'


@pytest.mark.parametrize('sql', [
    'SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = :value LIMIT 100; DROP TABLE EmailEvents',
    'SELECT SenderFromAddress FROM other.EmailEvents WHERE NetworkMessageId = :value LIMIT 100',
    'SELECT * FROM EmailEvents WHERE NetworkMessageId = :value LIMIT 100',
    'SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = :value OR 1=1 LIMIT 100',
    'SELECT SLEEP(5) FROM EmailEvents WHERE NetworkMessageId = :value LIMIT 100',
    'SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = :value LIMIT 100 FOR UPDATE',
])
def test_invalid_sql_contracts_rejected(memory, sql):
    t = deepcopy(sender_template(memory))
    t['sql'] = sql
    with pytest.raises(ValueError):
        validate_template(t, memory.document['schema_snapshot'])


def test_provenance_status_and_no_runtime_evidence(memory):
    audit = json.loads((PACKAGE / 'excytin/build_audit.json').read_text())
    assert audit['source']['split'] == 'train'
    assert audit['source']['aligned_episodes'] == 237
    runtime = json.dumps(memory.document)
    assert 'episode_id' not in runtime and 'attempt_id' not in runtime
    for s in memory.snippets.values():
        for t in s['steps'][0]['executor_sql_templates']:
            evidence = audit['snippets'][s['id']]['templates'][t['id']]
            assert evidence['support']
            assert (t['status'] == 'active') == (evidence['episodes'] >= 2 and evidence['incidents'] >= 2)
            validate_template(t, memory.document['schema_snapshot'])


def test_runtime_catalog_integrity(memory):
    doc = deepcopy(memory.document)
    doc['snippets'][0]['intent'] = 'changed'
    with pytest.raises(ValueError, match='digest'):
        SnippetMemory(doc)


def test_large_in_batches_preserve_every_value(memory):
    s = memory.snippets['proc_header_mail_sender']
    t = s['steps'][0]['executor_sql_templates'][0]
    values = [f'<example-{i}@invalid>' for i in range(109)]
    args = dict(snippet_id=s['id'], template_id=t['id'], kind='internet_message_id',
                bindings={'value': values}, schema=memory.document['schema_snapshot'], include_candidates=True)
    batches = memory.prepare_batches(**args)
    assert [len(b['params']) for b in batches] == [50, 50, 9]
    assert [v for b in batches for v in b['params']] == values
    with pytest.raises(ValueError):
        memory.prepare_batches(**{**args, 'bindings': {'value': values + [None]}})
    with pytest.raises(ValueError):
        memory.prepare_batches(**{**args, 'bindings': {'value': values * 10}})


def test_schema_snapshot_tampering_is_detected(memory):
    doc = deepcopy(memory.document)
    doc['schema_snapshot']['EmailEvents']['SenderFromAddress'] = 'changed'
    with pytest.raises(ValueError, match='digest'):
        SnippetMemory(doc)
