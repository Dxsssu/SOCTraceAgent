from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import ssl
from typing import Any
from urllib import error, request

from dotenv import load_dotenv

from .mcp import FastMCP, register_server

load_dotenv()

VT_BASE_URL = "https://www.virustotal.com/api/v3"
VT_GUI_BASE_URL = "https://www.virustotal.com/gui"
IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

mcp = register_server(FastMCP(
    "virustotal",
    instructions="VirusTotal reputation and report lookup server.",
))


@mcp.tool()
def get_ip_report(intent: str) -> dict[str, Any]:
    """
    Look up an IP address's VirusTotal report: detection stats, reputation, AS owner, country.

    Use this tool when the task asks for external IP reputation, malicious detection counts,
    ASN ownership, country, or a VirusTotal report for a specific IP address. This tool is
    read-only and requires VT_API_KEY to be configured for the VirusTotal v3 API.

    Args:
        intent: Natural-language investigation request that includes an IP address.

    Returns:
        A structured result containing a compact VirusTotal summary, the raw IP report,
        and any execution error message.
    """
    ip_address = _extract_ip_address(intent)
    if ip_address is None:
        return {
            "success": False,
            "result": {
                "summary": {},
                "virustotal_report": {},
            },
            "error_message": "query_build_error:No valid IP address found in intent",
            "tool_input": {
                "intent": intent,
                "ip_address": "",
            },
        }

    api_key = os.environ.get("VT_API_KEY", "").strip()
    if not api_key:
        return {
            "success": False,
            "result": {
                "summary": {},
                "virustotal_report": {},
            },
            "error_message": "configuration_error:VT_API_KEY is not set. Get a free key at https://www.virustotal.com/gui/my-apikey.",
            "tool_input": {
                "intent": intent,
                "ip_address": ip_address,
            },
        }

    try:
        payload = _fetch_ip_report(ip_address, api_key)
    except RuntimeError as exc:
        return {
            "success": False,
            "result": {
                "summary": {},
                "virustotal_report": {},
            },
            "error_message": str(exc),
            "tool_input": {
                "intent": intent,
                "ip_address": ip_address,
            },
        }

    return {
        "success": True,
        "result": {
            "summary": _build_summary(ip_address, payload),
            "virustotal_report": payload,
        },
        "error_message": "",
        "tool_input": {
            "intent": intent,
            "ip_address": ip_address,
        },
    }


def _extract_ip_address(intent: str) -> str | None:
    for candidate in IPV4_PATTERN.findall(intent):
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue

    for token in re.findall(r"[0-9A-Fa-f:.]+", intent):
        if ":" not in token:
            continue
        try:
            return str(ipaddress.ip_address(token))
        except ValueError:
            continue
    return None


def _fetch_ip_report(ip_address: str, api_key: str) -> dict[str, Any]:
    api_url = f"{VT_BASE_URL}/ip_addresses/{ip_address}"
    http_request = request.Request(
        api_url,
        headers={
            "x-apikey": api_key,
            "Accept": "application/json",
            "User-Agent": "SOCTraceAgent/1.0",
        },
    )
    try:
        with request.urlopen(http_request, timeout=30, context=ssl.create_default_context()) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        message = _normalize_vt_http_error(exc)
        raise RuntimeError(message) from exc
    except error.URLError as exc:
        raise RuntimeError(f"connection_error:{exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("query_execution_error:VirusTotal returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("query_execution_error:VirusTotal returned an unexpected response shape")
    return parsed


def _normalize_vt_http_error(exc: error.HTTPError) -> str:
    status = exc.code
    try:
        body = exc.read().decode("utf-8")
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}
    detail = ""
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            detail = str(error_payload.get("message") or error_payload.get("code") or "").strip()

    if status == 401:
        return "configuration_error:VirusTotal rejected VT_API_KEY (401 Unauthorized)."
    if status == 404:
        return "query_execution_error:Not found in VirusTotal (404) — this IP has not been seen or analyzed yet."
    if status == 429:
        return "rate_limit_error:VirusTotal rate limit hit (429). The free tier allows 4 requests/min and 500/day."
    suffix = f": {detail}" if detail else ""
    return f"query_execution_error:VirusTotal request failed ({status}){suffix}"


def _build_summary(ip_address: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    stats = _detection_stats(attributes.get("last_analysis_stats") or {})
    return {
        "ip_address": ip_address,
        "reputation": attributes.get("reputation"),
        "country": str(attributes.get("country") or ""),
        "as_owner": str(attributes.get("as_owner") or ""),
        "asn": attributes.get("asn"),
        "network": str(attributes.get("network") or ""),
        "last_analysis_date": _iso_date(attributes.get("last_analysis_date")),
        "stats": stats,
        "notable": _top_detections(attributes.get("last_analysis_results") or {}),
        "permalink": f"{VT_GUI_BASE_URL}/ip-address/{ip_address}",
    }


def _detection_stats(stats: dict[str, Any]) -> dict[str, int]:
    malicious = int(stats.get("malicious") or 0)
    suspicious = int(stats.get("suspicious") or 0)
    harmless = int(stats.get("harmless") or 0)
    undetected = int(stats.get("undetected") or 0)
    timeout = int(stats.get("timeout") or 0)
    return {
        "malicious": malicious,
        "suspicious": suspicious,
        "harmless": harmless,
        "undetected": undetected,
        "timeout": timeout,
        "total": malicious + suspicious + harmless + undetected + timeout,
    }


def _top_detections(results: dict[str, Any], limit: int = 5) -> list[str]:
    findings: list[str] = []
    for value in results.values():
        if not isinstance(value, dict):
            continue
        category = str(value.get("category") or "")
        if category not in {"malicious", "suspicious"}:
            continue
        engine_name = str(value.get("engine_name") or "unknown_engine")
        verdict = str(value.get("result") or category)
        findings.append(f"{engine_name}: {verdict}")
        if len(findings) >= limit:
            break
    return findings


def _iso_date(unix_seconds: Any) -> str:
    if not isinstance(unix_seconds, (int, float)):
        return ""
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat()


__all__ = [
    "get_ip_report",
    "mcp",
]
