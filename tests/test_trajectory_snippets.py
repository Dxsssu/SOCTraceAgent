from copy import deepcopy
import pytest
from sqlglot import parse_one
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
    normalize_sql,
    compile_sql,
    observation_rows,
    verify_link,
    digest,
    safe_text,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import (
    TrajectorySnippetStore,
)

SCHEMA = {
    "Mail": {"MailId": "text", "Sender": "text", "Subject": "text"},
    "Links": {"MailId": "text", "Url": "text"},
}


def test_case_value_in_like_pattern_is_removed_from_planner_prose():
    assert (
        "case-host"
        not in safe_text("查询 CASE-HOST 的活动", ["%case-host%"]).casefold()
    )


def test_nested_predicates_bind_in_rendered_sql_order():
    t, b = normalize_sql(
        "SELECT Sender FROM Mail WHERE (MailId='a' AND Sender='b') OR Subject='c'",
        SCHEMA,
    )
    sql, params = compile_sql(t, b, SCHEMA)
    assert params == ("a", "b", "c")
    assert "AND" in sql and "OR" in sql


def test_case_values_are_external_even_with_payload_text():
    t, b = normalize_sql("SELECT Sender FROM Mail WHERE MailId='abc'", SCHEMA)
    key = next(iter(b))
    payload = "x' OR 1=1; DROP TABLE Mail; --"
    sql, params = compile_sql(t, {key: payload}, SCHEMA)
    assert payload not in sql and params == (payload,)


def test_in_lengths_share_one_template():
    a, _ = normalize_sql("SELECT Sender FROM Mail WHERE MailId IN ('a','b')", SCHEMA)
    b, _ = normalize_sql("SELECT Sender FROM Mail WHERE MailId IN ('c')", SCHEMA)
    assert a["id"] == b["id"]


def test_truncated_observation_only_uses_complete_rows():
    assert observation_rows("[('a', 'x'), ('b', 'unterminated ... (Truncated)") == [
        ("a", "x")
    ]
    assert observation_rows(
        "[('a', 'x')] ... (Truncated) ... (Truncated) ... (Truncated)"
    ) == [("a", "x")]
    assert observation_rows("__import__('os').system('bad')") is None


def test_wildcard_fields_checked_against_real_schema():
    t, _ = normalize_sql(
        "SELECT * FROM Mail WHERE MailId='x'", SCHEMA, ["MailId", "Sender"]
    )
    assert t["outputs"] == ["MailId", "Sender"]
    with pytest.raises(ValueError):
        normalize_sql("SELECT Unknown FROM Mail WHERE MailId='x'", SCHEMA)


def chain():
    a, _ = normalize_sql("SELECT MailId FROM Links WHERE Url='x'", SCHEMA)
    b, _ = normalize_sql("SELECT Sender FROM Mail WHERE MailId IN ('y')", SCHEMA)
    s = {
        "id": "s",
        "status": "candidate",
        "steps": [
            {"id": "s1", "template_id": a["id"]},
            {"id": "s2", "template_id": b["id"]},
        ],
        "bindings": [
            {
                "from": "s1",
                "to": "s2",
                "from_column": "MailId",
                "to_field": "MailId",
                "transform": "identity",
            }
        ],
    }
    doc = {
        "format_version": "trajectory-snippets-2",
        "snippets": [s],
        "templates": {a["id"]: a, b["id"]: b},
    }
    doc["catalog_id"] = digest(doc)
    return TrajectorySnippetStore(doc)


def test_chain_binds_actual_observed_value():
    store = chain()
    r = store.prepare_step(
        "s",
        "s2",
        schema=SCHEMA,
        external_inputs={},
        observations={
            "s1": {
                "success": True,
                "rows": [{"MailId": "observed-id"}],
                "truncated": False,
            }
        },
        allow_candidates=True,
    )
    assert r["params"] == ("observed-id",)


