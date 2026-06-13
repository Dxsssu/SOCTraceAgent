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
    def test_ipinfo_extracts_ipv4_followed_by_text(self) -> None:
        extracted = _extract_ip_addresses("Look up the basic information for source IP 1.2.3.4, including geolocation, ASN, and prior activity.")
        self.assertEqual(extracted, ["1.2.3.4"])

    def test_virustotal_extracts_ipv4_followed_by_text(self) -> None:
        extracted = _extract_ip_address("Look up threat-intelligence labels and reputation scoring for source IP 1.2.3.4.")
        self.assertEqual(extracted, "1.2.3.4")


if __name__ == "__main__":
    unittest.main()
