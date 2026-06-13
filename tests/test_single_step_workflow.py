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
from src.schema import Event, EventStatus, ExecutionStatus, RoundReview, SeverityLevel, TTTNode, TTTNodeStatus, TracebackTaskTree
from src.storage import SQLiteStorage


class SingleStepWorkflowTests(unittest.TestCase):
    def test_reviewer_agent_exposes_three_responsibilities(self) -> None:
        agent = ReviewerAgent()
        self.assertEqual(len(agent.responsibilities), 3)
        self.assertEqual(
            agent.responsibilities,
            (
                "总结本轮工具执行结果。",
                "总结已经收集到的结论。",
                "给出 1 条 TTT 调整建议。",
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
                message="测试单次执行后立即交给 Reviewer",
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
                            title="阶段一：确认可疑登录风险",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="问题1.1：源 IP 是否为恶意来源？",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="查询源 IP 的基础情报"),
                                        TTTNode(node_id="1-1-2", title="查询源 IP 的威胁情报"),
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
                message="测试查询无日志时不要直接变成 n/a",
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
                            title="方向一：确认日志是否存在",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="问题1.1：是否存在相关 Web 日志？",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="查询相关 Web 日志"),
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
                message="测试未执行完所有叶子也能 review",
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
                            title="阶段一：确认可疑登录风险",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="问题1.1：源 IP 是否为恶意来源？",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="查询源 IP 的基础情报"),
                                        TTTNode(node_id="1-1-2", title="查询源 IP 的威胁情报"),
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
                    node_title="查询源 IP 的基础情报",
                    tool_name="ipinfo",
                    tool_input={"intent": "lookup"},
                    result={"ip": "11.22.33.44"},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch(
                "src.agent.reviewer.call_llm",
                return_value="已获得源 IP 基础情报，但尚未查询源 IP 的威胁情报。下一轮应优先补充相关情报查询。",
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
            self.assertIn("尚未查询源 IP 的威胁情报", review.summary_text)
            self.assertTrue(review.cumulative_fact_summary)

            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.ROUND_REVIEW_CREATED, message_types)
            self.assertIn(MessageType.HANDOFF_TO_PLANNER, message_types)

    def test_reviewer_includes_previous_cumulative_fact_summary_in_next_round_prompt(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "workflow.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = ReviewerRuntime(storage=storage, ttt_store=ttt_store, bus=bus)

            event = Event(
                event_id="single-step-reviewer-cumulative-facts",
                event_name="Reviewer Cumulative Fact Test",
                message="测试 Reviewer 下一轮会继承上一轮累计事实摘要",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REVIEWING,
                current_round=2,
            )
            storage.save_event(event)
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="第一轮已完成外部情报查询。",
                    cumulative_fact_summary="截至上一轮，11.22.33.44 已确认存在恶意情报命中；仍需继续确认其在内部环境中的实际影响。",
                )
            )
            ttt_store.save_snapshot(
                TracebackTaskTree(
                    event_id=event.event_id,
                    round_id=2,
                    root_nodes=(
                        TTTNode(
                            node_id="1",
                            title="阶段一：确认可疑登录风险",
                            children=(
                                TTTNode(
                                    node_id="1-1",
                                    title="问题1.1：该 IP 是否与内部异常登录直接相关？",
                                    children=(
                                        TTTNode(node_id="1-1-1", title="查询邮件网关认证日志"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )

            from src.schema import Execution
            from src.schema.execution import ExecutionStatus as ExecStatus

            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=2,
                    node_id="1-1-1",
                    node_title="查询邮件网关认证日志",
                    tool_name="splunk",
                    tool_input={"intent": "search auth logs"},
                    result={"events": [{"src_ip": "11.22.33.44"}]},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch(
                "src.agent.reviewer.call_llm",
                side_effect=[
                    "本轮已补充邮件网关认证日志，当前没有证据推翻上一轮对源 IP 恶意情报的判断，建议继续确认该 IP 与异常登录之间的直接关联。",
                    "截至上一轮，11.22.33.44 已确认存在恶意情报命中；本轮补充了邮件网关认证日志，仍需继续确认其与异常登录的直接关联。",
                ],
            ) as mocked_call:
                did_work = runtime.process_event(event)

            self.assertTrue(did_work)
            review = storage.get_round_review(event.event_id, 2)
            self.assertIsNotNone(review)
            assert review is not None
            self.assertIn("恶意情报命中", review.cumulative_fact_summary)
            first_user_prompt = mocked_call.call_args_list[0].args[1]
            self.assertIn("截至上一轮的累计事实摘要", first_user_prompt)
            self.assertIn("11.22.33.44 已确认存在恶意情报命中", first_user_prompt)


if __name__ == "__main__":
    unittest.main()
