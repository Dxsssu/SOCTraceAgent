from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.agent.planner import PlannerRuntime
from src.agent.llm import parse_yaml_response
from src.memory.longterm_memory import FactualMemoryLibrary, ProceduralMemoryLibrary
from src.memory.working_memory import TTTStore
from src.messaging import MessageQuery, MessageType, SQLiteMessageBus
from src.schema import Event, EventStatus, Execution, RoundReview, SeverityLevel
from src.schema.ttt import TTTNode, TTTNodeStatus, TracebackTaskTree, coerce_ttt_node_status
from src.schema.execution import ExecutionStatus as ExecStatus
from src.storage import SQLiteStorage


class PlannerRuntimeTests(unittest.TestCase):
    def test_parse_yaml_response_accepts_prefixed_explanation(self) -> None:
        parsed = parse_yaml_response(
            """
我们按照示例格式。
确认 event_id 和 round_id 来自输入。

ttt:
  root_nodes:
    - title: 阶段一：确认真实性
      status: todo
      children: []
"""
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn("ttt", parsed)

    def test_coerce_ttt_status_maps_failed_to_not_applicable(self) -> None:
        self.assertEqual(coerce_ttt_node_status("failed").value, "n/a")
        self.assertEqual(coerce_ttt_node_status("completed").value, "done")
        self.assertEqual(coerce_ttt_node_status("unknown-status").value, "todo")

    def test_process_initial_plan_runs_analysis_before_ttt_generation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-analysis-order",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )
            storage.save_event(event)

            responses = [
                "需要确认异常登录是否成功，并优先核查源 IP 的信誉、认证日志以及同时间窗内的关联活动。",
                """
ttt:
  root_nodes:
    - title: 方向一：确认告警真实性
      status: todo
      children:
        - title: 问题1.1：邮件网关是否出现来自 11.22.33.44 的真实登录尝试？
          status: todo
          children:
            - title: 查询该 IP 的基础情报
              status: todo
              children: []
    - title: 方向二：评估源 IP 风险
      status: todo
      children:
        - title: 问题2.1：11.22.33.44 是否具备恶意信誉？
          status: todo
          children:
            - title: 查询该 IP 的威胁情报标签
              status: todo
              children: []
""",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                updated_event = runtime.process_initial_plan(event)

            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)
            self.assertEqual(mocked_call_llm.call_count, 2)
            analysis_prompt = mocked_call_llm.call_args_list[0].args[0]
            ttt_prompt = mocked_call_llm.call_args_list[1].args[0]
            ttt_user_prompt = mocked_call_llm.call_args_list[1].args[1]
            self.assertIn("Planner 分析助手", analysis_prompt)
            self.assertIn("Traceback Task Tree", ttt_prompt)
            self.assertIn("多个调查大方向", ttt_prompt)
            self.assertIn("完整实体信息", ttt_prompt)
            self.assertNotIn("需要确认异常登录是否成功", ttt_user_prompt)

            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertNotIn("planner_analysis", latest_ttt.metadata)
            self.assertIsNone(latest_ttt.metadata["selected_procedural_memory"])
            self.assertEqual(latest_ttt.metadata["factual_memories"], [])
            self.assertTrue(any(item["server_name"] == "splunk" for item in latest_ttt.metadata["available_mcp_tools"]))
            self.assertEqual(len(latest_ttt.root_nodes), 2)
            self.assertIn("11.22.33.44", latest_ttt.root_nodes[0].children[0].children[0].title)
            self.assertIn("11.22.33.44", latest_ttt.root_nodes[1].children[0].children[0].title)

            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.PLANNER_ANALYSIS_COMPLETED, message_types)
            self.assertIn(MessageType.TTT_INITIALIZED, message_types)

    def test_analysis_message_is_published_even_if_ttt_generation_fails(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-analysis-before-failure",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )
            storage.save_event(event)

            responses = [
                "已完成初始分析，当前应优先检查认证日志与源 IP 风险。",
                "not yaml at all",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses):
                updated_event = runtime.process_initial_plan(event)

            self.assertEqual(updated_event.event_status, EventStatus.FAILED)
            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.PLANNER_ANALYSIS_COMPLETED, message_types)
            self.assertIn(MessageType.SYSTEM_ERROR, message_types)

    def test_process_initial_plan_selects_matching_procedural_memory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            memory_dir = base_dir / "procedural_memory"
            memory_dir.mkdir()
            factual_dir = base_dir / "factual_memory"
            factual_dir.mkdir()
            (memory_dir / "suspicious_login.md").write_text(
                """---
title: Suspicious Login Workflow
event_types:
  - suspicious_login
tags:
  - auth
summary: Login workflow
---

Use auth logs first.
""",
                encoding="utf-8",
            )
            (factual_dir / "splunk_context.md").write_text(
                """---
title: Splunk Context
categories:
  - enterprise_background
tags:
  - splunk
summary: Splunk environment
---

Use Splunk BOTS logs as the primary evidence source.
""",
                encoding="utf-8",
            )
            (memory_dir / "malicious_ip.md").write_text(
                """---
title: Malicious IP Workflow
event_types:
  - malicious_ip
tags:
  - intel
summary: IP workflow
---

Use threat intel first.
""",
                encoding="utf-8",
            )

            db_path = base_dir / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=SQLiteMessageBus(db_path),
                procedural_memory=ProceduralMemoryLibrary(memory_dir),
                factual_memory=FactualMemoryLibrary(factual_dir),
            )
            event = Event(
                event_id="planner-memory-match",
                event_name="Suspicious Login",
                message="检测到邮件系统异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )

            responses = [
                "需要先检查认证日志，并判断是否存在真实成功登录。",
                """
document_id: suspicious_login
""",
                """
ttt:
  root_nodes:
    - title: 阶段一：确认账号登录风险
      status: todo
      children:
        - title: 问题1.1：是否存在真实成功登录？
          status: todo
          children:
            - title: 查询认证日志中的成功登录记录
              status: todo
              children: []
""",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                runtime.process_initial_plan(event)

            self.assertEqual(mocked_call_llm.call_count, 3)
            memory_selection_user_prompt = mocked_call_llm.call_args_list[1].args[1]
            self.assertIn("Suspicious Login Workflow", memory_selection_user_prompt)
            self.assertNotIn("需要先检查认证日志", memory_selection_user_prompt)
            ttt_generation_user_prompt = mocked_call_llm.call_args_list[2].args[1]
            self.assertIn("Use auth logs first.", ttt_generation_user_prompt)
            self.assertIn("Use Splunk BOTS logs as the primary evidence source.", ttt_generation_user_prompt)
            self.assertIn("\"server_name\": \"splunk\"", ttt_generation_user_prompt)
            self.assertIn("\"name\": \"log_search\"", ttt_generation_user_prompt)
            self.assertNotIn("需要先检查认证日志", ttt_generation_user_prompt)

            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertEqual(
                latest_ttt.metadata["selected_procedural_memory"]["document_id"],
                "suspicious_login",
            )
            self.assertEqual(len(latest_ttt.metadata["factual_memories"]), 1)
            self.assertTrue(any(item["server_name"] == "splunk" for item in latest_ttt.metadata["available_mcp_tools"]))

    def test_initial_ttt_generation_retries_after_non_yaml_response(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=SQLiteMessageBus(db_path),
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-ttt-retry",
                event_name="Suspicious Login",
                message="检测到邮件系统异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )

            responses = [
                "先给出一段分析文本。",
                "我们按照示例格式输出。",
                """
ttt:
  root_nodes:
    - title: 阶段一：确认账号登录风险
      status: todo
      children:
        - title: 问题1.1：是否存在真实成功登录？
          status: todo
          children:
            - title: 查询认证日志中的成功登录记录
              status: todo
              children: []
""",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                updated_event = runtime.process_initial_plan(event)

            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)
            self.assertEqual(mocked_call_llm.call_count, 3)

    def test_normalize_leaf_title_replaces_ambiguous_entities(self) -> None:
        runtime = PlannerRuntime()
        tree = runtime._normalize_ttt_payload(
            event_id="entity-normalization",
            round_id=1,
            ttt_payload={
                "root_nodes": [
                    {
                        "title": "方向一：评估攻击源",
                        "children": [
                            {
                                "title": "问题1.1：源 IP 是否存在风险？",
                                "children": [
                                    {"title": "查询该 IP 的威胁情报"},
                                    {"title": "查询该主机 的后续活动"},
                                    {"title": "查询该账号的登录记录"},
                                ],
                            }
                        ],
                    }
                ]
            },
            entity_context={
                "ip": "11.22.33.44",
                "host": "mail_server_01",
                "account": "alice",
            },
        )
        leaves = tree.root_nodes[0].children[0].children
        self.assertEqual(leaves[0].title, "查询11.22.33.44 的威胁情报")
        self.assertIn("mail_server_01", leaves[1].title)
        self.assertIn("alice", leaves[2].title)

    def test_process_replanning_uses_conservative_policy_after_success(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-replanning-policy",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                runtime._normalize_ttt_payload(
                    event_id=event.event_id,
                    round_id=1,
                    ttt_payload={
                        "root_nodes": [
                            {
                                "title": "方向一：确认告警真实性",
                                "children": [
                                    {
                                        "title": "问题1.1：邮件网关是否出现来自 11.22.33.44 的真实登录尝试？",
                                        "children": [
                                            {"title": "查询告警中的源 IP 11.22.33.44 的基础情报"},
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                    entity_context={"ip": "11.22.33.44"},
                )
            )
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="已获得源 IP 基础情报，但尚未出现新的调查方向，下一轮继续补充相关情报查询即可。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询告警中的源 IP 11.22.33.44 的基础情报",
                    tool_name="ipinfo",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"ip": "11.22.33.44"},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            responses = [
                """
ttt:
  root_nodes:
    - title: 方向一：确认告警真实性
      status: todo
      children:
        - title: 问题1.1：邮件网关是否出现来自 11.22.33.44 的真实登录尝试？
          status: done
          children:
            - title: 查询告警中的源 IP 11.22.33.44 的基础情报
              status: done
              children: []
""",
            ]
            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.COMPLETED)
            replanning_prompt = mocked_call_llm.call_args_list[0].args[1]
            self.assertIn("默认只更新相关节点状态和最小必要调整", replanning_prompt)
            self.assertIn("多个 L1 根节点", replanning_prompt)
            self.assertIn("已经是 done 的节点必须保持 done", replanning_prompt)

    def test_process_replanning_keeps_done_nodes_frozen(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=SQLiteMessageBus(db_path),
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-freeze-done",
                event_name="Web Defacement",
                message="外部 IP 40.80.148.42 持续访问对外网站并出现篡改迹象",
                source="unit_test",
                severity=SeverityLevel.HIGH,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            initial_tree = runtime._normalize_ttt_payload(
                event_id=event.event_id,
                round_id=1,
                ttt_payload={
                    "root_nodes": [
                        {
                            "title": "方向一：评估源 IP 风险",
                            "children": [
                                {
                                    "title": "问题1.1：40.80.148.42 是否具备恶意情报？",
                                    "children": [
                                        {
                                            "title": "查询 40.80.148.42 的 VirusTotal 威胁情报",
                                            "status": "done",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
                entity_context={"ip": "40.80.148.42"},
            )
            ttt_store.save_snapshot(initial_tree)
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="本轮已确认源 IP 存在可疑情报，但没有新增调查方向。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 40.80.148.42 的 VirusTotal 威胁情报",
                    tool_name="virustotal",
                    tool_input={"intent": "lookup 40.80.148.42"},
                    result={"malicious_votes": 5},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            responses = [
                """
ttt:
  root_nodes:
    - title: 方向一：评估源 IP 风险
      status: todo
      children:
        - title: 问题1.1：40.80.148.42 是否具备恶意情报？
          status: todo
          children:
            - title: 查询 40.80.148.42 的 VirusTotal 威胁情报
              status: todo
              children: []
""",
            ]
            with patch("src.agent.planner.call_llm", side_effect=responses):
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.COMPLETED)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            leaf = latest_ttt.root_nodes[0].children[0].children[0]
            self.assertEqual(leaf.status.value, "done")

    def test_process_replanning_reuses_existing_ttt_after_successful_step(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=SQLiteMessageBus(db_path),
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-reuse-existing-ttt",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            initial_tree = runtime._normalize_ttt_payload(
                event_id=event.event_id,
                round_id=1,
                ttt_payload={
                    "root_nodes": [
                        {
                            "title": "方向一：评估源 IP 风险",
                            "children": [
                                {
                                    "title": "问题1.1：11.22.33.44 是否具备恶意情报？",
                                    "children": [
                                        {
                                            "title": "查询 11.22.33.44 的基础情报",
                                            "status": "done",
                                        },
                                        {
                                            "title": "查询 11.22.33.44 的威胁情报",
                                            "status": "todo",
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
                entity_context={"ip": "11.22.33.44"},
            )
            ttt_store.save_snapshot(initial_tree)
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="已拿到源 IP 基础情报，当前没有新的关键证据或新的调查方向，建议沿现有 TTT 继续执行剩余节点。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 11.22.33.44 的基础情报",
                    tool_name="splunk",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"no_data_found": True, "result_count": 0},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch("src.agent.planner.call_llm") as mocked_call_llm:
                updated_event = runtime.process_replanning(event)

            mocked_call_llm.assert_not_called()
            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)
            self.assertEqual(updated_event.current_round, 2)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertEqual(latest_ttt.round_id, 2)
            self.assertEqual(latest_ttt.metadata.get("planner_replanning_strategy"), "reuse_existing_ttt")
            leaves = latest_ttt.root_nodes[0].children[0].children
            self.assertEqual(leaves[0].status.value, "done")
            self.assertEqual(leaves[1].status.value, "todo")

    def test_process_replanning_does_not_reuse_existing_ttt_when_only_no_data_found(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=SQLiteMessageBus(db_path),
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-no-data-replan",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                runtime._normalize_ttt_payload(
                    event_id=event.event_id,
                    round_id=1,
                    ttt_payload={
                        "root_nodes": [
                            {
                                "title": "方向一：评估源 IP 风险",
                                "children": [
                                    {
                                        "title": "问题1.1：11.22.33.44 是否具备恶意情报？",
                                        "children": [
                                            {
                                                "title": "查询 11.22.33.44 的相关日志",
                                                "status": "done",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                    entity_context={"ip": "11.22.33.44"},
                )
            )
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="当前查询没有命中相关日志，建议尝试调整日志粒度后继续验证。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 11.22.33.44 的相关日志",
                    tool_name="splunk",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"no_data_found": True, "result_count": 0},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch("src.agent.planner.call_llm", return_value="""
ttt:
  root_nodes:
    - title: 方向一：评估源 IP 风险
      children:
        - title: 问题1.1：11.22.33.44 是否具备恶意情报？
          children:
            - title: 放宽日志条件后再次查询 11.22.33.44 的相关日志
              status: todo
              children: []
""") as mocked_call_llm:
                updated_event = runtime.process_replanning(event)

            mocked_call_llm.assert_called_once()
            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)

    def test_process_replanning_falls_back_to_reuse_existing_ttt_on_non_yaml_response(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-replan-non-yaml-fallback",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            initial_tree = runtime._normalize_ttt_payload(
                event_id=event.event_id,
                round_id=1,
                ttt_payload={
                    "root_nodes": [
                        {
                            "title": "方向一：评估源 IP 风险",
                            "children": [
                                {
                                    "title": "问题1.1：11.22.33.44 是否具备恶意情报？",
                                    "children": [
                                        {
                                            "title": "查询 11.22.33.44 的基础情报",
                                            "status": "done",
                                        },
                                        {
                                            "title": "查询 11.22.33.44 的威胁情报",
                                            "status": "todo",
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
                entity_context={"ip": "11.22.33.44"},
            )
            ttt_store.save_snapshot(initial_tree)
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="当前需要继续推进下一条威胁情报查询。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 11.22.33.44 的基础情报",
                    tool_name="splunk",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"no_data_found": True, "result_count": 0},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch("src.agent.planner.call_llm", side_effect=["not yaml", "still not yaml"]):
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)
            self.assertEqual(updated_event.current_round, 2)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertEqual(latest_ttt.round_id, 2)
            self.assertEqual(latest_ttt.metadata.get("planner_replanning_strategy"), "reuse_existing_ttt")
            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            self.assertTrue(
                any(str(message.message_type) == MessageType.SYSTEM_WARNING.value for message in messages)
            )

    def test_process_replanning_falls_back_to_reuse_existing_ttt_on_timeout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-replan-timeout-fallback",
                event_name="Web Defacement",
                message="外部 IP 40.80.148.42 持续访问对外站点并出现篡改迹象",
                source="unit_test",
                severity=SeverityLevel.HIGH,
                event_status=EventStatus.REPLANNING,
                current_round=7,
            )
            storage.save_event(event)
            initial_tree = runtime._normalize_ttt_payload(
                event_id=event.event_id,
                round_id=7,
                ttt_payload={
                    "root_nodes": [
                        {
                            "title": "方向一：评估源 IP 风险",
                            "children": [
                                {
                                    "title": "问题1.1：40.80.148.42 是否具备恶意情报？",
                                    "children": [
                                        {
                                            "title": "查询 40.80.148.42 的 VirusTotal 威胁情报",
                                            "status": "done",
                                        },
                                        {
                                            "title": "查询 40.80.148.42 的后续 Web 访问日志",
                                            "status": "todo",
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
                entity_context={"ip": "40.80.148.42"},
            )
            ttt_store.save_snapshot(initial_tree)
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=7,
                    summary_text="本轮需要继续推进后续 Web 日志核查。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=7,
                    node_id="1-1-1",
                    node_title="查询 40.80.148.42 的 VirusTotal 威胁情报",
                    tool_name="virustotal",
                    tool_input={"intent": "lookup 40.80.148.42"},
                    result={"no_data_found": True, "result_count": 0},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch("src.agent.planner.call_llm", side_effect=TimeoutError("request timed out")):
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.PLANNED)
            self.assertEqual(updated_event.current_round, 8)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertEqual(latest_ttt.round_id, 8)
            self.assertEqual(latest_ttt.metadata.get("planner_replanning_strategy"), "reuse_existing_ttt")
            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            warnings = [message for message in messages if message.message_type == MessageType.SYSTEM_WARNING]
            self.assertTrue(warnings)
            self.assertIn("timed out", str(warnings[-1].payload.get("reason", "")))

    def test_reuse_existing_ttt_resets_in_progress_nodes_to_todo(self) -> None:
        runtime = PlannerRuntime()
        reused = runtime._reuse_existing_ttt_for_next_round(
            latest_ttt=TracebackTaskTree(
                event_id="reuse-in-progress",
                round_id=1,
                root_nodes=(
                    TTTNode(
                        node_id="1",
                        title="方向一：确认真实性",
                        status=TTTNodeStatus.IN_PROGRESS,
                        children=(
                            TTTNode(
                                node_id="1-1",
                                title="问题1.1：是否存在真实攻击行为？",
                                status=TTTNodeStatus.IN_PROGRESS,
                                children=(
                                    TTTNode(
                                        node_id="1-1-1",
                                        title="查询相关日志",
                                        status=TTTNodeStatus.IN_PROGRESS,
                                    ),
                                    TTTNode(
                                        node_id="1-1-2",
                                        title="查询威胁情报",
                                        status=TTTNodeStatus.DONE,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            next_round=2,
        )
        self.assertEqual(reused.round_id, 2)
        self.assertEqual(reused.root_nodes[0].status, TTTNodeStatus.TODO)
        self.assertEqual(reused.root_nodes[0].children[0].status, TTTNodeStatus.TODO)
        self.assertEqual(reused.root_nodes[0].children[0].children[0].status, TTTNodeStatus.TODO)
        self.assertEqual(reused.root_nodes[0].children[0].children[1].status, TTTNodeStatus.DONE)

    def test_apply_l2_replan_policy_skips_l2_after_three_adjustments(self) -> None:
        runtime = PlannerRuntime()
        previous_tree = TracebackTaskTree(
            event_id="l2-cap",
            round_id=3,
            root_nodes=(
                TTTNode(
                    node_id="1",
                    title="方向一：评估源 IP 风险",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="问题1.1：40.80.148.42 是否仍需继续查证？",
                            metadata={"replan_attempt_count": 3},
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="查询 40.80.148.42 的相关日志",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="查询 40.80.148.42 的威胁情报",
                                    status=TTTNodeStatus.DONE,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        candidate_tree = TracebackTaskTree(
            event_id="l2-cap",
            round_id=4,
            root_nodes=(
                TTTNode(
                    node_id="1",
                    title="方向一：评估源 IP 风险",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="问题1.1：40.80.148.42 是否仍需继续查证？",
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="放宽条件后再次查询 40.80.148.42 的相关日志",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="查询 40.80.148.42 的威胁情报",
                                    status=TTTNodeStatus.DONE,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

        updated_tree = runtime._apply_l2_replan_policy(candidate_tree, previous_tree=previous_tree)
        l2_node = updated_tree.root_nodes[0].children[0]
        self.assertEqual(l2_node.metadata["replan_attempt_count"], 4)
        self.assertEqual(l2_node.metadata["skip_reason"], "l2_replan_cap_reached")
        self.assertEqual(l2_node.status, TTTNodeStatus.NOT_APPLICABLE)
        self.assertEqual(l2_node.children[0].status, TTTNodeStatus.NOT_APPLICABLE)
        self.assertEqual(l2_node.children[1].status, TTTNodeStatus.DONE)

    def test_apply_l2_replan_policy_does_not_increment_when_l2_is_unchanged(self) -> None:
        runtime = PlannerRuntime()
        previous_tree = TracebackTaskTree(
            event_id="l2-unchanged",
            round_id=3,
            root_nodes=(
                TTTNode(
                    node_id="1",
                    title="方向三：确认页面篡改影响",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="问题3.1：页面被篡改的具体表现是什么？",
                            metadata={"replan_attempt_count": 2},
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="查询篡改页面相关日志",
                                    status=TTTNodeStatus.TODO,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        candidate_tree = TracebackTaskTree(
            event_id="l2-unchanged",
            round_id=4,
            root_nodes=previous_tree.root_nodes,
        )

        updated_tree = runtime._apply_l2_replan_policy(candidate_tree, previous_tree=previous_tree)
        l2_node = updated_tree.root_nodes[0].children[0]
        self.assertEqual(l2_node.metadata["replan_attempt_count"], 2)
        self.assertNotIn("skip_reason", l2_node.metadata)
        self.assertEqual(l2_node.status, TTTNodeStatus.TODO)

    def test_normalize_ttt_payload_enforces_linear_progression_for_future_nodes(self) -> None:
        runtime = PlannerRuntime()
        previous_tree = TracebackTaskTree(
            event_id="linear-progress",
            round_id=1,
            root_nodes=(
                TTTNode(
                    node_id="1",
                    title="方向一：评估源 IP 风险",
                    status=TTTNodeStatus.TODO,
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="问题1.1：40.80.148.42 是否具备恶意情报？",
                            status=TTTNodeStatus.TODO,
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="查询 40.80.148.42 的基础情报",
                                    status=TTTNodeStatus.DONE,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="查询 40.80.148.42 的威胁情报",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-3",
                                    title="查询 40.80.148.42 的后续行为",
                                    status=TTTNodeStatus.TODO,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

        updated_tree = runtime._normalize_ttt_payload(
            event_id="linear-progress",
            round_id=2,
            ttt_payload={
                "root_nodes": [
                    {
                        "title": "方向一：评估源 IP 风险",
                        "status": "todo",
                        "children": [
                            {
                                "title": "问题1.1：40.80.148.42 是否具备恶意情报？",
                                "status": "todo",
                                "children": [
                                    {
                                        "title": "查询 40.80.148.42 的基础情报",
                                        "status": "done",
                                        "children": [],
                                    },
                                    {
                                        "title": "查询 40.80.148.42 的威胁情报",
                                        "status": "in_progress",
                                        "children": [],
                                    },
                                    {
                                        "title": "查询 40.80.148.42 的后续行为",
                                        "status": "n/a",
                                        "children": [],
                                    },
                                ],
                            },
                        ],
                    },
                ]
            },
            previous_tree=previous_tree,
            apply_l2_policy=False,
        )

        leaves = updated_tree.root_nodes[0].children[0].children
        self.assertEqual(leaves[0].status, TTTNodeStatus.DONE)
        self.assertEqual(leaves[1].status, TTTNodeStatus.TODO)
        self.assertEqual(leaves[2].status, TTTNodeStatus.TODO)

    def test_process_replanning_publishes_overall_assessment_when_completed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-overall-assessment",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.REPLANNING,
                current_round=1,
            )
            storage.save_event(event)
            ttt_store.save_snapshot(
                runtime._normalize_ttt_payload(
                    event_id=event.event_id,
                    round_id=1,
                    ttt_payload={
                        "root_nodes": [
                            {
                                "title": "方向一：评估源 IP 风险",
                                "children": [
                                    {
                                        "title": "问题1.1：11.22.33.44 是否具备恶意情报？",
                                        "children": [
                                            {
                                                "title": "查询 11.22.33.44 的基础情报",
                                                "status": "done",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                    entity_context={"ip": "11.22.33.44"},
                )
            )
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="本轮已完成核心核查，没有新的待执行问题。",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 11.22.33.44 的基础情报",
                    tool_name="splunk",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"events": [{"src_ip": "11.22.33.44"}]},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            responses = [
                """
ttt:
  root_nodes:
    - title: 方向一：评估源 IP 风险
      children:
        - title: 问题1.1：11.22.33.44 是否具备恶意情报？
          children:
            - title: 查询 11.22.33.44 的基础情报
              status: done
              children: []
""",
                "## 整体研判\n\n当前事件已经完成调查，现有证据不足以支持更深入扩展。",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses):
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.COMPLETED)
            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.OVERALL_ASSESSMENT_CREATED, message_types)

    def test_generate_overall_assessment_prompt_requests_recommendations(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "planner.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)
            runtime = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(Path(tmpdir) / "procedural_memory"),
                factual_memory=FactualMemoryLibrary(Path(tmpdir) / "factual_memory"),
            )
            event = Event(
                event_id="planner-overall-recommendations",
                event_name="Suspicious Login",
                message="外部 IP 11.22.33.44 对邮件网关出现异常登录尝试",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
                event_status=EventStatus.COMPLETED,
                current_round=1,
            )
            latest_ttt = runtime._normalize_ttt_payload(
                event_id=event.event_id,
                round_id=1,
                ttt_payload={
                    "root_nodes": [
                        {
                            "title": "方向一：评估源 IP 风险",
                            "children": [
                                {
                                    "title": "问题1.1：11.22.33.44 是否具备恶意情报？",
                                    "children": [
                                        {
                                            "title": "查询 11.22.33.44 的基础情报",
                                            "status": "done",
                                            "children": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
                entity_context={"ip": "11.22.33.44"},
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="查询 11.22.33.44 的基础情报",
                    tool_name="virustotal",
                    tool_input={"ip": "11.22.33.44"},
                    result={"summary": {"malicious": 3}},
                    execution_status=ExecStatus.COMPLETED,
                )
            )
            storage.save_round_review(
                RoundReview(
                    event_id=event.event_id,
                    round_id=1,
                    summary_text="本轮已完成核心核查，没有新的待执行问题。",
                )
            )

            with patch("src.agent.planner.call_llm", return_value="## 整体研判\n\n调查完成。\n\n## 建议措施\n\n- 立即封禁源 IP。") as mocked_call:
                summary_text = runtime._generate_overall_assessment(
                    event=event,
                    latest_ttt=latest_ttt,
                )

            self.assertIn("建议措施", summary_text)
            system_prompt, user_prompt = mocked_call.call_args.args[:2]
            self.assertIn("企业后续建议", system_prompt)
            self.assertIn("后续建议措施", user_prompt)


if __name__ == "__main__":
    unittest.main()