@pytest.mark.parametrize(
    "obs",
    [
        {},
        {"s1": {"success": False}},
        {"s1": {"success": True, "truncated": True}},
        {"s1": {"success": True, "rows": [{"MailId": "a"}, {"MailId": "b"}]}},
    ],
)
def test_chain_never_uses_missing_failed_truncated_or_ambiguous_evidence(obs):
    with pytest.raises(ValueError):
        chain().prepare_step(
            "s",
            "s2",
            schema=SCHEMA,
            external_inputs={},
            observations=obs,
            allow_candidates=True,
        )


def test_selection_cannot_invent_an_upstream_value():
    with pytest.raises(ValueError):
        chain().prepare_step(
            "s",
            "s2",
            schema=SCHEMA,
            external_inputs={},
            observations={"s1": {"success": True, "rows": [{"MailId": "a"}]}},
            selections={"s1.MailId": ["invented"]},
            allow_candidates=True,
        )


def test_source_quote_alone_does_not_prove_field_mapping():
    attempts = {
        1: {
            "status": "rows_returned",
            "tree": parse_one("SELECT MailId,Sender FROM Mail", read="mysql"),
            "observation": "[('id1','person')]",
        },
        2: {
            "status": "rows_returned",
            "tree": parse_one(
                "SELECT Sender FROM Mail WHERE MailId='person'", read="mysql"
            ),
        },
    }
    link = {
        "from": 1,
        "to": 2,
        "type": "dataflow",
        "value": "person",
        "source_quote": "person",
        "from_column": "MailId",
        "to_field": "MailId",
        "transform": "identity",
    }
    assert (
        verify_link(link, attempts, SCHEMA)["reason"]
        == "value_not_in_declared_source_column"
    )


def test_value_preserving_list_labels_have_one_canonical_dependency():
    attempts = {
        1: {
            "status": "rows_returned",
            "tree": parse_one("SELECT MailId FROM Mail"),
            "observation": "[('id1',)]",
        },
        2: {
            "status": "rows_returned",
            "tree": parse_one("SELECT Sender FROM Mail WHERE MailId IN ('id1')"),
        },
    }
    link = {
        "from": 1,
        "to": 2,
        "type": "dataflow",
        "value": "id1",
        "source_quote": "id1",
        "from_column": "MailId",
        "to_field": "MailId",
        "transform": "identity",
    }
    direct = verify_link(link, attempts, SCHEMA)
    wrapped = verify_link({**link, "transform": "singleton_list"}, attempts, SCHEMA)
    assert direct["verified"] and direct == wrapped


def test_temporal_boundary_binds_only_the_observed_lower_bound():
    schema = {
        "Clicks": {"Url": "text", "Timestamp": "text"},
        "Processes": {"Timestamp": "text", "Name": "text"},
    }
    source_sql = "SELECT Timestamp FROM Clicks WHERE Url='x'"
    target_sql = "SELECT Name FROM Processes WHERE Timestamp >= '2024-01-01 10:00:00' AND Timestamp <= '2024-01-01 11:00:00'"
    a, _ = normalize_sql(source_sql, schema)
    b, bindings = normalize_sql(target_sql, schema)
    attempts = {
        1: {
            "status": "rows_returned",
            "tree": parse_one(source_sql),
            "observation": "[('2024-01-01 10:00:00',)]",
        },
        2: {
            "status": "rows_returned",
            "tree": parse_one(target_sql),
            "bindings": bindings,
            "input_specs": b["inputs"],
        },
    }
    link = {
        "from": 1,
        "to": 2,
        "type": "dataflow",
        "value": "2024-01-01 10:00:00",
        "source_quote": "2024-01-01 10:00:00",
        "from_column": "Timestamp",
        "to_field": "Timestamp",
        "transform": "identity",
    }
    verified = verify_link(link, attempts, schema)
    assert verified["verified"] and verified["to_operator"] == "GTE"
    edge = {
        k: verified[k]
        for k in ("from_column", "to_field", "transform", "to_parameter", "to_operator")
    }
    edge.update({"from": "s1", "to": "s2"})
    snippet = {
        "id": "temporal",
        "status": "candidate",
        "steps": [
            {"id": "s1", "template_id": a["id"]},
            {"id": "s2", "template_id": b["id"]},
        ],
        "bindings": [edge],
    }
    doc = {
        "format_version": "trajectory-snippets-2",
        "snippets": [snippet],
        "templates": {a["id"]: a, b["id"]: b},
    }
    doc["catalog_id"] = digest(doc)
    store = TrajectorySnippetStore(doc)
    upper = {k: v for k, v in bindings.items() if k != verified["to_parameter"]}
    prepared = store.prepare_step(
        "temporal",
        "s2",
        schema=schema,
        external_inputs=upper,
        observations={
            "s1": {"success": True, "rows": [{"Timestamp": "2024-01-01 10:30:00"}]}
        },
        allow_candidates=True,
    )
    assert prepared["params"] == ("2024-01-01 10:30:00", "2024-01-01 11:00:00")


