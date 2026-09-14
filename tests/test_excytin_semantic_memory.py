from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
from jsonschema import Draft202012Validator

from src.memory.longterm_memory.semantic_memory.excytin_bench.build_knowledge import (
    DEFAULT_SOURCE, build_knowledge, read_schemas,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import retrieval_view

KNOWLEDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)
TABLE_KEYS = {"table_id", "name", "description", "description_zh", "field"}
FIELD_KEYS = {"field_id", "name", "data_type", "description", "description_zh"}


def load_knowledge() -> dict:
    return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))


def test_snapshot_is_scenario_independent_and_internally_consistent() -> None:
    knowledge = load_knowledge()
    policy = knowledge["generation_policy"]
    assert policy["scenario_independent"] is True
    assert policy["contains_log_values"] is False
    assert policy["contains_incident_labels"] is False
    assert not re.search(r"incident[_ -]?\d+", KNOWLEDGE_PATH.read_text(encoding="utf-8"), re.I)
    schema = json.loads(KNOWLEDGE_PATH.with_name("knowledge.schema.json").read_text())
    Draft202012Validator(schema).validate(knowledge)
    tables = knowledge["tables"]
    fields = [field for table in tables for field in table["field"]]
    catalog = knowledge["catalog"]
    assert knowledge["format_version"] == "2.0"
    assert catalog["source_schema_files"] == 365
    assert catalog["table_count"] == len(tables) == 60
    assert catalog["field_count"] == len(fields) == 2324
    assert catalog["join_key_count"] == len(knowledge["join_keys"]) == 18
    assert len({table["table_id"] for table in tables}) == len(tables)
    assert len({field["field_id"] for field in fields}) == len(fields)
    assert all(set(table) == TABLE_KEYS for table in tables)
    assert all(set(field) == FIELD_KEYS for field in fields)
    field_ids = {field["field_id"] for field in fields}
    for key in knowledge["join_keys"]:
        assert key["field_count"] == len(key["field_ids"])
        assert set(key["field_ids"]) <= field_ids


def test_descriptions_explain_table_selection_and_field_roles() -> None:
    tables = {table["name"]: table for table in load_knowledge()["tables"]}
    for table in tables.values():
        assert len(table["description"].split()) >= 30
        assert re.search(r"[\u4e00-\u9fff]", table["description_zh"])
        for field in table["field"]:
            assert len(field["description"].split()) >= 8
            assert re.search(r"[\u4e00-\u9fff]", field["description_zh"])
            assert "representing text" not in field["description"]
    process = {field["name"]: field for field in tables["DeviceProcessEvents"]["field"]}
    assert "parent" in process["InitiatingProcessParentId"]["description"].lower()
    assert "command" in process["ProcessCommandLine"]["description"].lower()
    assert "row" in process["rn"]["description"].lower()
    assert "click" in tables["EmailUrlInfo"]["description"].lower()
    assert "aggregate" in tables["AZFWNetworkRuleAggregation"]["description"].lower()
    assert "automated investigation" in tables["SecurityAlert"]["description"].lower()


def test_runtime_metadata_does_not_mutate_public_objects() -> None:
    knowledge = load_knowledge()
    before = deepcopy(knowledge)
    view = retrieval_view(knowledge)
    assert knowledge == before
    fields = {field["field_id"]: field for table in view["tables"] for field in table["fields"]}
    for key in knowledge["join_keys"]:
        assert all(fields[fid]["join_key"] == key["name"] for fid in key["field_ids"])


@pytest.mark.skipif(not DEFAULT_SOURCE.exists(), reason="Local schema archive unavailable")
def test_all_tables_and_fields_match_meta_union_and_rebuild() -> None:
    knowledge = load_knowledge()
    raw, _ = read_schemas(DEFAULT_SOURCE)
    for table in knowledge["tables"]:
        expected = {key for variant in raw[table["name"]] for key in variant}
        assert {field["name"] for field in table["field"]} == expected
    rebuilt = build_knowledge(DEFAULT_SOURCE)
    knowledge.pop("generated_at")
    rebuilt.pop("generated_at")
    assert rebuilt == knowledge


def test_neo4j_import_uses_v2_properties_and_keeps_join_edges() -> None:
    from unittest.mock import MagicMock, patch
    from src.memory.longterm_memory.semantic_memory.excytin_bench.import_neo4j import (
        import_knowledge, load_knowledge as load_for_import,
    )

    knowledge = load_for_import(KNOWLEDGE_PATH)
    session = MagicMock()
    session.run.return_value.single.return_value = {
        'tables': 60, 'fields': 2324, 'join_keys': 18, 'mappings': 0,
    }
    with patch('src.memory.longterm_memory.semantic_memory.excytin_bench.import_neo4j.GraphDatabase') as graph:
        graph.driver.return_value.session.return_value.__enter__.return_value = session
        import_knowledge(uri='bolt://unused', username='test', password='test', database='neo4j', knowledge=knowledge)
    table_rows, field_rows, mappings = [], [], []
    for call in session.run.call_args_list:
        query = call.args[0]
        if 'CREATE (table:LogTable' in query:
            table_rows.extend(call.kwargs['rows'])
        elif 'CREATE (field:Field' in query:
            field_rows.extend(call.kwargs['rows'])
        elif 'CREATE (field)-[mapping:MAPS_TO_JOIN_KEY]' in query:
            mappings.extend(call.kwargs['rows'])
    assert len(table_rows) == 60
    assert len(field_rows) == 2324
    assert all(set(row) == (TABLE_KEYS - {'field'}) | {'catalog_id'} for row in table_rows)
    assert all(set(row) == FIELD_KEYS | {'catalog_id', 'table_id'} for row in field_rows)
    assert len(mappings) == sum(key['field_count'] for key in knowledge['join_keys'])
    assert {row['field_id'] for row in mappings} <= {row['field_id'] for row in field_rows}


def test_agent_context_exposes_descriptions_and_only_five_keys() -> None:
    from src.workflow.context import ExcytinBenchSnapshotRepository

    view = ExcytinBenchSnapshotRepository().retrieve_semantic(
        'DeviceProcessEvents ProcessCommandLine', table_limit=1, field_limit=5,
    )
    table = view['tables'][0]
    assert table['name'] == 'DeviceProcessEvents'
    assert set(table) == TABLE_KEYS
    assert all(set(field) == FIELD_KEYS for field in table['field'])
    assert 'ProcessCommandLine' in {field['name'] for field in table['field']}
