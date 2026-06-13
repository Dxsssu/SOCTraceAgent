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
We follow the example format.
Confirm that event_id and round_id come from the input.

ttt:
  root_nodes:
    - title: Stage 1: Confirm authenticity
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )
            storage.save_event(event)

            responses = [
                "We need to confirm whether the abnormal login succeeded and prioritize checking source-IP reputation, authentication logs, and related activity in the same time window.",
                """
ttt:
  root_nodes:
    - title: Direction 1: Confirm the authenticity of the alert
      status: todo
      children:
        - title: Question 1.1: Did the mail gateway receive a real login attempt from 11.22.33.44?
          status: todo
          children:
            - title: Look up the basic intelligence for this IP
              status: todo
              children: []
    - title: Direction 2: Assess source-IP risk
      status: todo
      children:
        - title: Question 2.1: Does 11.22.33.44 have malicious reputation?
          status: todo
          children:
            - title: Look up threat-intelligence labels for this IP
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
            self.assertIn("Planner analysis assistant", analysis_prompt)
            self.assertIn("Traceback Task Tree", ttt_prompt)
            self.assertIn("multiple investigation directions", ttt_prompt)
            self.assertIn("complete entity information", ttt_prompt)
            self.assertNotIn("confirm whether the abnormal login succeeded", ttt_user_prompt)

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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )
            storage.save_event(event)

            responses = [
                "Initial analysis is complete. The current priority is to inspect authentication logs and source-IP risk.",
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
                message="Detected abnormal login attempts against the mail system",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )

            responses = [
                "Authentication logs must be checked first to determine whether any real successful login occurred.",
                """
document_id: suspicious_login
""",
                """
ttt:
  root_nodes:
    - title: Stage 1: Confirm account-login risk
      status: todo
      children:
        - title: Question 1.1: Was there any real successful login?
          status: todo
          children:
            - title: Search for successful-login records in authentication logs
              status: todo
              children: []
""",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                runtime.process_initial_plan(event)

            self.assertEqual(mocked_call_llm.call_count, 3)
            memory_selection_user_prompt = mocked_call_llm.call_args_list[1].args[1]
            self.assertIn("Suspicious Login Workflow", memory_selection_user_prompt)
            self.assertNotIn("Authentication logs must be checked first", memory_selection_user_prompt)
            ttt_generation_user_prompt = mocked_call_llm.call_args_list[2].args[1]
            self.assertIn("Use auth logs first.", ttt_generation_user_prompt)
            self.assertIn("Use Splunk BOTS logs as the primary evidence source.", ttt_generation_user_prompt)
            self.assertIn("\"server_name\": \"splunk\"", ttt_generation_user_prompt)
            self.assertIn("\"name\": \"log_search\"", ttt_generation_user_prompt)
            self.assertNotIn("Authentication logs must be checked first", ttt_generation_user_prompt)

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
                message="Detected abnormal login attempts against the mail system",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )

            responses = [
                "Provide an analysis paragraph first.",
                "We output according to the example format.",
                """
ttt:
  root_nodes:
    - title: Stage 1: Confirm account-login risk
      status: todo
      children:
        - title: Question 1.1: Was there any real successful login?
          status: todo
          children:
            - title: Search for successful-login records in authentication logs
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
                        "title": "Direction 1: Assess the attack source",
                        "children": [
                            {
                                "title": "Question 1.1: Does the source IP present risk?",
                                "children": [
                                    {"title": "Look up threat intelligence for this IP"},
                                    {"title": "Look up subsequent activity for this host"},
                                    {"title": "Look up login records for this account"},
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
        self.assertEqual(leaves[0].title, "Look up threat intelligence for 11.22.33.44")
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
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
                                "title": "Direction 1: Confirm the authenticity of the alert",
                                "children": [
                                    {
                                        "title": "Question 1.1: Did the mail gateway receive a real login attempt from 11.22.33.44?",
                                        "children": [
                                            {"title": "Look up the basic intelligence for source IP 11.22.33.44 from the alert"},
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
                    summary_text="The basic intelligence for the source IP has been collected, and no new investigation direction has emerged. The next round can continue by adding the remaining intelligence lookups.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up the basic intelligence for source IP 11.22.33.44 from the alert",
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
    - title: Direction 1: Confirm the authenticity of the alert
      status: todo
      children:
        - title: Question 1.1: Did the mail gateway receive a real login attempt from 11.22.33.44?
          status: done
          children:
            - title: Look up the basic intelligence for source IP 11.22.33.44 from the alert
              status: done
              children: []
""",
            ]
            with patch("src.agent.planner.call_llm", side_effect=responses) as mocked_call_llm:
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.COMPLETED)
            replanning_prompt = mocked_call_llm.call_args_list[0].args[1]
            self.assertIn("by default only update related node states and make the minimum necessary adjustments", replanning_prompt)
            self.assertIn("multiple L1 root nodes", replanning_prompt)
            self.assertIn("nodes that are already done must remain done", replanning_prompt)

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
                message="External IP 40.80.148.42 continuously accessed a public-facing website and signs of defacement appeared",
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
                            "title": "Direction 1: Assess source-IP risk",
                            "children": [
                                {
                                    "title": "Question 1.1: Does 40.80.148.42 have malicious intelligence indicators?",
                                    "children": [
                                        {
                                            "title": "Look up VirusTotal threat intelligence for 40.80.148.42",
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
                    summary_text="This round confirmed suspicious intelligence for the source IP, but no new investigation direction emerged.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up VirusTotal threat intelligence for 40.80.148.42",
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
    - title: Direction 1: Assess source-IP risk
      status: todo
      children:
        - title: Question 1.1: Does 40.80.148.42 have malicious intelligence indicators?
          status: todo
          children:
            - title: Look up VirusTotal threat intelligence for 40.80.148.42
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
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
                            "title": "Direction 1: Assess source-IP risk",
                            "children": [
                                {
                                    "title": "Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?",
                                    "children": [
                                        {
                                            "title": "Look up the basic intelligence for 11.22.33.44",
                                            "status": "done",
                                        },
                                        {
                                            "title": "Look up the threat intelligence for 11.22.33.44",
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
                    summary_text="The basic intelligence for the source IP has been collected. There is no new key evidence or new investigation direction, so continue executing the remaining nodes in the current TTT.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up the basic intelligence for 11.22.33.44",
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
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
                                "title": "Direction 1: Assess source-IP risk",
                                "children": [
                                    {
                                        "title": "Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?",
                                        "children": [
                                            {
                                                "title": "Search the related logs for 11.22.33.44",
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
                    summary_text="The current query returned no matching logs. Try adjusting log granularity and continue validation.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Search the related logs for 11.22.33.44",
                    tool_name="splunk",
                    tool_input={"intent": "lookup 11.22.33.44"},
                    result={"no_data_found": True, "result_count": 0},
                    execution_status=ExecStatus.COMPLETED,
                )
            )

            with patch("src.agent.planner.call_llm", return_value="""
ttt:
  root_nodes:
    - title: Direction 1: Assess source-IP risk
      children:
        - title: Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?
          children:
            - title: Search the related logs for 11.22.33.44 again after broadening log conditions
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
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
                            "title": "Direction 1: Assess source-IP risk",
                            "children": [
                                {
                                    "title": "Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?",
                                    "children": [
                                        {
                                            "title": "Look up the basic intelligence for 11.22.33.44",
                                            "status": "done",
                                        },
                                        {
                                            "title": "Look up the threat intelligence for 11.22.33.44",
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
                    summary_text="The next threat-intelligence query still needs to be executed.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up the basic intelligence for 11.22.33.44",
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
                message="External IP 40.80.148.42 continuously accessed a public-facing site and signs of defacement appeared",
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
                            "title": "Direction 1: Assess source-IP risk",
                            "children": [
                                {
                                    "title": "Question 1.1: Does 40.80.148.42 have malicious intelligence indicators?",
                                    "children": [
                                        {
                                            "title": "Look up VirusTotal threat intelligence for 40.80.148.42",
                                            "status": "done",
                                        },
                                        {
                                            "title": "Search subsequent Web access logs for 40.80.148.42",
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
                    summary_text="This round needs to continue with follow-up Web-log validation.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=7,
                    node_id="1-1-1",
                    node_title="Look up VirusTotal threat intelligence for 40.80.148.42",
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
                        title="Direction 1: Confirm authenticity",
                        status=TTTNodeStatus.IN_PROGRESS,
                        children=(
                            TTTNode(
                                node_id="1-1",
                                title="Question 1.1: Is there real attack activity?",
                                status=TTTNodeStatus.IN_PROGRESS,
                                children=(
                                    TTTNode(
                                        node_id="1-1-1",
                                        title="Search the related logs",
                                        status=TTTNodeStatus.IN_PROGRESS,
                                    ),
                                    TTTNode(
                                        node_id="1-1-2",
                                        title="Search threat intelligence",
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
                    title="Direction 1: Assess source-IP risk",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="Question 1.1: Does 40.80.148.42 still require further validation?",
                            metadata={"replan_attempt_count": 3},
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="Search related logs for 40.80.148.42",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="Search threat intelligence for 40.80.148.42",
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
                    title="Direction 1: Assess source-IP risk",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="Question 1.1: Does 40.80.148.42 still require further validation?",
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="Search related logs for 40.80.148.42 again after broadening conditions",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="Search threat intelligence for 40.80.148.42",
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
                    title="Direction 3: Confirm the impact of page defacement",
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="Question 3.1: What are the concrete signs of page defacement?",
                            metadata={"replan_attempt_count": 2},
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="Search logs related to the defaced page",
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
                    title="Direction 1: Assess source-IP risk",
                    status=TTTNodeStatus.TODO,
                    children=(
                        TTTNode(
                            node_id="1-1",
                            title="Question 1.1: Does 40.80.148.42 have malicious intelligence indicators?",
                            status=TTTNodeStatus.TODO,
                            children=(
                                TTTNode(
                                    node_id="1-1-1",
                                    title="Search basic intelligence for 40.80.148.42",
                                    status=TTTNodeStatus.DONE,
                                ),
                                TTTNode(
                                    node_id="1-1-2",
                                    title="Search threat intelligence for 40.80.148.42",
                                    status=TTTNodeStatus.TODO,
                                ),
                                TTTNode(
                                    node_id="1-1-3",
                                    title="Search subsequent activity for 40.80.148.42",
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
                        "title": "Direction 1: Assess source-IP risk",
                        "status": "todo",
                        "children": [
                            {
                                "title": "Question 1.1: Does 40.80.148.42 have malicious intelligence indicators?",
                                "status": "todo",
                                "children": [
                                    {
                                        "title": "Search basic intelligence for 40.80.148.42",
                                        "status": "done",
                                        "children": [],
                                    },
                                    {
                                        "title": "Search threat intelligence for 40.80.148.42",
                                        "status": "in_progress",
                                        "children": [],
                                    },
                                    {
                                        "title": "Search subsequent activity for 40.80.148.42",
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
                message="External IP 11.22.33.44 attempted abnormal logins against the mail gateway",
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
                                "title": "Direction 1: Assess source-IP risk",
                                "children": [
                                    {
                                        "title": "Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?",
                                        "children": [
                                            {
                                                "title": "Look up the basic intelligence for 11.22.33.44",
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
                    summary_text="This round completed the core validation and produced no new pending issues.",
                )
            )
            storage.save_execution(
                Execution(
                    event_id=event.event_id,
                    round_id=1,
                    node_id="1-1-1",
                    node_title="Look up the basic intelligence for 11.22.33.44",
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
    - title: Direction 1: Assess source-IP risk
      children:
        - title: Question 1.1: Does 11.22.33.44 have malicious intelligence indicators?
          children:
            - title: Look up the basic intelligence for 11.22.33.44
              status: done
              children: []
""",
                "## Overall Assessment\n\nThe current event investigation is complete, and the available evidence does not support deeper expansion at this time.",
            ]

            with patch("src.agent.planner.call_llm", side_effect=responses):
                updated_event = runtime.process_replanning(event)

            self.assertEqual(updated_event.event_status, EventStatus.COMPLETED)
            messages = bus.list_messages(MessageQuery(event_id=event.event_id))
            message_types = [message.message_type for message in messages]
            self.assertIn(MessageType.OVERALL_ASSESSMENT_CREATED, message_types)


if __name__ == "__main__":
    unittest.main()