def test_json_path_values_are_bound_without_flattening_other_fields():
    schema = {
        "Alerts": {"Id": "text", "Entities": "text"},
        "Mail": {"MailId": "text", "Sender": "text"},
    }
    a, _ = normalize_sql("SELECT Entities FROM Alerts WHERE Id='a'", schema)
    b, _ = normalize_sql("SELECT Sender FROM Mail WHERE MailId='b'", schema)
    snippet = {
        "id": "json",
        "status": "candidate",
        "steps": [
            {"id": "s1", "template_id": a["id"]},
            {"id": "s2", "template_id": b["id"]},
        ],
        "bindings": [
            {
                "from": "s1",
                "to": "s2",
                "from_column": "Entities",
                "to_field": "MailId",
                "transform": "parse_json",
                "json_path": ["*", "MailId"],
            }
        ],
    }
    doc = {
        "format_version": "trajectory-snippets-2",
        "snippets": [snippet],
        "templates": {a["id"]: a, b["id"]: b},
    }
    doc["catalog_id"] = digest(doc)
    store = TrajectorySnippetStore(doc)
    obs = {
        "s1": {
            "success": True,
            "rows": [{"Entities": '[{"MailId":"right","Name":"wrong"}]'}],
        }
    }
    result = store.prepare_step(
        "json",
        "s2",
        schema=schema,
        external_inputs={},
        observations=obs,
        allow_candidates=True,
    )
    assert result["params"] == ("right",)
    with pytest.raises(ValueError):
        store.prepare_step(
            "json",
            "s2",
            schema=schema,
            external_inputs={},
            observations=obs,
            selections={"s1.Entities": ["wrong"]},
            allow_candidates=True,
        )


def test_shared_sql_keeps_matching_intent_instead_of_first_training_story():
    doc = deepcopy(chain().document)
    s = doc["snippets"][0]
    s.update(
        name="first story",
        kind="evidence_chain",
        intent="first intent",
        trigger_entity={"concept": "URL", "kind": "indicator"},
        required_inputs=[],
        caution=["first caution"],
        intent_aliases=["first intent", "second intent"],
    )
    s["intent_variants"] = [
        {
            "id": "v2",
            "name": "second story",
            "intent": "second intent",
            "caution": ["second caution"],
            "step_guides": [
                {"step": "s1", "guide": "second first step"},
                {"step": "s2", "guide": "second last step"},
            ],
        }
    ]
    doc["catalog_id"] = digest({k: v for k, v in doc.items() if k != "catalog_id"})
    store = TrajectorySnippetStore(doc)
    hit = store.retrieve("URL", "second intent")["candidates"][0]
    assert hit["name"] == "second story" and hit["caution"] == ["second caution"]
    assert hit["steps"][0]["planner_guide"] == "second first step"
    assert store.retrieve("URL", "unknown intent") == {
        "route": "semantic",
        "candidates": [],
    }
    assert store.retrieve("Email_ID", "second intent")["route"] == "semantic"


def test_terminal_failed_query_is_not_a_successful_recovery():
    from scripts.audit_reviewed_procedural_memory import recovery_shape

    shape = recovery_shape(
        {"from": 5, "to": None},
        {5: {"status": "error"}},
        {5: {"sql_shape": "SELECT missing_column FROM logs"}},
    )
    assert shape["after"] is None
    assert shape["after_status"] == "not_attempted"
    assert shape["observed_guard"] == "error" and shape["unrecovered"]
