from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.ipinfo import _extract_ip_addresses
from src.tools.virustotal import _extract_ip_address


class ToolIPExtractionTests(unittest.TestCase):
    def test_ipinfo_extracts_ipv4_followed_by_chinese_text(self) -> None:
        extracted = _extract_ip_addresses("查询源IP 1.2.3.4的基础信息（地理位置、ASN、历史活动）")
        self.assertEqual(extracted, ["1.2.3.4"])

    def test_virustotal_extracts_ipv4_followed_by_chinese_text(self) -> None:
        extracted = _extract_ip_address("查询源IP 1.2.3.4的威胁情报标签与信誉评分")
        self.assertEqual(extracted, "1.2.3.4")


if __name__ == "__main__":
    unittest.main()
