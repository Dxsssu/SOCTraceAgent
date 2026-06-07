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


class ExecutorToolRoutingTests(unittest.TestCase):
    def test_executor_selects_and_runs_log_search_tool_from_ttt_leaf(self) -> None:
        runtime = ExecutorRuntime()
        event = Event(
            event_id="event-executor-routing-log-search",
            event_name="Windows Login Log Search Test",
            message="SOC 调查：需要查询 Windows 登录相关日志。",
            context={
                "splunk_dataset": "botsv1",
            },
            source="integration_test",
            severity=SeverityLevel.MEDIUM,
        )
        node = TTTNode(
            node_id="1-2-1",
            title="查询windows登陆日志",
        )

        tool_selection = runtime._select_tool(node, event)
        success, result, error_message, tool_input = runtime._execute_tool(
            event,
            node,
            tool_selection["tool_name"],
            tool_selection=tool_selection,
        )

        print("\n=== TTT Leaf Node ===")
        print(json.dumps(node.to_dict(), ensure_ascii=False, indent=2))
        print("\n=== Selected Tool ===")
        print(json.dumps(tool_selection, ensure_ascii=False, indent=2))
        print("\n=== Tool Input ===")
        print(json.dumps(tool_input, ensure_ascii=False, indent=2))
        print("\n=== Execution Success ===")
        print(success)
        print("\n=== Execution Error ===")
        print(error_message)
        print("\n=== Execution Result ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        self.assertEqual(tool_selection["tool_name"], "log_search")
        self.assertIn("intent", tool_input)
        self.assertIn("query_spec", tool_input)
        self.assertIn("query", result)


if __name__ == "__main__":
    unittest.main()
