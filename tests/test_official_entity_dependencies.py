import json
import pytest
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.entity_identifiers import entity_identifier_values
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import dependency_candidates


def test_entity_role_and_reference_resolution_without_nested_role_leak():
    rows=[{'$id':'1','Type':'account','Name':'actor','AadUserId':'actor-id'},
          {'$id':'2','Type':'process','CommandLine':'powershell.exe > AdminTicket.txt',
           'ImageFile':{'$id':'3','Type':'file','Name':'powershell.exe'}},
          {'$ref':'3'}]
    assert entity_identifier_values(json.dumps(rows),'account','Name')==['actor']
    assert entity_identifier_values(json.dumps(rows),'file','Name')==['powershell.exe']
    assert entity_identifier_values(json.dumps(rows),'process','ExtractedFileName')==['powershell.exe','adminticket.txt']


def test_composite_identity_and_email_do_not_substitute_upn():
    rows=[{'Type':'account','Name':'Jordan P','UPNSuffix':'example.test','UserPrincipalName':'different@example.test'},
          {'Type':'process','ProcessId':'88','CreatedTimeUtc':'2024-01-01T00:00:00Z','CommandLine':'CURL example.test'}]
    assert entity_identifier_values(rows,'account','Email')==['Jordan P@example.test']
    assert entity_identifier_values(rows,'process','ProcessId__CreatedTimeUtc__CommandLine')==['88__2024-01-01t00:00:00z__curl example.test']


@pytest.mark.parametrize('raw', ['[{"Type":"file","Name":"C:\\Windows"}]', '[{"Type":"process","CommandLine":"""cmd.exe"""}]'])
def test_corrupted_database_json_is_never_repaired_or_treated_as_empty(raw):
    with pytest.raises(ValueError,match='dependency_entity_json_invalid'):
        entity_identifier_values(raw,'process','ExtractedFileName')


def test_invalid_references_rejected():
    with pytest.raises(ValueError,match='reference_invalid'):
        entity_identifier_values([{'$ref':'missing'}],'account','Name')


def test_typed_bridge_candidates_preserve_all_observed_values():
    edge={'from_column':'Entities','transform':'entity_identifier','entity_type':'account','identifier_fields':'Sid'}
    rows=[{'Entities':json.dumps([{'Type':'account','Sid':'SID-A'},{'Type':'account','Sid':'SID-B'}, {'Type':'account','Sid':'SID-A'}])}]
    assert dependency_candidates(edge,rows)==['SID-A','SID-B']
    with pytest.raises(ValueError,match='contract_missing'):
        dependency_candidates({'from_column':'Entities','transform':'entity_identifier'},rows)
    with pytest.raises(ValueError,match='unsupported_dependency_transform'):
        dependency_candidates({'from_column':'Entities','transform':'guess'},rows)


def test_resource_group_is_derived_from_observed_resource_id():
    entities=[{'Type':'azure-resource','ResourceId':'/subscriptions/731adb71-b11c-4bbf-850b-57989b350e96/resourceGroups/ExampleRG/providers/Microsoft.Compute/virtualMachines/a'}]
    assert entity_identifier_values(entities,'azure-resource','ResourceGroup')==['examplerg']
    assert entity_identifier_values(entities,'azure-resource','SubscriptionId')==['731adb71-b11c-4bbf-850b-57989b350e96']
