from __future__ import annotations

from pm_fakes import approve

import json
from copy import deepcopy

import pytest
from jsonschema import ValidationError

from src.memory.longterm_memory.procedural_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import (
    DEFAULT_TRAJECTORIES,
    build_knowledge,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j import (
    graph_rows,
    load_knowledge,
    validate_knowledge,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import (
    PREREQUISITE_PROMPT,
    GUARD_PROMPT,
    catalog,
    load_selected,
    retrieve,
)
from src.schema import Event, TracebackTaskTree, TTTNode
from src.workflow.context import ExcytinBenchSnapshotRepository, MemoryContextService

FIELDS = {"name", "status", "goal", "input_slots", "operations", "semantic_refs"}


def snapshot():
    return json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())


def test_memory_contains_only_six_fields_and_single_operation():
    k = load_knowledge(DEFAULT_KNOWLEDGE_PATH)
    assert k["format_version"] == "4.0"
    assert len(k["procedures"]) == 65
    assert sum(p["status"] == "active" for p in k["procedures"]) == 54
    for p in k["procedures"]:
        assert set(p) == FIELDS
        assert isinstance(p["input_slots"], dict)
        assert isinstance(p["operations"], str)
        assert "SELECT " not in json.dumps(p)
    assert all(set(p) == {"name", "status", "goal"} for p in catalog(k))


@pytest.mark.skipif(
    not DEFAULT_TRAJECTORIES.exists(), reason="Training data not installed"
)
def test_rebuild_is_deterministic():
    rebuilt = build_knowledge()
    saved = snapshot()
    rebuilt.pop("generated_at")
    saved.pop("generated_at")
    assert rebuilt == saved


@pytest.mark.parametrize("mutation", ["extra_field", "duplicate", "hash"])
def test_invalid_catalog_is_rejected(mutation):
    k = deepcopy(snapshot())
    if mutation == "extra_field":
        k["procedures"][0]["stage"] = "locate"
    elif mutation == "duplicate":
        k["procedures"][1]["name"] = k["procedures"][0]["name"]
    else:
        k["semantic_catalog"]["schema_hash"] = "wrong"
    with pytest.raises((ValueError, ValidationError)):
        validate_knowledge(k)


def test_selector_sees_active_goals_and_input_requirements():
    k = snapshot()
    chosen = next(
        p
        for p in k["procedures"]
        if p["name"] == "通过邮件系统分配的消息标识从邮件链接关联邮件详情"
    )

    def selector(system, prompt):
        if system in {PREREQUISITE_PROMPT, GUARD_PROMPT}:
            return approve(prompt)
        payload = json.loads(prompt)
        assert payload["role"] == "executor"
        assert payload["task"]["question"] == "查询这封邮件的收件人"
        assert all(set(p) == {"name", "status", "goal", "input_slots"} for p in payload["catalog"])
        assert (
            "input_slots" in prompt
            and "operations" not in prompt
            and "semantic_refs" not in prompt
        )
        return json.dumps([chosen["name"]])

    # No table name or join key is needed in the user's question.
    result = retrieve(k, "查询这封邮件的收件人", selector=selector, role="executor")
    assert result == [chosen]
    result[0]["semantic_refs"].clear()
    assert chosen["semantic_refs"]  # callers cannot mutate the source catalog


def test_agent_choice_is_not_overridden_by_lexical_scores():
    k = snapshot()
    assert retrieve(k, "AlertInfo AlertEvidence", selector=lambda *_: "[]") == []
    assert retrieve(k, "AlertInfo AlertEvidence") == []
    chosen = next(p for p in k["procedures"] if p["status"] == "active")
    assert retrieve(
        k,
        "没有任何表名",
        selector=lambda system, prompt: (
            approve(prompt)
            if system in {PREREQUISITE_PROMPT, GUARD_PROMPT}
            else json.dumps([chosen["name"]])
        ),
    ) == [chosen]


@pytest.mark.parametrize("response", ["invalid", "{}", "null", "[1]", '{"names": []}'])
def test_malformed_selection_loads_nothing(response):
    assert retrieve(snapshot(), "task", selector=lambda *_: response) == []


def test_unknown_inactive_duplicate_and_excess_names_are_filtered():
    k = snapshot()
    active = [p for p in k["procedures"] if p["status"] == "active"]
    candidate = next(p for p in k["procedures"] if p["status"] == "candidate")
    names = ["unknown", candidate["name"], active[0]["name"], active[0]["name"]] + [
        p["name"] for p in active[1:]
    ]
    assert load_selected(k, names, limit=2) == active[:2]
    assert load_selected(k, names, limit=0) == []
    assert (
        retrieve(
            k,
            "task",
            selector=lambda system, prompt: (
                approve(prompt)
                if system in {PREREQUISITE_PROMPT, GUARD_PROMPT}
                else "```json\n" + json.dumps([active[0]["name"]]) + "\n```"
            ),
        )
        == active[:1]
    )


def test_graph_rows_do_not_restore_operation_graph_fields():
    rows = graph_rows(snapshot())
    assert len(rows) == 65
    assert all(
        "procedure_id" not in r and "stage" not in r and "operations" in r for r in rows
    )


def test_each_role_uses_selection_before_loading_details():
    roles = []

    def selector(system, prompt):
        if system in {PREREQUISITE_PROMPT, GUARD_PROMPT}:
            return approve(prompt)
        p = json.loads(prompt)
        roles.append(p["role"])
        assert all(set(x) == {"name", "status", "goal", "input_slots"} for x in p["catalog"])
        return json.dumps([x["name"] for x in p["catalog"] if x["status"] == "active"])

    repo = ExcytinBenchSnapshotRepository()
    service = MemoryContextService(
        repositories={"excytin_bench": repo}, procedure_selector=selector
    )
    event = Event(
        event_name="调查邮件",
        message="定位邮件相关记录",
        context={"memory_profile": "excytin_bench"},
    )
    views = [
        service.planner_view().build(event),
        service.executor_view().build(
            event,
            node=TTTNode(node_id="1", title="查询邮件收件人"),
            include_episodic=False,
        ),
        service.reviewer_view().build(
            event,
            ttt=TracebackTaskTree(event_id=event.event_id, round_id=1, root_nodes=()),
        ),
    ]
    assert roles == ["planner", "executor", "reviewer"]
    assert [len(v["procedural_memory"]["procedures"]) for v in views] == [1, 1, 1]
    for v in views:
        assert all(set(p) == FIELDS for p in v["procedural_memory"]["procedures"])
        assert "episodic_memory" not in v


@pytest.mark.parametrize(
    "check",
    [
        {"bindings": {}, "missing": ["告警标识"]},
        {"bindings": {"告警标识": "invented-id"}, "missing": []},
        {"bindings": {}, "missing": []},
        {"bindings": {"告警标识": "a-known", "extra": "a-known"}, "missing": []},
        {"bindings": {"告警标识": ""}, "missing": []},
    ],
)
def test_unmet_or_ungrounded_prerequisites_never_load_operations(check):
    def selector(system, prompt):
        if system in {PREREQUISITE_PROMPT, GUARD_PROMPT}:
            assert "operations" in prompt and "semantic_refs" in prompt
            return json.dumps(check)
        return '["通过告警标识从告警概要关联告警证据"]'

    assert retrieve(snapshot(), "已查得告警标识 a-known", selector=selector) == []


def test_goals_and_operations_are_natural_language():
    import re

    for p in snapshot()["procedures"]:
        assert not re.search(r"[A-Za-z_]{2,}", p["goal"])
        assert not re.search(r"[A-Za-z_]{2,}", p["operations"])
        assert not re.search(r"[A-Za-z_]{2,}", p["name"])


@pytest.mark.parametrize("mutation", ["table", "field", "duplicate_table"])
def test_invalid_semantic_references_are_rejected(mutation):
    k = deepcopy(snapshot())
    refs = k["procedures"][0]["semantic_refs"]
    if mutation == "table":
        refs[0]["table"] = "NonexistentTable"
    elif mutation == "field":
        refs[0]["fields"].append("NonexistentField")
    else:
        refs.append(deepcopy(refs[0]))
    with pytest.raises(ValueError):
        validate_knowledge(k)


def test_explicit_references_load_schema_without_technical_names_in_text():
    repo = ExcytinBenchSnapshotRepository()
    procedure = next(
        p
        for p in snapshot()["procedures"]
        if p["name"] == "根据邮件系统分配的消息标识查证邮件详情"
    )
    view = repo.retrieve_semantic(
        "查询这封邮件的收件人", procedures=[procedure], table_limit=1
    )
    assert view["tables"][0]["name"] == "EmailEvents"
    fields = [f["name"] for f in view["tables"][0]["field"]]
    refs = procedure["semantic_refs"][0]["fields"]
    assert set(fields[: len(refs)]) == set(refs)
    assert "RecipientEmailAddress" in fields
    row = next(r for r in graph_rows(snapshot()) if r["name"] == procedure["name"])
    assert json.loads(row["semantic_refs_json"]) == procedure["semantic_refs"]
