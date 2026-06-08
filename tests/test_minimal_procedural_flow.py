from __future__ import annotations

from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import warnings

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.agent.executor import ExecutorRuntime
from src.agent.planner import PlannerRuntime
from src.agent.reviewer import ReviewerRuntime
from src.memory.longterm_memory import FactualMemoryLibrary, ProceduralMemoryLibrary
from src.memory.working_memory import TTTStore
from src.messaging import SQLiteMessageBus
from src.schema import Event, EventStatus, SeverityLevel
from src.storage import SQLiteStorage

warnings.simplefilter("ignore", ResourceWarning)


class MinimalProceduralFlowTests(unittest.TestCase):
    def test_public_ip_reputation_procedural_flow_runs_end_to_end(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "minimal_procedural_flow.db"
            storage = SQLiteStorage(db_path)
            ttt_store = TTTStore(db_path)
            bus = SQLiteMessageBus(db_path)

            planner = PlannerRuntime(
                storage=storage,
                ttt_store=ttt_store,
                bus=bus,
                procedural_memory=ProceduralMemoryLibrary(
                    ROOT_DIR / "src" / "memory" / "longterm_memory" / "procedural_memory"
                ),
                factual_memory=FactualMemoryLibrary(
                    ROOT_DIR / "src" / "memory" / "longterm_memory" / "factual_memory"
                ),
            )
            executor = ExecutorRuntime(storage=storage, ttt_store=ttt_store, bus=bus)
            reviewer = ReviewerRuntime(storage=storage, ttt_store=ttt_store, bus=bus)

            event = Event(
                event_id="procedural-public-ip-demo",
                event_name="Public IP Reputation Demo",
                message="告警：公网 IP 8.8.8.8 多次访问外部服务，当前仅需做最小验证，请先确认该 IP 的基础归属与外部威胁情报。",
                source="unit_test",
                severity=SeverityLevel.MEDIUM,
            )
            storage.save_event(event)

            initial_planner_responses = [
                "该告警只给出了一个公网 IP，当前最小可行排查应先补齐基础属性，再检查外部威胁情报，暂不扩展到更复杂日志分析。",
                "document_id: public_ip_reputation_triage",
                """
ttt:
  root_nodes:
    - title: 方向一：评估公网 IP 8.8.8.8 的风险背景
      status: todo
      children:
        - title: 问题1.1：公网 IP 8.8.8.8 的基础归属和外部信誉如何？
          status: todo
          children:
            - title: 查询公网 IP 8.8.8.8 的基础情报
              status: todo
              children: []
            - title: 查询公网 IP 8.8.8.8 的威胁情报报告
              status: todo
              children: []
""",
            ]

            with patch("src.agent.planner.call_llm", side_effect=initial_planner_responses):
                planned_event = planner.process_initial_plan(event)

            self.assertEqual(planned_event.event_status, EventStatus.PLANNED)
            latest_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(latest_ttt)
            assert latest_ttt is not None
            self.assertEqual(len(latest_ttt.root_nodes), 1)
            self.assertEqual(len(latest_ttt.root_nodes[0].children), 1)
            self.assertEqual(len(latest_ttt.root_nodes[0].children[0].children), 2)
            self.assertEqual(
                latest_ttt.metadata["selected_procedural_memory"]["document_id"],
                "public_ip_reputation_triage",
            )

            with patch("src.agent.executor.call_llm", return_value="tool_name: ipinfo_lookup_ips"), patch(
                "src.tools.ipinfo._fetch_ipinfo_record",
                return_value={
                    "ip": "8.8.8.8",
                    "city": "Mountain View",
                    "region": "California",
                    "country": "US",
                    "country_name": "United States",
                    "org": "AS15169 Google LLC",
                    "asn": "AS15169",
                    "asn_type": "hosting",
                    "postal": "94043",
                    "timezone": "America/Los_Angeles",
                    "loc": "37.4056,-122.0775",
                    "ts_retrieved": "2026-06-08T00:00:00+00:00",
                },
            ):
                did_work = executor.process_event(planned_event)

            self.assertTrue(did_work)
            round1_event = storage.get_event(event.event_id)
            self.assertIsNotNone(round1_event)
            assert round1_event is not None
            self.assertEqual(round1_event.event_status, EventStatus.REVIEWING)

            round1_executions = storage.list_executions(event.event_id, 1)
            self.assertEqual(len(round1_executions), 1)
            self.assertEqual(round1_executions[0].tool_name, "ipinfo_lookup_ips")
            self.assertEqual(round1_executions[0].result["summary"][0]["ip"], "8.8.8.8")

            with patch(
                "src.agent.reviewer.call_llm",
                return_value="已完成 8.8.8.8 的基础情报查询，拿到了国家、ASN 和组织归属。当前没有新的关键调查方向，建议保持 TTT 不变，继续执行公网 IP 8.8.8.8 的威胁情报查询。",
            ):
                reviewed = reviewer.process_event(round1_event)

            self.assertTrue(reviewed)
            replanning_event = storage.get_event(event.event_id)
            self.assertIsNotNone(replanning_event)
            assert replanning_event is not None
            self.assertEqual(replanning_event.event_status, EventStatus.REPLANNING)

            round2_event = planner.process_replanning(replanning_event)
            self.assertEqual(round2_event.event_status, EventStatus.PLANNED)
            self.assertEqual(round2_event.current_round, 2)

            round2_ttt = ttt_store.get_latest_ttt(event.event_id)
            self.assertIsNotNone(round2_ttt)
            assert round2_ttt is not None
            leaves = round2_ttt.root_nodes[0].children[0].children
            self.assertEqual(leaves[0].status.value, "done")
            self.assertEqual(leaves[1].status.value, "todo")

            vt_payload = {
                "data": {
                    "attributes": {
                        "reputation": 0,
                        "country": "US",
                        "as_owner": "Google LLC",
                        "asn": 15169,
                        "network": "8.8.8.0/24",
                        "last_analysis_date": 1717804800,
                        "last_analysis_stats": {
                            "malicious": 0,
                            "suspicious": 0,
                            "harmless": 92,
                            "undetected": 5,
                            "timeout": 0,
                        },
                        "last_analysis_results": {},
                    }
                }
            }
            with patch("src.agent.executor.call_llm", return_value="tool_name: get_ip_report"), patch.dict(
                os.environ,
                {"VT_API_KEY": "demo-key"},
                clear=False,
            ), patch("src.tools.virustotal._fetch_ip_report", return_value=vt_payload):
                did_work = executor.process_event(round2_event)

            self.assertTrue(did_work)
            round2_reviewing_event = storage.get_event(event.event_id)
            self.assertIsNotNone(round2_reviewing_event)
            assert round2_reviewing_event is not None
            self.assertEqual(round2_reviewing_event.event_status, EventStatus.REVIEWING)

            round2_executions = storage.list_executions(event.event_id, 2)
            self.assertEqual(len(round2_executions), 1)
            self.assertEqual(round2_executions[0].tool_name, "get_ip_report")
            self.assertEqual(round2_executions[0].result["summary"]["ip_address"], "8.8.8.8")

            with patch(
                "src.agent.reviewer.call_llm",
                return_value="已完成 8.8.8.8 的基础情报和 VirusTotal 威胁情报查询。当前没有新的证据缺口，建议结束这一最小排查流程。",
            ):
                reviewed = reviewer.process_event(round2_reviewing_event)

            self.assertTrue(reviewed)
            final_replanning_event = storage.get_event(event.event_id)
            self.assertIsNotNone(final_replanning_event)
            assert final_replanning_event is not None
            self.assertEqual(final_replanning_event.event_status, EventStatus.REPLANNING)

            final_planner_responses = [
                """
ttt:
  root_nodes:
    - title: 方向一：评估公网 IP 8.8.8.8 的风险背景
      status: done
      children:
        - title: 问题1.1：公网 IP 8.8.8.8 的基础归属和外部信誉如何？
          status: done
          children:
            - title: 查询公网 IP 8.8.8.8 的基础情报
              status: done
              children: []
            - title: 查询公网 IP 8.8.8.8 的威胁情报报告
              status: done
              children: []
""",
                "整体结论：8.8.8.8 的基础归属与外部威胁情报已补齐，当前未见明确恶意命中，这条最小 procedural flow 已按预期完成。",
            ]
            with patch("src.agent.planner.call_llm", side_effect=final_planner_responses):
                completed_event = planner.process_replanning(final_replanning_event)

            self.assertEqual(completed_event.event_status, EventStatus.COMPLETED)
            self.assertEqual(completed_event.current_round, 3)

            all_executions = storage.list_executions(event.event_id)
            self.assertEqual([item.tool_name for item in all_executions], ["ipinfo_lookup_ips", "get_ip_report"])
            self.assertTrue(all(item.execution_status.value == "completed" for item in all_executions))


if __name__ == "__main__":
    unittest.main()
