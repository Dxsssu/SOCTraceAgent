from __future__ import annotations

import json
from pathlib import Path
import re


KNOWLEDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)


def load_knowledge() -> dict:
    return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))


def test_snapshot_is_scenario_independent_and_internally_consistent() -> None:
    knowledge = load_knowledge()
    policy = knowledge["generation_policy"]
    assert policy["scenario_independent"] is True
    assert policy["contains_log_values"] is False
    assert policy["contains_incident_labels"] is False
    assert not re.search(r"incident[_ -]?\d+", KNOWLEDGE_PATH.read_text(encoding="utf-8"), re.I)

    tables = knowledge["tables"]
    fields = [field for table in tables for field in table["fields"]]
    join_keys = knowledge["join_keys"]
    catalog = knowledge["catalog"]

    assert catalog["source_schema_files"] == 365
    assert catalog["table_count"] == len(tables) == 60
    assert catalog["field_count"] == len(fields) == 2324
    assert catalog["join_key_count"] == len(join_keys) == 18
    assert len({table["table_id"] for table in tables}) == len(tables)
    assert len({field["field_id"] for field in fields}) == len(fields)

    field_by_id = {field["field_id"]: field for field in fields}
    for join_key in join_keys:
        assert join_key["field_count"] == len(join_key["field_ids"])
        for field_id in join_key["field_ids"]:
            assert field_by_id[field_id]["join_key"] == join_key["name"]


def test_representative_schemas_come_from_meta_union() -> None:
    knowledge = load_knowledge()
    table_by_name = {table["name"]: table for table in knowledge["tables"]}

    assert table_by_name["AlertInfo"]["field_count"] == 12
    assert table_by_name["AlertEvidence"]["field_count"] == 40
    assert table_by_name["OfficeActivity"]["field_count"] == 139

    process_fields = {field["name"] for field in table_by_name["DeviceProcessEvents"]["fields"]}
    assert {"TimeGenerated", "DeviceId", "ProcessCommandLine", "SHA256"} <= process_fields
    assert "rn" in process_fields
    assert next(
        field for field in table_by_name["DeviceProcessEvents"]["fields"] if field["name"] == "rn"
    )["is_technical"] is True
