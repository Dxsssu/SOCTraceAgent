from __future__ import annotations

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

IPINFO_BASE_URL = "https://ipapi.co"
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

mcp = register_server(FastMCP(
    "ip_info",
    instructions="IP intelligence and geolocation server backed by ipapi.co.",
))


@mcp.tool()
def get_ip_info(intent: str) -> dict[str, Any]:
    """
    Fetch geolocation and network information for an IP address mentioned in the investigation intent.

    Use this tool when the task asks for IP ownership, ASN, country, city, geolocation,
    or other basic external context about a public IP address. This tool is read-only and
    uses ipapi.co. It is best for single-IP enrichment, not for bulk lookups or threat intel
    verdicts such as maliciousness scoring.

    Args:
        intent: Natural-language investigation request that includes an IP address.

    Returns:
        A structured result containing the resolved IP, raw provider response, a compact
        summary, and any execution error message.
    """
    ip_address = _extract_ip_address(intent)
    if ip_address is None:
        return {
            "success": False,
            "result": {
                "summary": {},
                "ip_info": {},
            },
            "error_message": "query_build_error:No valid IP address found in intent",
            "tool_input": {
                "intent": intent,
                "ip_address": "",
            },
        }

    try:
        payload = _fetch_ip_info(ip_address)
    except RuntimeError as exc:
        return {
            "success": False,
            "result": {
                "summary": {},
                "ip_info": {},
            },
            "error_message": f"connection_error:{exc}",
            "tool_input": {
                "intent": intent,
                "ip_address": ip_address,
            },
        }

    if payload.get("error"):
        message = str(payload.get("reason") or payload.get("error") or "IP info lookup failed")
        return {
            "success": False,
            "result": {
                "summary": _build_summary(ip_address, payload),
                "ip_info": payload,
            },
            "error_message": f"query_execution_error:{message}",
            "tool_input": {
                "intent": intent,
                "ip_address": ip_address,
            },
        }

    return {
        "success": True,
        "result": {
            "summary": _build_summary(ip_address, payload),
            "ip_info": payload,
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


def _fetch_ip_info(ip_address: str) -> dict[str, Any]:
    api_url = _build_ipapi_url(ip_address)
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
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"ipapi.co returned HTTP {exc.code}: {body or exc.reason}") from exc
    except error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ipapi.co returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("ipapi.co returned an unexpected response shape")
    return parsed


def _build_ipapi_url(ip_address: str) -> str:
    api_key = os.environ.get("IPAPI_API_KEY", "").strip()
    base_url = f"{IPINFO_BASE_URL}/{ip_address}/json/"
    if not api_key:
        return base_url
    query_string = parse.urlencode({"key": api_key})
    return f"{base_url}?{query_string}"


def _build_summary(ip_address: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ip_address": ip_address,
        "city": str(payload.get("city") or ""),
        "region": str(payload.get("region") or ""),
        "country_name": str(payload.get("country_name") or ""),
        "country_code": str(payload.get("country_code") or ""),
        "org": str(payload.get("org") or ""),
        "asn": str(payload.get("asn") or ""),
        "postal": str(payload.get("postal") or ""),
        "timezone": str(payload.get("timezone") or ""),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
    }


__all__ = [
    "get_ip_info",
    "mcp",
]
