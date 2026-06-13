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

from src.tools import get_registered_tool, list_registered_tools


class VirusTotalToolTests(unittest.TestCase):
    def test_get_ip_report_is_registered(self) -> None:
        tool = get_registered_tool("get_ip_report")
        self.assertIsNotNone(tool)

        names = {item.name for item in list_registered_tools(include_non_routable=False)}
        self.assertIn("get_ip_report", names)

    def test_get_ip_report_returns_real_result(self) -> None:
        tool = get_registered_tool("get_ip_report")
        assert tool is not None

        intent = "Please query the VirusTotal report for IP 8.8.8.8 and return detection statistics plus ASN information."
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
