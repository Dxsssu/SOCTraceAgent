from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.agent.executor import ExecutorRuntime
from src.agent.reviewer import ReviewerAgent, ReviewerRuntime
from src.memory.working_memory import TTTStore
from src.messaging import MessageQuery, MessageType, SQLiteMessageBus
from src.schema import Event, EventStatus, ExecutionStatus, SeverityLevel, TTTNode, TTTNodeStatus, TracebackTaskTree
from src.storage import SQLiteStorage


class SingleStepWorkflowTests(unittest.TestCase):
    def test_reviewer_agent_exposes_three_responsibilities(self) -> None:
        agent = ReviewerAgent()
        self.assertEqual(len(agent.responsibilities), 3)
        self.assertEqual(
            agent.responsibilities,
            (
                "Summarize the tool execution results for this round.",
                "Summarize the conclusions collected so far.",
                "Provide 1 suggestion for adjusting the TTT.",
            ),
        )

    def test_executor_handoffs_to_reviewer_after_single_execution(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "workflow.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = ExecutorRuntime(storage=storage, ttt_store=ttt_store, bus=bus)

            event = Event(
                event_id="single-step-executor",
                event_name="Single Step Executor Test",
                message="Test that a single execution is handed to Reviewer immediately",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.PLANNED,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                TracebackTaskTree(
                    event_id=event.event_id,
                    round_id=1,
                    root_nodes=(
                        TTTNode(
                            node_id="1",
                            title="Stage 1: Confirm suspicious login risk",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="Question 1.1: Is the source IP malicious?",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="Look up the basic intelligence for the source IP"),
                                        TTTNode(node_id="1-1-2", title="Look up the threat intelligence for the source IP"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )

            with (
                patch.object(runtime, "_select_tool", return_value={"tool_name": "ipinfo"}),
                patch.object(
                    runtime,
                    "_execute_tool",
                    return_value=(True, {"ip": "11.22.33.44"}, "", {"intent": "lookup"}),
                ),
            ):
                did_work = runtime.process_event(event)

            self.assertTrue(did_work)
            updated_event = storage.get_event(event.event_id)
            self.assertIsNotNone(updated_event)
            assert updated_event is not None
            self.assertEqual(updated_event.event_status, EventStatus.REVIEWING)

            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            leaves = ttt_store.list_leaf_nodes(latest_ttt)
            statuses = {leaf.node_id: leaf.status.value for leaf in leaves}
            self.assertEqual(statuses["1-1-1"], "done")
            self.assertEqual(statuses["1-1-2"], "todo")

            executions = storage.list_executions(event.event_id, 1)
            self.assertEqual(len(executions), 1)
            self.assertEqual(executions[0].execution_status, ExecutionStatus.COMPLETED)

            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.HANDOFF_TO_REVIEWER, message_types)
            self.assertIn(MessageType.EXECUTION_COMPLETED, message_types)

    def test_executor_keeps_zero_log_search_as_done_not_not_applicable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "workflow.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = ExecutorRuntime(storage=storage, ttt_store=ttt_store, bus=bus)

            event = Event(
                event_id="single-step-zero-log-search",
                event_name="Zero Result Search Test",
                message="Test that no-log searches do not become n/a directly",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.PLANNED,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                TracebackTaskTree(
                    event_id=event.event_id,
                    round_id=1,
                    root_nodes=(
                        TTTNode(
                            node_id="1",
                            title="Direction 1: Confirm whether the logs exist",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="Question 1.1: Are the relevant Web logs present?",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="Search the relevant Web logs"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )

            with (
                patch.object(runtime, "_select_tool", return_value={"tool_name": "log_search"}),
                patch.object(
                    runtime,
                    "_execute_tool",
                    return_value=(
                        True,
                        {"no_data_found": True, "result_count": 0, "warnings": ["No matching logs found in coarse search"]},
                        "",
                        {"intent": "lookup"},
                    ),
                ),
            ):
                did_work = runtime.process_event(event)

            self.assertTrue(did_work)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            leaf = latest_ttt.root_nodes[0].children[0].children[0]
            self.assertEqual(leaf.status, TTTNodeStatus.DONE)
            self.assertTrue(leaf.metadata["no_data_found"])

    def test_reviewer_reviews_round_without_waiting_for_all_leaves(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "workflow.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = ReviewerRuntime(storage=storage, ttt_store=ttt_store, bus=bus)

            event = Event(
                event_id="single-step-reviewer",
                event_name="Single Step Reviewer Test",
                message="Test that review can proceed before all leaves are executed",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REVIEWING,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                TracebackTaskTree(
                    event_id=event.event_id,
                    round_id=1,
                    root_nodes=(
                        TTTNode(
                            node_id="1",
                            title="Stage 1: Confirm suspicious login risk",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="Question 1.1: Is the source IP malicious?",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="Look up the basic intelligence for the source IP"),
                                        TTTNode(node_id="1-1-2", title="Look up the threat intelligence for the source IP"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )
            latest = ttt_store.update_node_status(
                event_id=event.event_id,
                node_id="1-1-1",
                new_status=TTTNodeStatus.DONE,
                updated_by="_executor",
                round_id=1,
            )
            self.assertIsNotNone(latest)

            from src.schema import Execution
            from src.schema.execution import ExecutionStatus as ExecStatus

            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up the basic intelligence for the source IP",
                    tool_name="ipinfo",
                    tool_input={"intent": "lookup"},
                    result={"ip": "11.22.33.44"},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch(
                "src.agent.reviewer.call_llm",
                return_value="The basic intelligence for the source IP has been collected, but the threat-intelligence lookup has not been completed yet. The next round should prioritize that lookup.",
            ):
                did_work = runtime.process_event(event)

            self.assertTrue(did_work)
            updated_event = storage.get_event(event.event_id)
            self.assertIsNotNone(updated_event)
            assert updated_event is not None
            self.assertEqual(updated_event.event_status, EventStatus.REPLANNING)

            review = storage.get_round_review(event.event_id, 1)
            self.assertIsNotNone(review)
            assert review is not None
            self.assertIn("the threat-intelligence lookup has not been completed yet", review.summary_text)

            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.ROUND_REVIEW_CREATED, message_types)
            self.assertIn(MessageType.HANDOFF_TO_PLANNER, message_types)


if __name__ == "__main__":
    unittest.main()
