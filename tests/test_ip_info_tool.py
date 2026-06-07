from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools import get_registered_tool


class IPInfoToolTests(unittest.TestCase):
    def test_query_sentence_to_ip_info_result(self) -> None:
        tool = get_registered_tool("get_ip_info")
        self.assertIsNotNone(tool)

        intent = "请查询 IP 1.2.3.4 的信息。"
        result = tool.execute(intent=intent)

        print("\n=== Query Intent ===")
        print(intent)
        print("\n=== Tool Result ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        self.assertIn("success", result)
        self.assertIn("result", result)
        self.assertIn("error_message", result)


if __name__ == "__main__":
    unittest.main()
