from __future__ import annotations

from pm_fakes import approve

import json

import pytest
import sqlglot

from src.memory.longterm_memory.procedural_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import (
    build_bundle,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.evaluate import (
    CASES_PATH,
    check_sql,
    evaluate_case,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.mining import (
    mine,
    value_filters,
)
from src.workflow.context import ExcytinBenchSnapshotRepository


def record(repo, ep, incident, sequence, sql, observation, status="rows_returned"):
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="mysql")
    table = next(tree.find_all(exp.Table)).name
    fields = next(t for t in repo.semantic["tables"] if t["name"] == table)["fields"]
    where = tree.args.get("where")
    columns = sorted({c.name for c in where.find_all(exp.Column)}) if where else []
    projected = {p.name for p in tree.expressions if isinstance(p, exp.Column)}
    return {
        "episode_id": ep,
        "incident": incident,
        "attempt_id": f"{ep}:{sequence}",
        "sequence": sequence,
        "status": status,
        "error_type": "",
        "table": table,
        "fields": columns,
        "output_fields": [f["field_id"] for f in fields if f["name"] in projected],
        "tree": tree,
        "sql": sql,
        "valid": True,
        "observation": observation,
    }


def test_miner_finds_value_link_across_intervening_successful_query():
    repo = ExcytinBenchSnapshotRepository()
    rows = []
    for i in range(2):
        rows.extend(
            [
                record(
                    repo,
                    f"ep{i}",
                    f"inc{i}",
                    1,
                    "SELECT AlertId FROM AlertInfo WHERE Title='secret-source'",
                    "[('private-alert-value',)]",
                ),
                record(
                    repo,
                    f"ep{i}",
                    f"inc{i}",
                    2,
                    "SELECT NetworkMessageId FROM EmailEvents WHERE SenderFromAddress='secret-address'",
                    "[('unrelated-message',)]",
                ),
                record(
                    repo,
                    f"ep{i}",
                    f"inc{i}",
                    3,
                    "SELECT DeviceName FROM AlertEvidence WHERE AlertId='private-alert-value'",
                    "[('private-device',)]",
                ),
            ]
        )
    procedures, audit = mine(rows, repo.semantic)
    pivot = next(
        p for p in procedures if p["name"] == "AlertInfo → AlertEvidence (alert_id)"
    )
    assert pivot["status"] == "active"
    assert all(
        token not in json.dumps(procedures)
        for token in (
            "private-alert-value",
            "private-device",
            "secret-address",
            "secret-source",
        )
    )
    support = next(a for a in audit["procedures"] if a["name"] == pivot["name"])
    assert all(len(e["attempt_ids"]) == 2 for e in support["evidence"])


def test_repetitions_in_one_question_cannot_create_a_procedure():
    repo = ExcytinBenchSnapshotRepository()
    rows = [
        record(
            repo,
            "one-question",
            "one-incident",
            i,
            "SELECT AlertId FROM AlertInfo WHERE Title='a'",
            "[('alert-a',)]",
        )
        for i in range(1, 12)
    ]
    assert mine(rows, repo.semantic)[0] == []
    rows.append(
        record(
            repo,
            "second-question",
            "one-incident",
            1,
            "SELECT AlertId FROM AlertInfo WHERE Title='b'",
            "[('alert-b',)]",
        )
    )
    procedures, _ = mine(rows, repo.semantic)
    assert procedures and all(p["status"] == "candidate" for p in procedures)


def test_or_and_negation_do_not_establish_identity_links():
    for sql in [
        "SELECT * FROM AlertEvidence WHERE AlertId='secret-value' OR Title='x'",
        "SELECT * FROM AlertEvidence WHERE NOT AlertId='secret-value'",
    ]:
        assert value_filters(sqlglot.parse_one(sql)) == []


def test_build_audit_accounts_for_every_attempt_and_separates_provenance():
    knowledge, audit = build_bundle()
    assert sum(audit["result_counts"].values()) == audit["attempt_count"] == 1605
    covered = {
        i
        for p in audit["procedures"]
        if p["status"] == "active"
        for e in p["evidence"]
        for i in e["attempt_ids"]
    }
    uncovered = {a["attempt_id"] for a in audit["uncovered_attempts"]}
    assert not covered & uncovered
    assert len(covered | uncovered) == 1605
    assert {p["family"] for p in audit["procedures"]} == {
        "lookup",
        "pivot",
        "chain",
        "sample",
        "schema",
        "error_repair",
        "empty_repair",
    }
    for p in audit["procedures"]:
        assert p["episode_count"] >= 2
        assert p["status"] == "candidate" or p["incident_count"] >= 2
    assert all(
        set(p)
        == {"name", "status", "goal", "input_slots", "operations", "semantic_refs"}
        for p in knowledge["procedures"]
    )


def test_curation_survives_rebuild_without_changing_structure(tmp_path):
    name = "根据告警标识查证告警证据"
    path = tmp_path / "curation.json"
    path.write_text(json.dumps({name: {"goal": "从已核实告警 ID 继续补查实体信息。"}}))
    knowledge, audit = build_bundle(curation_path=path)
    assert (
        next(p for p in knowledge["procedures"] if p["name"] == name)["goal"]
        == "从已核实告警 ID 继续补查实体信息。"
    )
    assert audit["curated_names"] == [name]
    path.write_text(json.dumps({name: {"status": "active"}}))
    with pytest.raises(ValueError):
        build_bundle(curation_path=path)


def test_partial_step_evaluation_hides_answers_and_detects_repeated_source():
    cases = json.loads(CASES_PATH.read_text())
    case = next(c for c in cases if c["case_id"] == "alert_partial")
    knowledge = json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())
    calls = []

    def llm(system, prompt):
        calls.append(prompt)
        assert "host-pm-01" not in prompt  # expected answer stays private
        if len(calls) == 1:
            assert "operations" not in prompt
            return '["通过告警标识从告警概要关联告警证据"]'
        if len(calls) in (2, 3):
            assert "operations" in prompt
            return approve(prompt)
        assert "operations" in prompt
        return "sql: SELECT DeviceName FROM AlertEvidence WHERE AlertId = 'a-pm-01'"

    assert evaluate_case(case, knowledge, llm)["passed"]
    assert len(calls) == 4
    assert not check_sql(case, "SELECT AlertId FROM AlertInfo WHERE AlertId='a-pm-01'")[
        "passed"
    ]
    assert not check_sql(
        case, "SELECT DeviceName FROM AlertEvidence WHERE AlertId='a-pm-01' OR 1=1"
    )["passed"]


def test_scope_checks_reject_pid_only_and_wrong_process_role():
    cases = json.loads(CASES_PATH.read_text())
    case = next(c for c in cases if c["case_id"] == "pid_reuse_scope")
    assert not check_sql(
        case, "SELECT ProcessCommandLine FROM DeviceProcessEvents WHERE ProcessId=4242"
    )["passed"]
    case = next(c for c in cases if c["case_id"] == "parent_process_partial")
    assert not check_sql(
        case, "SELECT ProcessCommandLine FROM DeviceProcessEvents WHERE ProcessId=4242"
    )["passed"]
    assert check_sql(
        case,
        "SELECT InitiatingProcessCommandLine FROM DeviceProcessEvents WHERE ProcessId=4242",
    )["passed"]
