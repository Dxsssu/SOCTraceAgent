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
            message="SOC investigation: please use Splunk to analyze network-related logs in botsv1.",
            context={
                "splunk_dataset": "botsv1",
                "splunk_fields": ["_time", "src", "dest", "query", "answer", "sourcetype"],
            },
            source="integration_test",
            severity=SeverityLevel.MEDIUM,
        )
        node = TTTNode(
            node_id="1-1-1",
            title="Query sample DNS request logs in botsv1, confirm the common source IP, destination IP, query, and answer fields, and return the first few results.",
        )

        tool_selection = runtime._select_tool(node, event)
        success, result, error_message, tool_input = runtime._execute_tool(
            event,
            node,
            tool_selection["tool_name"],
            tool_selection=tool_selection,
        )
        query = str(tool_input.get("query") or "")

        print("\n=== Selected Tool ===")
        print(json.dumps(tool_selection, ensure_ascii=False, indent=2))
        print("\n=== Tool Input ===")
        print(json.dumps(tool_input, ensure_ascii=False, indent=2))
        print("\n=== Generated SPL ===")
        print(query)
        print("\n=== Search Summary ===")
        print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))
        print("\n=== Sample Events ===")
        print(json.dumps(result.get("sample_events", []), ensure_ascii=False, indent=2))

        self.assertEqual(tool_selection["tool_name"], "log_search")
        self.assertTrue(query.strip())
        self.assertIn("index=botsv1", query)
        self.assertTrue(success, error_message)
        self.assertEqual(result["query"], query)
        self.assertGreater(result["result_count"], 0, json.dumps(result, ensure_ascii=False, indent=2))
        self.assertGreater(len(result["sample_events"]), 0, json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main()
