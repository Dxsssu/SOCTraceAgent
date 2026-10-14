from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import ValidationError

from src.memory.longterm_memory.episodic_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge import (
    build_bundle,
    entry_id,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.import_neo4j import (
    graph_rows,
    load_knowledge,
    validate_knowledge,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.retrieval import (
    RecoveryIndex,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    DEFAULT_QUESTIONS_DIR,
    DEFAULT_TRAJECTORIES,
    load_jsonl,
    load_train_questions,
    map_trajectories_to_train,
)


def snapshot():
    return load_knowledge(DEFAULT_KNOWLEDGE_PATH)


def test_snapshot_is_three_fields_without_old_episodes():
    k = snapshot()
    assert "episodes" not in k
    assert len(k["entries"]) == 21
    assert all(set(e) == {"background", "error", "repair"} for e in k["entries"])
    assert k["catalog"]["error_attempt_count"] == 81
    assert k["catalog"]["supported_error_count"] == 70
    assert k["catalog"]["excluded_error_count"] == 11
    text = json.dumps(k)
    for value in ["170.54.121.63", "raphaelt@", "submitted_answer", "REPAIRED_BY"]:
        assert value not in text


def test_every_failure_has_exactly_one_audit_disposition():
    k = snapshot()
    audit = json.loads(DEFAULT_KNOWLEDGE_PATH.with_name("build_audit.json").read_text())
    assert {x["entry_id"] for x in audit["entries"]} == {
        entry_id(e) for e in k["entries"]
    }
    sources = [s for e in audit["entries"] for s in e["sources"]]
    ids = [s["attempt_id"] for s in sources + audit["excluded"]]
    assert len(ids) == len(set(ids)) == 81
    for source in sources:
        assert source["recovery_result_status"] in {"rows_returned", "empty_result"}
        assert not source["recovery_sql_template"].startswith(("DESCRIBE", "SHOW"))
    assert audit["trajectory_alignment"]["source_key_mismatch_count"] == 236


@pytest.mark.skipif(
    not DEFAULT_TRAJECTORIES.exists(), reason="Training data not installed"
)
def test_rebuild_and_train_alignment():
    rebuilt, audit = build_bundle()
    saved = snapshot()
    rebuilt.pop("generated_at")
    saved.pop("generated_at")
    assert rebuilt == saved
    assert audit == json.loads(
        DEFAULT_KNOWLEDGE_PATH.with_name("build_audit.json").read_text()
    )
    mapped = map_trajectories_to_train(
        load_jsonl(DEFAULT_TRAJECTORIES), load_train_questions(DEFAULT_QUESTIONS_DIR)
    )
    assert len(mapped) == 237


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "counts", "test"])
def test_invalid_memory_rejected(mutation):
    k = deepcopy(snapshot())
    if mutation == "extra":
        k["entries"][0]["sql"] = "SELECT 1"
    elif mutation == "duplicate":
        k["entries"][1] = k["entries"][0]
    elif mutation == "counts":
        k["catalog"]["error_attempt_count"] += 1
    else:
        k["generation_policy"]["source_split"] = "test"
    with pytest.raises((ValueError, ValidationError)):
        validate_knowledge(k)


def test_error_vectors_and_background_disambiguate_identical_errors():
    error = "ProgrammingError: Unknown column 'Description' in 'where clause'"
    index = RecoveryIndex(snapshot())
    alert = index.retrieve(error, background="SELECT Description FROM AlertInfo")
    signin = index.retrieve(error, background="SELECT Description FROM SigninLogs")
    assert len(alert) == len(signin) == 1
    assert "Title" in alert[0]["repair"]
    assert "RiskEventTypes" in signin[0]["repair"]
    assert alert[0] != signin[0]
    assert index.retrieve(error, background="SELECT Description FROM EmailEvents") == []


