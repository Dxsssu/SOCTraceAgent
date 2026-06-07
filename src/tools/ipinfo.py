from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import ssl
from typing import Any
from urllib import error, parse, request

from dotenv import load_dotenv

from .mcp import FastMCP, register_server

load_dotenv()

IPINFO_BASE_URL = "https://ipinfo.io"
IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:[0-9A-Fa-f:]+:+)+[0-9A-Fa-f]+\b")

mcp = register_server(FastMCP(
    "ipinfo",
    instructions="IP geolocation and ASN intelligence server backed by ipinfo.io.",
))


@mcp.tool()
def ipinfo_lookup_ips(intent: str) -> dict[str, Any]:
    """
    Geolocate one or more IPs and return country, city, ISP, and ASN details.

    Use this tool when the task asks for IP geolocation, ISP ownership, ASN context,
    city/region/country, or other basic IPInfo lookups. This tool is read-only and uses
    ipinfo.io. Without IPINFO_API_TOKEN it falls back to Lite-tier data, so only basic
    country and ASN fields may be present.

    Args:
        intent: Natural-language investigation request that includes one or more IP addresses.

    Returns:
        A structured result containing summarized IPInfo records, filtered/skipped IPs,
        and any execution error message.
    """
    extracted_ips = _extract_ip_addresses(intent)
    if not extracted_ips:
        return {
            "success": False,
            "result": {
                "summary": [],
                "ipinfo_results": [],
                "skipped": [],
            },
            "error_message": "query_build_error:No valid IP address found in intent",
            "tool_input": {
                "intent": intent,
                "ips": [],
            },
        }

    valid_ips: list[str] = []
    skipped: list[dict[str, str]] = []
    for ip_text in extracted_ips:
        reason = _unsupported_reason(ip_text)
        if reason:
            skipped.append({"ip": ip_text, "reason": reason})
        else:
            valid_ips.append(ip_text)

    if not valid_ips:
        return {
            "success": False,
            "result": {
                "summary": [],
                "ipinfo_results": [],
                "skipped": skipped,
            },
            "error_message": "query_build_error:All extracted IPs are special-use or unsupported",
            "tool_input": {
                "intent": intent,
                "ips": extracted_ips,
            },
        }

    try:
        results = [_fetch_ipinfo_record(ip_address) for ip_address in valid_ips]
    except RuntimeError as exc:
        return {
            "success": False,
            "result": {
                "summary": [],
                "ipinfo_results": [],
                "skipped": skipped,
            },
            "error_message": str(exc),
            "tool_input": {
                "intent": intent,
                "ips": valid_ips,
            },
        }

    return {
        "success": True,
        "result": {
            "summary": [_summarize_record(item) for item in results],
            "ipinfo_results": results,
            "skipped": skipped,
        },
        "error_message": "",
        "tool_input": {
            "intent": intent,
            "ips": valid_ips,
        },
    }


def _extract_ip_addresses(intent: str) -> list[str]:
    seen: set[str] = set()
    extracted: list[str] = []
    for candidate in IP_PATTERN.findall(intent):
        try:
            ip_text = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if ip_text in seen:
            continue
        seen.add(ip_text)
        extracted.append(ip_text)
    return extracted


def _unsupported_reason(ip_text: str) -> str:
    parsed = ipaddress.ip_address(ip_text)
    if parsed.is_private:
        return "private_ip"
    if parsed.is_loopback:
        return "loopback_ip"
    if parsed.is_multicast:
        return "multicast_ip"
    if parsed.is_reserved:
        return "reserved_ip"
    if parsed.is_unspecified:
        return "unspecified_ip"
    return ""


def _fetch_ipinfo_record(ip_address: str) -> dict[str, Any]:
    token = os.environ.get("IPINFO_API_TOKEN", "").strip()
    query = parse.urlencode({"token": token}) if token else ""
    suffix = f"?{query}" if query else ""
    api_url = f"{IPINFO_BASE_URL}/{ip_address}/json{suffix}"
    http_request = request.Request(
        api_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SOCTraceAgent/1.0",
        },
    )
    try:
        with request.urlopen(http_request, timeout=20, context=ssl.create_default_context()) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise RuntimeError(_normalize_ipinfo_http_error(exc)) from exc
    except error.URLError as exc:
        raise RuntimeError(f"connection_error:{exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("query_execution_error:ipinfo.io returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("query_execution_error:ipinfo.io returned an unexpected response shape")
    if parsed.get("error"):
        message = str(parsed.get("reason") or parsed.get("title") or "ipinfo.io request failed")
        raise RuntimeError(f"query_execution_error:{message}")

    flattened = _flatten_nested_response(parsed)
    flattened["ts_retrieved"] = datetime.now(timezone.utc).isoformat()
    return flattened


def _flatten_nested_response(details: dict[str, Any]) -> dict[str, Any]:
    out = dict(details)

    geo = out.pop("geo", None)
    if isinstance(geo, dict):
        geo = dict(geo)
        if "country" in geo:
            geo["country_name"] = geo.pop("country")
        if "country_code" in geo:
            geo["country"] = geo.pop("country_code")
        for key, value in geo.items():
            out.setdefault(key, value)

    as_block = out.pop("as", None)
    if isinstance(as_block, dict):
        if "asn" not in out and "asn" in as_block:
            out["asn"] = as_block.get("asn")
        if "name" in as_block and "org" not in out:
            out["org"] = as_block.get("name")
        if "domain" in as_block and "domain" not in out:
            out["domain"] = as_block.get("domain")
        if "type" in as_block and "asn_type" not in out:
            out["asn_type"] = as_block.get("type")

    return out


def _normalize_ipinfo_http_error(exc: error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("error") or payload.get("reason") or payload.get("title") or "").strip()

    if exc.code == 401:
        return "configuration_error:ipinfo.io rejected IPINFO_API_TOKEN (401 Unauthorized)."
    if exc.code == 403:
        return "configuration_error:IPINFO_API_TOKEN is invalid or lacks access for this endpoint."
    if exc.code == 429:
        return "rate_limit_error:ipinfo.io rate limit hit (429). Retry later or upgrade your plan."
    if exc.code == 404:
        return "query_execution_error:ipinfo.io returned 404 for this IP."
    suffix = f": {message}" if message else ""
    return f"query_execution_error:ipinfo.io request failed ({exc.code}){suffix}"


def _summarize_record(record: dict[str, Any]) -> dict[str, Any]:
    location = str(record.get("loc") or "")
    latitude = ""
    longitude = ""
    if "," in location:
        latitude, longitude = location.split(",", 1)
    return {
        "ip": str(record.get("ip") or ""),
        "city": str(record.get("city") or ""),
        "region": str(record.get("region") or ""),
        "country": str(record.get("country") or ""),
        "country_name": str(record.get("country_name") or ""),
        "org": str(record.get("org") or ""),
        "asn": str(record.get("asn") or ""),
        "asn_type": str(record.get("asn_type") or ""),
        "postal": str(record.get("postal") or ""),
        "timezone": str(record.get("timezone") or ""),
        "latitude": latitude,
        "longitude": longitude,
        "ts_retrieved": str(record.get("ts_retrieved") or ""),
    }


__all__ = [
    "ipinfo_lookup_ips",
    "mcp",
]
