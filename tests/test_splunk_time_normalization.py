from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.splunk import (
    SplunkQuerySpec,
    SplunkSearchTool,
    _canonicalize_query_spec,
    _fill_partial_time_terms,
    _normalize_time_term,
)


class SplunkTimeNormalizationTests(unittest.TestCase):
    def test_normalize_time_term_converts_iso_datetime_to_epoch(self) -> None:
        self.assertEqual(_normalize_time_term("2026-06-07T16:43:00"), "1780850580")

    def test_normalize_time_term_keeps_relative_time_terms(self) -> None:
        self.assertEqual(_normalize_time_term("-24h"), "-24h")
        self.assertEqual(_normalize_time_term("now"), "now")

    def test_build_query_omits_time_terms_by_default(self) -> None:
        tool = SplunkSearchTool()
        query = tool.build_query(
            SplunkQuerySpec(
                dataset="botsv1",
                earliest="2026-06-07T16:43:00",
                latest="2026-06-07T17:00:00",
                keywords=("failed login",),
            )
        )
        self.assertNotIn('earliest="1780850580"', query)
        self.assertNotIn('latest="1780851600"', query)

    def test_fill_partial_time_terms_uses_date_from_intent(self) -> None:
        patched = _fill_partial_time_terms(
            {
                "dataset": "botsv1",
                "earliest": "04:40:00",
                "latest": "05:10:00",
            },
            intent="2016-08-19 05:10:00，外部 IP 40.80.148.42 持续访问目标站点。",
            additional_context={},
        )
        self.assertEqual(patched["earliest"], "2016-08-19T04:40:00")
        self.assertEqual(patched["latest"], "2016-08-19T05:10:00")

    def test_build_query_still_omits_time_terms_after_patch(self) -> None:
        tool = SplunkSearchTool()
        patched = _fill_partial_time_terms(
            {
                "dataset": "botsv1",
                "earliest": "04:40:00",
                "latest": "05:10:00",
                "keywords": ["web"],
            },
            intent="2016-08-19 05:10:00，外部 IP 40.80.148.42 持续访问目标站点。",
            additional_context={},
        )
        query = tool.build_query(SplunkQuerySpec.from_dict(patched))
        self.assertNotIn('earliest="1471581600"', query)
        self.assertNotIn('latest="1471583400"', query)

    def test_canonicalize_query_spec_rewrites_stream_http_fields(self) -> None:
        patched = _canonicalize_query_spec(
            {
                "dataset": "botsv1",
                "sourcetype": "stream:http",
                "field_filters": {
                    "src": "40.80.148.42",
                    "dest": "imreallynotbatman.com",
                },
            }
        )
        self.assertEqual(patched["field_filters"]["src_ip"], "40.80.148.42")
        self.assertNotIn("src", patched["field_filters"])
        self.assertNotIn("dest", patched["field_filters"])
        self.assertIn("imreallynotbatman.com", patched["keywords"])

    def test_build_query_prefers_src_ip_and_keyword_for_http_domain_targets(self) -> None:
        tool = SplunkSearchTool()
        patched = _canonicalize_query_spec(
            {
                "dataset": "botsv1",
                "sourcetype": "stream:http",
                "earliest": "2016-08-19T04:40:00",
                "latest": "2016-08-19T05:10:00",
                "field_filters": {
                    "src": "40.80.148.42",
                    "dest": "imreallynotbatman.com",
                },
            }
        )
        query = tool.build_query(SplunkQuerySpec.from_dict(patched))
        self.assertIn('sourcetype="stream:http"', query)
        self.assertIn('src_ip="40.80.148.42"', query)
        self.assertIn('imreallynotbatman.com', query)
        self.assertNotIn('dest="imreallynotbatman.com"', query)

    def test_search_returns_success_when_coarse_search_finds_no_logs(self) -> None:
        tool = SplunkSearchTool()
        spec = SplunkQuerySpec(
            dataset="botsv1",
            sourcetype="stream:http",
            field_filters={"src_ip": "40.80.148.42", "method": "POST"},
            keywords=("imreallynotbatman.com",),
        )
        with patch.object(tool, "_execute_search", return_value=[]):
            result = tool.search(spec=spec)

        self.assertTrue(result["success"])
        self.assertTrue(result["no_data_found"])
        self.assertEqual(result["search_stage"], "coarse_only")
        self.assertEqual(result["result_count"], 0)
        self.assertIn("No matching logs found in coarse search", result["warnings"])

    def test_search_falls_back_to_coarse_results_when_refined_search_is_empty(self) -> None:
        tool = SplunkSearchTool()
        spec = SplunkQuerySpec(
            dataset="botsv1",
            sourcetype="stream:http",
            field_filters={"src_ip": "40.80.148.42", "http_method": "POST"},
            keywords=("imreallynotbatman.com",),
            limit=5,
        )
        coarse_rows = [{"src_ip": "40.80.148.42", "uri": "/administrator/index.php"}]
        with patch.object(tool, "_execute_search", side_effect=[coarse_rows, []]):
            result = tool.search(spec=spec)

        self.assertTrue(result["success"])
        self.assertFalse(result["no_data_found"])
        self.assertEqual(result["search_stage"], "coarse_then_refined")
        self.assertTrue(result["used_fallback_results"])
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["coarse_result_count"], 1)
        self.assertIn("Refined search returned no events", " ".join(result["warnings"]))


if __name__ == "__main__":
    unittest.main()
