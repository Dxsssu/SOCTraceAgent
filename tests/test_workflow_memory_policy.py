from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from src.agent.executor import ExecutorRuntime
from src.agent.planner import PlannerRuntime
from src.messaging import RoleName
from src.schema import Event, EventStatus, TracebackTaskTree, TTTNode, TTTNodeStatus
from src.workflow.context import (
    ExcytinBenchSnapshotRepository,
    MemoryAccessPolicy,
    MemoryContextService,
    MemoryType,
)
from src.workflow.orchestrator import SOCTraceWorkflow


def excytin_event() -> Event:
    return Event(
        event_id="workflow-memory-test",
        event_name="ExCyTIn network investigation",
        message=(
            "Given an initial alert and suspicious IP, identify the device, process, "
            "and account involved in the network activity."
        ),
        context={"memory_profile": "excytin_bench"},
    )


def test_role_policy_prevents_planner_episodic_access() -> None:
    policy = MemoryAccessPolicy()
    assert policy.allowed(RoleName.PLANNER, MemoryType.PROCEDURAL)
    assert policy.allowed(RoleName.PLANNER, MemoryType.SEMANTIC)
    assert not policy.allowed(RoleName.PLANNER, MemoryType.EPISODIC)
    with pytest.raises(PermissionError):
        policy.require(RoleName.PLANNER, MemoryType.EPISODIC)


def test_planner_receives_only_procedural_and_semantic_memory() -> None:
    view = MemoryContextService().planner_view()
    context = view.build(excytin_event())
    serialized = json.dumps(context, ensure_ascii=False).lower()

    assert context["enabled"] is True
    assert context["allowed_memory_types"] == ["procedural", "semantic"]
    assert context["procedural_memory"]["procedures"]
    assert context["semantic_memory"]["tables"]
    assert "episodic_memory" not in context
    assert "exemplar_attempt" not in serialized
    assert "support_episode" not in serialized
    assert not hasattr(view, "retrieve_episodes")


def test_executor_receives_semantic_and_sanitized_episodic_memory() -> None:
    event = excytin_event()
    node = TTTNode(
        node_id="1-1-1",
        title="Collect the device and process associated with the suspicious IP",
        metadata={
            "candidate_tables": ["DeviceNetworkEvents"],
            "semantic_memory_refs": ["table:DeviceNetworkEvents"],
            "procedural_memory_refs": ["skill:network-activity-investigation"],
        },
    )
    context = MemoryContextService().executor_view().build(event, node=node)

    assert context["allowed_memory_types"] == ["semantic", "episodic"]
    assert "procedural_memory" not in context
    assert context["semantic_memory"]["tables"][0]["name"] == "DeviceNetworkEvents"
    assert context["episodic_memory"]["episodes"]
    assert all(
        episode["source_split"] == "train"
        for episode in context["episodic_memory"]["episodes"]
    )


def test_manual_investigation_semantics_retrieve_security_alert() -> None:
    repository = ExcytinBenchSnapshotRepository()
    semantic = repository.retrieve_semantic(
        "Identify the host where an automated investigation was manually started",
        table_limit=8,
    )

    assert semantic["tables"][0]["name"] == "SecurityAlert"


def test_no_profile_preserves_existing_non_ltm_workflow() -> None:
    event = Event(event_name="Splunk event", message="Investigate botsv1 DNS logs")
    service = MemoryContextService()
    planner_context = service.planner_view().build(event)
    executor_context = service.executor_view().build(
        event,
        node=TTTNode(node_id="1-1-1", title="Search DNS logs"),
    )
    assert planner_context == {"enabled": False, "reason": "no_memory_profile"}
    assert executor_context == {"enabled": False, "reason": "no_memory_profile"}


def test_planner_prompt_contains_workflow_filtered_context() -> None:
    class FakePlannerContext:
        def build(self, event: Event, **_: object) -> dict[str, object]:
            return {
                "enabled": True,
                "allowed_memory_types": ["procedural", "semantic"],
                "procedural_memory": {"marker": "planner-pm"},
                "semantic_memory": {"marker": "planner-sm"},
            }

    response = """
response_type: TTT_PLAN
ttt:
  root_nodes:
    - title: Investigate the alert
      status: todo
      children:
        - title: Validate the network hypothesis
          status: todo
          children:
            - title: Collect network evidence
              status: todo
              children: []
"""
    runtime = PlannerRuntime(context_provider=FakePlannerContext())
    with patch("src.agent.planner.call_llm", return_value=response) as mocked:
        tree = runtime._generate_initial_ttt(excytin_event())

    user_prompt = mocked.call_args.args[1]
    assert "planner-pm" in user_prompt
    assert "planner-sm" in user_prompt
    assert "episodic_memory" not in user_prompt
    assert tree.root_nodes[0].children[0].children[0].node_id == "1-1-1"


