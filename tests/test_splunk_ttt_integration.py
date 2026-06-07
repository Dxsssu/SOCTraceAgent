from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")

from src.agent.executor import ExecutorRuntime
from src.schema import Event, SeverityLevel, TTTNode


class SplunkTTTIntegrationTests(unittest.TestCase):
    def test_ttt_leaf_natural_language_query_end_to_end(self) -> None:
        runtime = ExecutorRuntime()
        event = Event(
            event_id="event-live-splunk-ttt",
            event_name="Live Splunk TTT Query Test",
            message="SOC 调查：请使用 Splunk 分析 botsv1 中的网络相关日志。",
            context={
                "splunk_dataset": "botsv1",
                "splunk_fields": ["_time", "src", "dest", "query", "answer", "sourcetype"],
            },
            source="integration_test",
            severity=SeverityLevel.MEDIUM,
        )
        node = TTTNode(
            node_id="1-1-1",
            title="查询 botsv1 中的 DNS 请求日志样本，确认常见的源 IP、目的 IP、query 和 answer 字段，返回前几条结果。",
        )

        query_spec, strategy, translation_error = runtime._derive_log_search_spec(event, node)
        query = runtime.splunk_tool.build_query(query_spec)
        result = runtime.splunk_tool.search(
            spec=query_spec,
            additional_context={
                "event": event.to_dict(),
                "node": {"node_id": node.node_id, "title": node.title},
            },
        )

        print("\n=== Translation Strategy ===")
        print(strategy)
        if translation_error:
            print("\n=== Translation Error ===")
            print(translation_error)
        print("\n=== Query Spec ===")
        print(json.dumps(query_spec.to_dict(), ensure_ascii=False, indent=2))
        print("\n=== Generated SPL ===")
        print(query)
        print("\n=== Search Summary ===")
        print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))
        print("\n=== Sample Events ===")
        print(json.dumps(result.get("sample_events", []), ensure_ascii=False, indent=2))

        self.assertEqual(strategy, "llm_intent_translation", translation_error)
        self.assertTrue(query.strip())
        self.assertIn("index=botsv1", query)
        self.assertTrue(result["success"], result["error_message"])
        self.assertEqual(result["query"], query)
        self.assertGreater(result["result_count"], 0, json.dumps(result, ensure_ascii=False, indent=2))
        self.assertGreater(len(result["sample_events"]), 0, json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main()
