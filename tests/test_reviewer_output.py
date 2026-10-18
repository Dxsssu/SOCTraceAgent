import json

import pytest
import yaml

from src.schema.reviewer_output import parse_review, evidence_result, SUMMARY_MAX_CHARS


def review(**overrides):
    return dict(summary="关联进程已定位", facts=[{
        "host_id": "host-01", "pid": "0042", "process_guid": "123456789012345678901234567890",
        "timestamp": "2026-10-17T08:30:00.123456Z", "path": "C:\\Program Files\\Agent\\a.exe",
    }], limitations=[]) | overrides


def test_evidence_over_80_chars_retains_join_keys_and_precision():
    value = review()
    assert len(json.dumps(value["facts"])) > 80
    parsed = parse_review(yaml.safe_load(yaml.safe_dump(value, allow_unicode=True)))
    assert parsed == value
    assert json.loads(json.dumps(parsed)) == value
    parsed['facts'][0]['pid'] = 'changed'
    assert value['facts'][0]['pid'] == '0042'


def test_summary_limit_does_not_truncate_facts():
    value = review(summary="中" * (SUMMARY_MAX_CHARS - 3) + "a! ")
    assert parse_review(value)["summary"] == value["summary"].strip()
    with pytest.raises(ValueError, match="move details into facts"):
        parse_review(value | {"summary": value["summary"] + "x"})


@pytest.mark.parametrize("value", [
    {}, {"summary": "old summary only"}, review(source={"execution_id": "invented"}),
    review(summary=[]), review(summary=" "), review(facts="found"),
    review(facts=[{}]), review(facts=[{"pid": [1, 2]}]), review(facts=[{1: "pid"}]),
    review(facts=[{"pid": float('nan')}]), review(limitations="none"), review(limitations=[""]),
])
def test_invalid_evidence_is_rejected(value):
    with pytest.raises(ValueError):
        parse_review(value)


def test_nested_schema_fact_error_identifies_location_without_losing_rows():
    with pytest.raises(ValueError) as error:
        parse_review(review(facts=[{"TABLE_NAME": "Events", "COLUMN_NAME": ["Id", "Time"]}]))
    assert "facts[0]['COLUMN_NAME'] has type list" in str(error.value)
    rows = [{"TABLE_NAME": "Events", "COLUMN_NAME": column} for column in ("Id", "Time")]
    assert parse_review(review(facts=rows))["facts"] == rows


def test_observed_json_cell_can_be_preserved_as_text():
    cell = '[{"pid":"0042","parent":"0001"}]'
    result = parse_review(review(facts=[{"Entities": cell}]))
    assert result["facts"][0]["Entities"] == cell


@pytest.mark.parametrize("status,success", [("empty_result", True), ("error", False)])
def test_runtime_preserves_scope_and_drops_claims_from_unsuccessful_queries(status, success):
    record = {"execution_id": "E7", "result_status": status, "query_success": success}
    evidence = evidence_result(review(), record)
    assert evidence['facts'] == []
    assert evidence['source'] == record
    assert evidence['limitations']


def test_runtime_marks_truncation_even_when_reviewer_omits_it():
    evidence = evidence_result(review(), {"execution_id": "E9", "observation_truncated": True,
                                         "original_observation_chars": 20000})
    assert evidence['facts'] == review()['facts']
    assert '截断' in evidence['limitations'][0]
    assert evidence['source']['execution_id'] == 'E9'


def test_failed_sql_diagnostic_survives_a_vague_summary():
    record = {"execution_id": "E2", "query_success": False,
              "observation": "Unknown column 'ProcessGuid'"}
    evidence = evidence_result(review(summary="查询失败"), record)
    assert evidence['source']['error'] == record['observation']
    assert evidence['facts'] == []
