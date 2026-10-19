"""Ranking must prefer flows whose entry step consumes the supplied evidence."""
from types import SimpleNamespace

from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import digest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.session import ProceduralPlanningSession
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import TrajectorySnippetStore


def make_session():
    # Equal output coverage, wording and step count isolate input evidence coverage.
    template = {
        'id': 'sql_test',
        'sql': 'SELECT AlertId FROM Evidence WHERE Title = :p1 LIMIT 10',
        'inputs': {'p1': {'field': 'Title', 'operator': 'EQ', 'type': 'string'}},
        'outputs': ['AlertId'], 'schema_fields': {'Evidence': ['Title', 'AlertId']},
        'row_cap': 10, 'scope': 'filtered',
    }
    def flow(sid, fields, trigger_kind='title'):
        return {
            'id': sid, 'name': '定位账户关联告警', 'intent': '定位账户关联告警',
            'kind': 'evidence_chain', 'status': 'candidate', 'caution': [],
            'trigger_entity': {'concept': 'Title' if trigger_kind == 'title' else 'Account',
                               'kind': trigger_kind},
            'required_inputs': [
                {'step': 's1', 'parameter': field, 'table': 'Evidence', 'field': field}
                for field in fields
            ],
            'steps': [{'id': sid, 'template_id': 'sql_test', 'planner_guide': '定位关联证据',
                       'outputs': {'AlertId': {'entity_concept': 'Alert_ID'}}}
                      for sid in ('s1', 's2')],
            'bindings': [],
        }
    doc = {'format_version': 'trajectory-snippets-2', 'templates': {'sql_test': template},
           'snippets': [flow('a_title_only', ['Title']),
                        flow('b_wrong_account_kind', ['Title'], 'object_id'),
                        flow('z_title_and_upn', ['Title', 'AccountUpn'])]}
    doc['catalog_id'] = digest(doc)
    memory = SimpleNamespace(
        schema={'Evidence': {'Title': 'text', 'AccountUpn': 'text', 'AlertId': 'text'}},
        aliases={},
        concepts={
            'Title': {'fields': [{'table': 'Evidence', 'field': 'Title', 'kind': 'title'}]},
            'Account': {'fields': [{'table': 'Evidence', 'field': 'AccountUpn', 'kind': 'upn'}]},
            'Alert_ID': {'fields': [{'table': 'Evidence', 'field': 'AlertId', 'kind': 'alert_id'}]},
        },
    )
    def queries(query):
        return {'known': [SimpleNamespace(**x) for x in query['known']],
                'wanted': [SimpleNamespace(concept='Alert_ID', kind=None)],
                'intent': '定位账户关联告警'}
    semantic = SimpleNamespace(memory=memory, queries=queries, default_intent='')
    return ProceduralPlanningSession(semantic, TrajectorySnippetStore(doc))


def ids(session, known):
    result = session.retrieve({'known': known, 'wanted': ['Alert_ID'],
                               'intent': '定位账户关联告警'})
    return [candidate['id'] for candidate in result['candidates']]


def test_two_known_inputs_outrank_title_only_flow():
    assert ids(make_session(), [{'concept': 'Title', 'kind': 'title'},
                                {'concept': 'Account', 'kind': 'upn'}]) == [
        'z_title_and_upn', 'a_title_only', 'b_wrong_account_kind']


def test_wrong_kind_does_not_count_as_matching_known_input():
    # With a known object ID, the upn field cannot earn coverage for that identity.
    assert ids(make_session(), [{'concept': 'Title', 'kind': 'title'},
                                {'concept': 'Account', 'kind': 'object_id'}]) == [
        'b_wrong_account_kind', 'a_title_only', 'z_title_and_upn']


def test_single_known_keeps_equal_flow_order_and_duplicates_add_no_bonus():
    assert ids(make_session(), [{'concept': 'Title', 'kind': 'title'}]) == [
        'a_title_only', 'b_wrong_account_kind', 'z_title_and_upn']
    known = [{'concept': 'Title', 'kind': 'title'}, {'concept': 'Account', 'kind': 'upn'}]
    assert ids(make_session(), known) == ids(make_session(), known + [known[0]])