def test_current_failing_sql_overrides_broad_table_hints():
    index = RecoveryIndex(snapshot())
    error = "Unknown column 'Description' in 'where clause'"
    hits = index.retrieve(
        error,
        background="SELECT Description FROM `incident_db`.`SigninLogs`",
        table_hints=["AlertInfo"],
    )
    assert len(hits) == 1 and "SigninLogs" in hits[0]["background"]


@pytest.mark.parametrize(
    "error,background",
    [
        ("", "SELECT * FROM AlertInfo"),
        ("[]", "SELECT * FROM AlertInfo"),
        ("ConnectionError: timeout", "SELECT * FROM AlertInfo"),
        ("Unknown column 'InventedField' in 'where clause'", "SELECT * FROM AlertInfo"),
        ("Unknown column 'Description' in 'where clause'", ""),
    ],
)
def test_no_relevant_recovery_means_no_result(error, background):
    assert RecoveryIndex(snapshot()).retrieve(error, background=background) == []


def test_returned_entries_are_isolated_and_limit_honored():
    index = RecoveryIndex(snapshot())
    error = "Unknown column 'SenderIP' in 'field list'"
    hits = index.retrieve(error, background="SELECT SenderIP FROM EmailEvents", limit=1)
    assert len(hits) == 1 and "SenderIPv4" in hits[0]["repair"]
    hits[0]["repair"] = "mutated"
    assert (
        index.retrieve(error, background="SELECT SenderIP FROM EmailEvents")[0][
            "repair"
        ]
        != "mutated"
    )
    assert (
        index.retrieve(error, background="SELECT SenderIP FROM EmailEvents", limit=0)
        == []
    )


def test_graph_rows_preserve_public_entries_without_attempt_graph():
    k = snapshot()
    rows = graph_rows(k)
    assert len(rows) == len(k["entries"])
    assert [
        {key: row[key] for key in ("background", "error", "repair")} for row in rows
    ] == k["entries"]
    assert all("attempts" not in row and "transitions" not in row for row in rows)


def test_all_supported_failures_retrieve_their_reviewed_recovery():
    index = RecoveryIndex(snapshot())
    audit = json.loads(DEFAULT_KNOWLEDGE_PATH.with_name('build_audit.json').read_text())
    for group in audit['entries']:
        for source in group['sources']:
            hits = index.retrieve(source['error'], background=source['failed_sql_template'])
            assert group['entry_id'] in {entry_id(e) for e in hits}, source['attempt_id']


def test_alias_scopes_disambiguate_join_errors():
    index = RecoveryIndex(snapshot())
    sql = "SELECT s.Description FROM AlertInfo a JOIN SigninLogs s ON a.Title=s.UserPrincipalName"
    hits = index.retrieve("(1054) Unknown column 's.Description' in 'field list'", background=sql, limit=1)
    assert len(hits) == 1 and "SigninLogs" in hits[0]["background"]
    assert index.retrieve("Unknown column 'Description' in 'field list'", background=sql) == []
    assert index.retrieve("Unknown column 'x.Description' in 'field list'", background=sql) == []


def test_literals_do_not_supply_fake_tables_and_case_is_normalized():
    index = RecoveryIndex(snapshot())
    assert index.retrieve("Unknown column 'Description' in 'field list'",
                          background="SELECT Description, 'FROM SigninLogs' FROM EmailEvents") == []
    hits = index.retrieve("Unknown column 's.Description' in 'field list'",
                          background="SELECT s.Description FROM `db`.`signinlogs` s", limit=1)
    assert len(hits) == 1 and "SigninLogs" in hits[0]["background"]


def test_equivalent_repairs_are_deduplicated_and_exact_clause_ranks_first():
    index = RecoveryIndex(snapshot())
    hits = index.retrieve("Unknown column 'AadDeviceId' in 'field list'",
                          background="SELECT AadDeviceId FROM DeviceProcessEvents")
    assert len(hits) == 1
    assert "field list" in hits[0]["error"]
