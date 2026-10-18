from copy import deepcopy

import yaml

from src.schema.evidence_view import dump_evidence_prompt


def payload(facts):
    return {"question": "Find the process", "itt": [{"id": "G1", "children": [
        {"id": "E1", "children": [{"id": "T1", "objective": "query", "execution_code": "SELECT ...",
            "execution_result": {"summary": "records", "facts": facts,
                "source": {"execution_id": "E1", "observation_truncated": True},
                "limitations": ["Only visible records"]}}]}]}]}


def result(value):
    return value["itt"][0]["children"][0]["children"][0]["execution_result"]


def test_compact_facts_roundtrip_preserves_scalar_types_rows_and_tree():
    rows = [{"DeviceName": "host", "ProcessId": "0001", "Timestamp": "2024-01-01T00:00:00.123456Z",
             "optional": None, "empty": "", "flag": False, "count": i, "literal": "null"} for i in range(30)]
    value = payload(rows)
    before = deepcopy(value)
    text = dump_evidence_prompt(value, compact=True)
    decoded = yaml.safe_load(text)
    table = result(decoded)["facts"]
    assert isinstance(table, dict)
    result(decoded)["facts"] = [dict(zip(table["columns"], row)) for row in table["rows"]]
    decoded.pop("facts_encoding")
    assert decoded == before == value
    assert len(text.encode()) < 0.9 * len(dump_evidence_prompt(value).encode())


def test_heterogeneous_and_small_facts_keep_original_representation():
    for rows in ([{"a": None}, {"b": ""}, {"a": "value"}], [{"a": 1}],
                 [{"a": 1}, {"a": 2}, {"a": 3}]):
        value = payload(rows)
        assert dump_evidence_prompt(value, compact=True) == dump_evidence_prompt(value)