def test_executor_tool_intent_includes_only_its_scoped_context() -> None:
    context = {
        "allowed_memory_types": ["semantic", "episodic"],
        "semantic_memory": {"marker": "executor-sm"},
        "episodic_memory": {"marker": "executor-em"},
    }
    intent = ExecutorRuntime._build_tool_intent(
        excytin_event(),
        TTTNode(node_id="1-1-1", title="Collect network evidence"),
        workflow_context=context,
    )
    assert "executor-sm" in intent
    assert "executor-em" in intent
    assert "procedural_memory" not in intent


def test_orchestrator_wires_shared_state_and_scoped_views() -> None:
    with TemporaryDirectory() as directory:
        db_path = str(Path(directory) / "workflow.db")
        workflow = SOCTraceWorkflow(db_path=db_path, poll_interval=0.01)
        assert workflow.planner.storage is workflow.storage
        assert workflow.executor.ttt_store is workflow.ttt_store
        assert workflow.reviewer.bus is workflow.bus
        assert (
            workflow.planner.context_provider.__class__.__name__ == "PlannerMemoryView"
        )
        assert (
            workflow.executor.context_provider.__class__.__name__
            == "ExecutorMemoryView"
        )
        assert (
            workflow.reviewer.context_provider.__class__.__name__
            == "ReviewerMemoryView"
        )


def test_executor_hands_off_after_one_l3_per_round() -> None:
    with TemporaryDirectory() as directory:
        workflow = SOCTraceWorkflow(
            db_path=str(Path(directory) / "single-step.db"),
            poll_interval=0.01,
        )
        event = Event(
            event_id="single-step-event",
            event_name="Single-step workflow",
            message="Investigate an alert",
            event_status=EventStatus.PLANNED,
        )
        workflow.storage.save_event(event)
        workflow.ttt_store.save_snapshot(
            TracebackTaskTree(
                event_id=event.event_id,
                round_id=event.current_round,
                root_nodes=(
                    TTTNode(
                        node_id="1",
                        title="Investigate",
                        children=(
                            TTTNode(
                                node_id="1-1",
                                title="Collect evidence",
                                children=(
                                    TTTNode(node_id="1-1-1", title="First query"),
                                    TTTNode(node_id="1-1-2", title="Second query"),
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )

        with (
            patch.object(
                workflow.executor,
                "_select_tool",
                return_value={
                    "tool_name": "fake",
                    "reason": "test",
                    "confidence": "high",
                },
            ),
            patch.object(
                workflow.executor,
                "_execute_tool",
                return_value=(True, {"rows": 1}, "", {"query": "SELECT 1"}),
            ),
        ):
            assert workflow.executor.process_event(event)

        updated_event = workflow.storage.get_event(event.event_id)
        latest_ttt = workflow.ttt_store.get_latest_ttt(event.event_id)
        assert updated_event is not None
        assert updated_event.event_status == EventStatus.REVIEWING
        assert latest_ttt is not None
        leaves = workflow.ttt_store.list_leaf_nodes(latest_ttt)
        assert [leaf.status for leaf in leaves] == [
            TTTNodeStatus.DONE,
            TTTNodeStatus.TODO,
        ]
        assert (
            len(
                workflow.storage.list_executions(
                    event.event_id,
                    event.current_round,
                )
            )
            == 1
        )

        review_response = """
response_type: ROUND_REVIEW
findings:
  - The first query returned one row.
gaps:
  - The second evidence goal remains open.
recommendations:
  - Preserve the remaining TTT branch for replanning.
"""
        with patch("src.agent.reviewer.call_llm", return_value=review_response):
            assert workflow.reviewer.process_event(updated_event)
        replanning_event = workflow.storage.get_event(event.event_id)
        assert replanning_event is not None
        assert replanning_event.event_status == EventStatus.REPLANNING
        assert workflow.storage.get_round_review(event.event_id, event.current_round)
