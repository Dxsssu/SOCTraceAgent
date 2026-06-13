from __future__ import annotations

from dataclasses import dataclass, field
import base64
from datetime import datetime, timezone
import json
import os
import re
import ssl
import time
from typing import Any
from urllib import error, parse, request

from dotenv import load_dotenv

from .mcp import FastMCP, register_server

load_dotenv()


COMMON_EVENT_FIELDS = (
    "_time",
    "host",
    "source",
    "sourcetype",
    "index",
    "src",
    "src_ip",
    "dest",
    "dest_ip",
    "clientip",
    "ip",
    "user",
    "action",
    "signature",
    "_raw",
)
COMMON_IP_FIELDS = ("src", "src_ip", "dest", "dest_ip", "clientip", "ip")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_SAMPLE_EVENTS = 20
MAX_FIELD_LENGTH = 500
COARSE_QUERY_LIMIT = 10
COARSE_FIELD_FILTER_ALLOWLIST = frozenset(
    {
        "src",
        "src_ip",
        "dest",
        "dest_ip",
        "clientip",
        "ip",
        "host",
        "source",
        "c_ip",
        "s_ip",
    }
)

STREAM_HTTP_SOURCETYPE = "stream:http"
IIS_SOURCETYPE = "iis"
WINEVENTLOG_SOURCETYPE = "wineventlog"
SYSMON_SOURCETYPE = "xmlwineventlog:microsoft-windows-sysmon/operational"
DNS_SOURCETYPE = "stream:dns"
SMB_SOURCETYPE = "stream:smb"

mcp = register_server(FastMCP(
    "splunk",
    instructions="Splunk-backed investigation server for search planning, SPL construction, and log retrieval.",
))


class SplunkToolError(RuntimeError):
    """Raised when the Splunk tool cannot build or execute a query."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SplunkDatasetConfig:
    name: str
    base_url: str
    index: str


@dataclass(frozen=True, slots=True)
class SourcetypeCatalogEntry:
    sourcetype: str
    description: str
    representative_fields: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DatasetCatalogEntry:
    dataset: str
    description: str
    sourcetypes: tuple[SourcetypeCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class SplunkToolConfig:
    username: str
    password: str
    verify_tls: bool
    default_dataset: str
    datasets: dict[str, SplunkDatasetConfig]
    request_timeout: float = 30.0
    poll_interval: float = 1.0
    poll_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "SplunkToolConfig":
        datasets: dict[str, SplunkDatasetConfig] = {}
        for dataset_name, default_url in (
            ("botsv1", "http://127.0.0.1:8000"),
            ("botsv2", "http://127.0.0.1:8020"),
            ("botsv3", "http://127.0.0.1:8030"),
        ):
            prefix = dataset_name.upper()
            datasets[dataset_name] = SplunkDatasetConfig(
                name=dataset_name,
                base_url=os.environ.get(f"SPLUNK_{prefix}_BASE_URL", default_url).strip().rstrip("/"),
                index=os.environ.get(f"SPLUNK_{prefix}_INDEX", dataset_name).strip() or dataset_name,
            )

        return cls(
            username=os.environ.get("SPLUNK_USERNAME", "admin").strip() or "admin",
            password=os.environ.get("SPLUNK_PASSWORD", "").strip(),
            verify_tls=os.environ.get("SPLUNK_VERIFY_TLS", "false").strip().lower() in {"1", "true", "yes", "on"},
            default_dataset=os.environ.get("SPLUNK_DEFAULT_DATASET", "botsv1").strip().lower() or "botsv1",
            datasets=datasets,
            request_timeout=float(os.environ.get("SPLUNK_REQUEST_TIMEOUT", "30").strip() or "30"),
            poll_interval=float(os.environ.get("SPLUNK_POLL_INTERVAL", "1").strip() or "1"),
            poll_timeout=float(os.environ.get("SPLUNK_POLL_TIMEOUT", "30").strip() or "30"),
        )

    def resolve_dataset(self, dataset: str | None) -> SplunkDatasetConfig:
        name = (dataset or self.default_dataset).strip().lower()
        resolved = self.datasets.get(name)
        if resolved is None:
            raise SplunkToolError(
                "configuration_error",
                f"Unsupported Splunk dataset: {name}",
            )
        return resolved


BOTSV1_SOURCETYPES: tuple[SourcetypeCatalogEntry, ...] = (
    SourcetypeCatalogEntry("stream:sip", "SIP communication logs", ("src_ip", "dest_ip", "method", "request_call_id", "caller_user_phone", "callee_user_phone")),
    SourcetypeCatalogEntry("stream:snmp", "SNMP access logs", ("src_ip", "dest_ip", "community{}", "method{}", "request_id", "timestamp")),
    SourcetypeCatalogEntry("stream:dhcp", "DHCP network-flow logs", ("src_ip", "dest_ip", "chaddr", "opcode", "dns_server", "lease_duration")),
    SourcetypeCatalogEntry("fgt_event", "FortiGate event logs", ("devname", "logdesc", "action", "interface", "dhcp_msg", "ip")),
    SourcetypeCatalogEntry("nessus:scan", "Nessus vulnerability scan results", ("host", "host-ip", "pluginName", "severity", "port", "protocol")),
    SourcetypeCatalogEntry("stream:ldap", "LDAP directory-access logs", ("src_ip", "dest_ip", "message_type", "message_id", "assertion_description{}", "assertion_value{}")),
    SourcetypeCatalogEntry("stream:mapi", "MAPI/mail-client protocol logs", ("src_ip", "dest_ip", "login", "domain", "auth_type", "login_server")),
    SourcetypeCatalogEntry("stream:dns", "DNS query logs", ("src_ip", "dest_ip", "query{}", "query_type{}", "answer", "rcode")),
    SourcetypeCatalogEntry("stream:icmp", "ICMP traffic logs", ("src_ip", "dest_ip", "code", "code_string", "sequence", "timestamp")),
    SourcetypeCatalogEntry("iis", "IIS web access logs", ("c_ip", "s_ip", "cs_method", "cs_uri_stem", "cs_uri_query", "sc_status")),
    SourcetypeCatalogEntry("stream:http", "HTTP request logs", ("src_ip", "dest_ip", "http_method", "site", "uri", "src_headers")),
    SourcetypeCatalogEntry("fgt_utm", "FortiGate UTM/threat-detection logs", ("srcip", "dstip", "file_name", "file_hash", "appcat", "action")),
    SourcetypeCatalogEntry("stream:tcp", "TCP session logs", ("src_ip", "dest_ip", "src_port", "dest_port", "connection", "refused")),
    SourcetypeCatalogEntry("fgt_traffic", "FortiGate traffic logs", ("srcip", "dstip", "srcport", "dstport", "app", "action")),
    SourcetypeCatalogEntry("stream:ip", "Generic IP traffic logs", ("src_ip", "dest_ip", "protocol", "bytes_in", "bytes_out", "packets")),
    SourcetypeCatalogEntry("WinRegistry", "Windows registry activity logs", ("host", "registry_path", "registry_value_name", "registry_value_data", "action", "process_image")),
    SourcetypeCatalogEntry("wineventlog", "Windows event logs for authentication, system, and application investigations", ("ComputerName", "EventCode", "Account_Name", "LogonType", "IpAddress", "Message")),
    SourcetypeCatalogEntry("suricata", "Suricata IDS alert logs", ("src_ip", "dest_ip", "signature", "category", "severity", "http.http_method")),
    SourcetypeCatalogEntry("stream:smb", "SMB file-sharing logs", ("src_ip", "dest_ip", "filename", "path", "command{}", "nt_status{}")),
    SourcetypeCatalogEntry("XmlWinEventLog:Microsoft-Windows-Sysmon/Operational", "Sysmon process, network, file, and registry behavior logs", ("EventCode", "Image", "CommandLine", "ParentImage", "SourceIp", "DestinationIp")),
)


DATASET_CATALOG: dict[str, DatasetCatalogEntry] = {
    "botsv1": DatasetCatalogEntry(
        dataset="botsv1",
        description="Boss of the SOC v1 attack dataset, including Windows, Sysmon, FortiGate, IIS, Stream, and Suricata logs.",
        sourcetypes=BOTSV1_SOURCETYPES,
    ),
    "botsv2": DatasetCatalogEntry(
        dataset="botsv2",
        description="Boss of the SOC v2 attack dataset, including Windows, Sysmon, Apache, IIS, PAN, Suricata, and Stream logs.",
        sourcetypes=BOTSV1_SOURCETYPES,
    ),
    "botsv3": DatasetCatalogEntry(
        dataset="botsv3",
        description="Boss of the SOC v3 dataset, covering cloud, Windows, Sysmon, O365, AWS, Suricata, and Stream logs.",
        sourcetypes=BOTSV1_SOURCETYPES,
    ),
}


@dataclass(frozen=True, slots=True)
class SplunkQuerySpec:
    dataset: str | None = None
    index: str | None = None
    sourcetype: str | None = None
    earliest: str | None = None
    latest: str | None = None
    keywords: tuple[str, ...] = field(default_factory=tuple)
    ip: str | None = None
    host: str | None = None
    source: str | None = None
    field_filters: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    limit: int = DEFAULT_LIMIT
    fields: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SplunkQuerySpec":
        return cls(
            dataset=_none_if_blank(data.get("dataset")),
            index=_none_if_blank(data.get("index")),
            sourcetype=_none_if_blank(data.get("sourcetype")),
            earliest=_none_if_blank(data.get("earliest")),
            latest=_none_if_blank(data.get("latest")),
            keywords=_normalize_string_sequence(data.get("keywords")),
            ip=_none_if_blank(data.get("ip")),
            host=_none_if_blank(data.get("host")),
            source=_none_if_blank(data.get("source")),
            field_filters=_normalize_field_filters(data.get("field_filters")),
            limit=_normalize_limit(data.get("limit")),
            fields=_normalize_string_sequence(data.get("fields")),
        )

    def to_dict(self) -> dict[str, Any]:
        normalized_filters: dict[str, Any] = {}
        for key, value in self.field_filters.items():
            normalized_filters[key] = list(value) if isinstance(value, tuple) else value
        return {
            "dataset": self.dataset,
            "index": self.index,
            "sourcetype": self.sourcetype,
            "earliest": self.earliest,
            "latest": self.latest,
            "keywords": list(self.keywords),
            "ip": self.ip,
            "host": self.host,
            "source": self.source,
            "field_filters": normalized_filters,
            "limit": self.limit,
            "fields": list(self.fields),
        }


class SplunkSearchTool:
    """Builds and executes Splunk SPL searches through the REST API."""

    def __init__(self, config: SplunkToolConfig | None = None) -> None:
        self.config = config or SplunkToolConfig.from_env()
        self._field_context_cache: dict[tuple[str, str], tuple[str, ...]] = {}

    def interpret_intent(
        self,
        intent: str,
        *,
        dataset: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> SplunkQuerySpec:
        if not intent.strip():
            raise SplunkToolError("query_build_error", "Search intent cannot be empty")

        dataset_name = dataset or self.config.default_dataset
        dataset_context = self.get_dataset_context(dataset_name)
        context_text = json.dumps(additional_context or {}, ensure_ascii=False, indent=2)
        user_prompt = "\n".join(
            [
                "Convert the TTT investigation intent below into an executable Splunk query spec and return YAML only.",
                "Only these fields are allowed: dataset, index, sourcetype, earliest, latest, keywords, ip, host, source, field_filters, limit, fields.",
                "field_filters values may only be strings or arrays of strings.",
                "Do not output SPL and do not provide extra explanation.",
                "You must use the dataset, sourcetype, and field-background information below to choose the most reasonable log scope.",
                f"default dataset: {dataset_name}",
                f"dataset_context: {json.dumps(dataset_context, ensure_ascii=False, indent=2)}",
                f"intent: {intent}",
                f"context: {context_text}",
            ]
        )
        response_text = call_llm(
            """
You are a Splunk SPL planner.
Your task is to convert a natural-language TTT investigation intent into a structured query spec that can later be turned into executable SPL.
Do not output SPL, explanatory prose, or any fields not listed in the schema.
You will receive full dataset background, including available sourcetypes and representative fields.
Prefer the most relevant sourcetype and add the most investigation-useful fields when appropriate.
If the intent does not specify fields, you may leave them empty, but do not invent nonexistent sourcetypes.
""".strip(),
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise SplunkToolError(
                "intent_parse_error",
                "LLM returned empty or non-YAML Splunk query spec",
            )
        if not parsed.get("dataset"):
            parsed["dataset"] = dataset_name
        parsed = _fill_partial_time_terms(
            parsed,
            intent=intent,
            additional_context=additional_context or {},
        )
        parsed = _canonicalize_query_spec(parsed)
        try:
            return SplunkQuerySpec.from_dict(parsed)
        except (TypeError, ValueError) as exc:
            raise SplunkToolError(
                "intent_parse_error",
                f"Failed to normalize Splunk query spec: {exc}",
            ) from exc

    def build_query(
        self,
        spec: SplunkQuerySpec | dict[str, Any],
        *,
        dataset: str | None = None,
    ) -> str:
        normalized = spec if isinstance(spec, SplunkQuerySpec) else SplunkQuerySpec.from_dict(spec)
        dataset_cfg = self.config.resolve_dataset(dataset or normalized.dataset)
        index = normalized.index or dataset_cfg.index
        if not index:
            raise SplunkToolError("configuration_error", "Splunk index is required")

        clauses = [f"search index={_quote_if_needed(index)}"]
        if normalized.sourcetype:
            clauses.append(f'sourcetype="{_escape_quotes(normalized.sourcetype)}"')
        if normalized.host:
            clauses.append(f'host="{_escape_quotes(normalized.host)}"')
        if normalized.source:
            clauses.append(f'source="{_escape_quotes(normalized.source)}"')
        if normalized.ip:
            clauses.append(
                "(" + " OR ".join(f'{field}="{_escape_quotes(normalized.ip)}"' for field in COMMON_IP_FIELDS) + ")"
            )
        for keyword in normalized.keywords:
            clauses.append(_quote_if_needed(keyword))
        for field_name, field_value in normalized.field_filters.items():
            if isinstance(field_value, tuple):
                values = ", ".join(f'"{_escape_quotes(item)}"' for item in field_value)
                clauses.append(f"{field_name} IN ({values})")
            else:
                clauses.append(f'{field_name}="{_escape_quotes(field_value)}"')

        query = " ".join(clauses)
        query += f" | head {normalized.limit}"
        if normalized.fields:
            table_fields = " ".join(normalized.fields)
            query += f" | table {table_fields}"
        return query

    def search(
        self,
        *,
        spec: SplunkQuerySpec | dict[str, Any] | None = None,
        intent: str | None = None,
        dataset: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            normalized_spec = self._resolve_search_spec(
                spec=spec,
                intent=intent,
                dataset=dataset,
                additional_context=additional_context,
            )
            dataset_cfg = self.config.resolve_dataset(dataset or normalized_spec.dataset)
            coarse_spec = self._build_coarse_query_spec(normalized_spec)
            coarse_result = self._run_search_stage(
                dataset_cfg=dataset_cfg,
                spec=coarse_spec,
                stage_name="coarse_only",
            )
            if int(coarse_result["result_count"]) == 0:
                coarse_result["warnings"].append("No matching logs found in coarse search")
                coarse_result["no_data_found"] = True
                return coarse_result

            if coarse_spec.to_dict() == normalized_spec.to_dict():
                coarse_result["search_stage"] = "coarse_only"
                coarse_result["coarse_query"] = coarse_result["query"]
                coarse_result["coarse_result_count"] = coarse_result["result_count"]
                coarse_result["coarse_query_spec"] = coarse_result["query_spec"]
                coarse_result["no_data_found"] = False
                return coarse_result

            refined_result = self._run_search_stage(
                dataset_cfg=dataset_cfg,
                spec=normalized_spec,
                stage_name="coarse_then_refined",
            )
            refined_result["coarse_query"] = coarse_result["query"]
            refined_result["coarse_result_count"] = coarse_result["result_count"]
            refined_result["coarse_query_spec"] = coarse_result["query_spec"]
            refined_result["no_data_found"] = False
            if int(refined_result["result_count"]) == 0:
                refined_result["warnings"].append(
                    "Refined search returned no events; falling back to coarse search samples"
                )
                refined_result["result_count"] = coarse_result["result_count"]
                refined_result["summary"] = coarse_result["summary"]
                refined_result["sample_events"] = coarse_result["sample_events"]
                refined_result["query"] = coarse_result["query"]
                refined_result["query_spec"] = coarse_result["query_spec"]
                refined_result["used_fallback_results"] = True
            return refined_result
        except SplunkToolError as exc:
            return {
                "success": False,
                "dataset": (dataset or getattr(spec, "dataset", None) or self.config.default_dataset),
                "query": "",
                "job_mode": "search_job_polling",
                "result_count": 0,
                "summary": {},
                "sample_events": [],
                "warnings": [],
                "error_message": f"{exc.code}:{exc.message}",
                "query_spec": spec.to_dict() if isinstance(spec, SplunkQuerySpec) else dict(spec or {}),
            }

    def _run_search_stage(
        self,
        *,
        dataset_cfg: SplunkDatasetConfig,
        spec: SplunkQuerySpec,
        stage_name: str,
    ) -> dict[str, Any]:
        query = self.build_query(spec, dataset=dataset_cfg.name)
        raw_results = self._execute_search(dataset_cfg, query)
        sample_events = self._format_sample_events(raw_results, spec.fields)
        warnings: list[str] = []
        return {
            "success": True,
            "dataset": dataset_cfg.name,
            "query": query,
            "job_mode": "search_job_polling",
            "result_count": len(raw_results),
            "summary": self._build_summary(dataset_cfg, spec, raw_results, sample_events),
            "sample_events": sample_events,
            "warnings": warnings,
            "error_message": "",
            "query_spec": spec.to_dict(),
            "search_stage": stage_name,
        }

    def _build_coarse_query_spec(self, spec: SplunkQuerySpec) -> SplunkQuerySpec:
        coarse_filters = {
            key: value
            for key, value in spec.field_filters.items()
            if key in COARSE_FIELD_FILTER_ALLOWLIST
        }
        coarse_keywords = spec.keywords[:2]
        return SplunkQuerySpec(
            dataset=spec.dataset,
            index=spec.index,
            sourcetype=spec.sourcetype,
            earliest=spec.earliest,
            latest=spec.latest,
            keywords=coarse_keywords,
            ip=spec.ip,
            host=spec.host,
            source=spec.source,
            field_filters=coarse_filters,
            limit=min(spec.limit, COARSE_QUERY_LIMIT),
            fields=spec.fields,
        )

    def build_query_from_intent(
        self,
        intent: str,
        *,
        dataset: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> tuple[SplunkQuerySpec, str]:
        spec = self.interpret_intent(intent, dataset=dataset, additional_context=additional_context)
        query = self.build_query(spec, dataset=dataset or spec.dataset)
        return spec, query

    def get_dataset_context(self, dataset: str | None = None) -> dict[str, Any]:
        dataset_cfg = self.config.resolve_dataset(dataset)
        catalog_entry = get_dataset_catalog_entry(dataset_cfg.name)
        sourcetypes: list[dict[str, Any]] = []
        if catalog_entry is not None:
            for entry in catalog_entry.sourcetypes:
                discovered_fields = self._get_discovered_fields(dataset_cfg.name, entry.sourcetype)
                sourcetypes.append(
                    {
                        "sourcetype": entry.sourcetype,
                        "description": entry.description,
                        "representative_fields": list(entry.representative_fields),
                        "discovered_fields": list(discovered_fields),
                    }
                )
        return {
            "dataset": dataset_cfg.name,
            "index": dataset_cfg.index,
            "description": catalog_entry.description if catalog_entry is not None else "",
            "sourcetypes": sourcetypes,
        }

    def _resolve_search_spec(
        self,
        *,
        spec: SplunkQuerySpec | dict[str, Any] | None,
        intent: str | None,
        dataset: str | None,
        additional_context: dict[str, Any] | None,
    ) -> SplunkQuerySpec:
        if spec is None and not intent:
            raise SplunkToolError("query_build_error", "Either a query spec or intent is required")
        if intent:
            return self.interpret_intent(intent, dataset=dataset, additional_context=additional_context)
        if isinstance(spec, SplunkQuerySpec):
            return spec
        return SplunkQuerySpec.from_dict(spec or {})

    def _get_discovered_fields(self, dataset: str, sourcetype: str) -> tuple[str, ...]:
        cache_key = (dataset, sourcetype)
        if cache_key in self._field_context_cache:
            return self._field_context_cache[cache_key]
        try:
            dataset_cfg = self.config.resolve_dataset(dataset)
            fields = self._discover_fields_for_sourcetype(dataset_cfg, sourcetype)
        except SplunkToolError:
            fields = ()
        self._field_context_cache[cache_key] = fields
        return fields

    def _discover_fields_for_sourcetype(
        self,
        dataset_cfg: SplunkDatasetConfig,
        sourcetype: str,
    ) -> tuple[str, ...]:
        if not self.config.password:
            return ()
        query = (
            f'search index={_quote_if_needed(dataset_cfg.index)} sourcetype="{_escape_quotes(sourcetype)}" '
            "| head 200 | fields * | fieldsummary"
        )
        sid = self._create_search_job(dataset_cfg, query)
        self._wait_for_job(dataset_cfg, sid)
        results = self._fetch_job_results(dataset_cfg, sid)
        fields = []
        for item in results:
            field_name = str(item.get("field") or "").strip()
            if field_name and field_name not in fields:
                fields.append(field_name)
        return tuple(fields[:40])

    def _execute_search(self, dataset_cfg: SplunkDatasetConfig, query: str) -> list[dict[str, Any]]:
        if not self.config.password:
            raise SplunkToolError("configuration_error", "Missing SPLUNK_PASSWORD")
        sid = self._create_search_job(dataset_cfg, query)
        self._wait_for_job(dataset_cfg, sid)
        return self._fetch_job_results(dataset_cfg, sid)

    def _create_search_job(self, dataset_cfg: SplunkDatasetConfig, query: str) -> str:
        payload = {
            "search": query,
            "output_mode": "json",
            "exec_mode": "normal",
        }
        response = self._request(
            dataset_cfg,
            path="/services/search/jobs",
            method="POST",
            data=parse.urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        sid = str(response.get("sid") or "").strip()
        if not sid:
            raise SplunkToolError("query_execution_error", "Splunk response missing search job sid")
        return sid

    def _wait_for_job(self, dataset_cfg: SplunkDatasetConfig, sid: str) -> None:
        deadline = time.time() + self.config.poll_timeout
        while time.time() < deadline:
            response = self._request(
                dataset_cfg,
                path=f"/services/search/jobs/{parse.quote(sid)}",
                query={"output_mode": "json"},
            )
            entry = next(iter(response.get("entry") or []), {})
            content = entry.get("content") or {}
            if content.get("isDone"):
                return
            time.sleep(self.config.poll_interval)
        raise SplunkToolError(
            "connection_error",
            f"Timed out waiting for Splunk search job {sid}",
        )

    def _fetch_job_results(self, dataset_cfg: SplunkDatasetConfig, sid: str) -> list[dict[str, Any]]:
        response = self._request(
            dataset_cfg,
            path=f"/services/search/jobs/{parse.quote(sid)}/results",
            query={"output_mode": "json"},
        )
        results = response.get("results")
        if not isinstance(results, list):
            return []
        return [dict(item) for item in results if isinstance(item, dict)]

    def _request(
        self,
        dataset_cfg: SplunkDatasetConfig,
        *,
        path: str,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        candidate_paths = _candidate_api_paths(path)
        last_http_error: SplunkToolError | None = None
        last_connection_error: SplunkToolError | None = None

        for candidate_path in candidate_paths:
            url = f"{dataset_cfg.base_url}{candidate_path}"
            if query:
                url = f"{url}?{parse.urlencode(query)}"

            request_headers = {
                "Authorization": f"Basic {self._build_basic_auth_header()}",
                "Accept": "application/json",
            }
            if headers:
                request_headers.update(headers)
            req = request.Request(url=url, data=data, method=method, headers=request_headers)
            context = None
            if not self.config.verify_tls and url.startswith("https://"):
                context = ssl._create_unverified_context()

            try:
                with request.urlopen(req, timeout=self.config.request_timeout, context=context) as response:
                    body = response.read().decode("utf-8")
            except error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                last_http_error = SplunkToolError(
                    "query_execution_error",
                    f"Splunk API returned HTTP {exc.code} for {candidate_path}: {error_body[:300]}",
                )
                if exc.code == 404 and candidate_path != candidate_paths[-1]:
                    continue
                raise last_http_error from exc
            except error.URLError as exc:
                last_connection_error = SplunkToolError(
                    "connection_error",
                    f"Failed to connect to Splunk at {dataset_cfg.base_url}: {exc.reason}",
                )
                raise last_connection_error from exc

            try:
                parsed = json.loads(body or "{}")
            except json.JSONDecodeError as exc:
                raise SplunkToolError(
                    "query_execution_error",
                    f"Splunk API returned non-JSON content from {candidate_path}: {body[:300]}",
                ) from exc
            if not isinstance(parsed, dict):
                raise SplunkToolError(
                    "query_execution_error",
                    "Splunk API returned an unexpected response shape",
                )
            return parsed

        if last_http_error is not None:
            raise last_http_error
        if last_connection_error is not None:
            raise last_connection_error
        raise SplunkToolError("query_execution_error", "Splunk request failed without a response")

    def _build_basic_auth_header(self) -> str:
        token = f"{self.config.username}:{self.config.password}".encode("utf-8")
        return base64.b64encode(token).decode("ascii")

    def _format_sample_events(
        self,
        raw_results: list[dict[str, Any]],
        requested_fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        keep_fields = list(dict.fromkeys([*COMMON_EVENT_FIELDS, *requested_fields]))
        sample_events: list[dict[str, Any]] = []
        for item in raw_results[:MAX_SAMPLE_EVENTS]:
            normalized: dict[str, Any] = {}
            for field_name in keep_fields:
                if field_name not in item:
                    continue
                normalized[field_name] = _truncate_value(item[field_name])
            if not normalized:
                normalized = {key: _truncate_value(value) for key, value in item.items()}
            sample_events.append(normalized)
        return sample_events

    def _build_summary(
        self,
        dataset_cfg: SplunkDatasetConfig,
        spec: SplunkQuerySpec,
        raw_results: list[dict[str, Any]],
        sample_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        field_coverage = sorted(
            {
                field_name
                for event in sample_events
                for field_name in event.keys()
            }
        )
        return {
            "time_range": {
                "earliest": spec.earliest or "",
                "latest": spec.latest or "",
            },
            "result_count": len(raw_results),
            "dataset": dataset_cfg.name,
            "index": spec.index or dataset_cfg.index,
            "sourcetype": spec.sourcetype or "",
            "sample_event_count": len(sample_events),
            "field_coverage": field_coverage,
        }


def call_llm(*args: Any, **kwargs: Any) -> str:
    from src.agent.llm import call_llm as _call_llm

    return _call_llm(*args, **kwargs)


def parse_yaml_response(response_text: str) -> dict[str, Any] | None:
    from src.agent.llm import parse_yaml_response as _parse_yaml_response

    return _parse_yaml_response(response_text)


def get_dataset_catalog_entry(dataset: str) -> DatasetCatalogEntry | None:
    return DATASET_CATALOG.get(dataset.strip().lower())


def get_default_splunk_tool() -> SplunkSearchTool:
    return SplunkSearchTool()


@mcp.tool()
def log_search(intent: str) -> dict[str, Any]:
    """
    Translate a TTT log-investigation intent into SPL and query Splunk for matching events.

    Use this tool when the task asks to search logs, authentication records, DNS, HTTP,
    Sysmon, Suricata, or other Splunk-accessible telemetry in the configured BOTS datasets.
    This tool is read-only, but it requires a reachable Splunk instance and indexed data.
    Zero-result queries are possible if the intent is too narrow or the dataset does not
    contain matching events.

    Args:
        intent: Natural-language investigation request, optionally including event and
            TTT context rendered into a single prompt string by the caller.

    Returns:
        A structured result containing success, query details, summary, sample events,
        and any execution error message.
    """
    service = get_default_splunk_tool()
    dataset = service.config.default_dataset
    try:
        query_spec = service.interpret_intent(
            intent,
            dataset=dataset,
            additional_context={"rendered_intent": intent},
        )
        search_result = service.search(
            spec=query_spec,
            additional_context={"rendered_intent": intent},
        )
        tool_input = {
            "intent": intent,
            "query_spec": search_result.get("query_spec", query_spec.to_dict()),
            "query": search_result.get("query", ""),
            "translation_strategy": "llm_intent_translation",
            "translation_error": "",
        }
        return {
            "success": bool(search_result.get("success")),
            "result": search_result,
            "error_message": str(search_result.get("error_message") or ""),
            "tool_input": tool_input,
        }
    except Exception as exc:
        return {
            "success": False,
            "result": {
                "success": False,
                "dataset": dataset,
                "query": "",
                "job_mode": "search_job_polling",
                "result_count": 0,
                "summary": {},
                "sample_events": [],
                "warnings": [],
                "error_message": f"intent_parse_error:{exc}",
                "query_spec": {},
            },
            "error_message": f"intent_parse_error:{exc}",
            "tool_input": {
                "intent": intent,
                "query_spec": {},
                "query": "",
                "translation_strategy": "llm_intent_translation",
                "translation_error": str(exc),
            },
        }


def _build_spl_query_result(
    *,
    intent: str = "",
    event: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service = get_default_splunk_tool()
    normalized_spec = SplunkQuerySpec.from_dict(spec or {})
    query = service.build_query(normalized_spec)
    return {
        "success": True,
        "result": {"query": query, "query_spec": normalized_spec.to_dict()},
        "error_message": "",
        "tool_input": {"intent": intent},
    }


def _list_splunk_datasets_result(
    *,
    intent: str = "",
    event: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service = get_default_splunk_tool()
    datasets = [
        service.get_dataset_context(dataset_name)
        for dataset_name in sorted(service.config.datasets.keys())
    ]
    return {
        "success": True,
        "result": {"datasets": datasets},
        "error_message": "",
        "tool_input": {"intent": intent},
    }


def _candidate_api_paths(path: str) -> tuple[str, ...]:
    normalized = path if path.startswith("/") else f"/{path}"
    if not normalized.startswith("/services/"):
        return (normalized,)
    return (
        normalized,
        f"/en-US/splunkd/__raw{normalized}",
        f"/splunkd/__raw{normalized}",
    )


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_string_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    return tuple(item.strip() for item in (str(entry) for entry in items) if item.strip())


def _normalize_field_filters(value: Any) -> dict[str, str | tuple[str, ...]]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("field_filters must be a mapping")
    normalized: dict[str, str | tuple[str, ...]] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
            raise ValueError(f"Invalid field filter name: {raw_key}")
        if isinstance(raw_value, (list, tuple, set)):
            values = tuple(item.strip() for item in (str(entry) for entry in raw_value) if item.strip())
            if not values:
                continue
            normalized[key] = values
            continue
        text = str(raw_value).strip()
        if text:
            normalized[key] = text
    return normalized


def _normalize_limit(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_LIMIT
    limit = int(value)
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _escape_quotes(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quote_if_needed(value: str) -> str:
    text = value.strip()
    if not text:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_.:/-]+", text):
        return text
    return f'"{_escape_quotes(text)}"'


def _truncate_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_FIELD_LENGTH:
            return value
        return f"{value[:MAX_FIELD_LENGTH]}...(truncated)"
    if isinstance(value, list):
        return [_truncate_value(item) for item in value[:10]]
    return value


def _normalize_time_term(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    if text.lower() == "now":
        return text
    if re.fullmatch(r"-?\d+[smhdwmonqy](@[smhdw])?", text):
        return text
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return text

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp()))


def _fill_partial_time_terms(
    spec: dict[str, Any],
    *,
    intent: str,
    additional_context: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(spec)
    date_hint = _extract_date_hint(
        "\n".join(
            [
                intent,
                json.dumps(additional_context, ensure_ascii=False, indent=2),
            ]
        )
    )
    if not date_hint:
        return normalized
    for field_name in ("earliest", "latest"):
        value = normalized.get(field_name)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
            normalized[field_name] = f"{date_hint}T{text}"
    return normalized


def _extract_date_hint(text: str) -> str | None:
    match = re.search(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b", text)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def _canonicalize_query_spec(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(spec)
    sourcetype = str(normalized.get("sourcetype") or "").strip().lower()
    field_filters = dict(normalized.get("field_filters") or {})
    keywords = list(_normalize_string_sequence(normalized.get("keywords")))

    rewritten_filters: dict[str, str | tuple[str, ...]] = {}
    for raw_key, raw_value in field_filters.items():
        key = str(raw_key).strip()
        value = raw_value
        if isinstance(value, (list, tuple, set)):
            values = tuple(str(item).strip() for item in value if str(item).strip())
            if not values:
                continue
            rewritten_filters[key] = values
            continue

        text = str(value).strip()
        if not text:
            continue

        if sourcetype == "stream:http":
            if key == "src" and _looks_like_ip(text):
                rewritten_filters["src_ip"] = text
                continue
            if key == "dest" and _looks_like_ip(text):
                rewritten_filters["dest_ip"] = text
                continue
            if key == "dest" and _looks_like_domain(text):
                if text not in keywords:
                    keywords.append(text)
                continue

        rewritten_filters[key] = text

    normalized["field_filters"] = rewritten_filters
    normalized["keywords"] = keywords
    return normalized


def _looks_like_ip(value: str) -> bool:
    return bool(re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value))


def _looks_like_domain(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)) and not _looks_like_ip(value)


__all__ = [
    "DATASET_CATALOG",
    "DatasetCatalogEntry",
    "SplunkQuerySpec",
    "SplunkSearchTool",
    "get_default_splunk_tool",
    "mcp",
    "SourcetypeCatalogEntry",
    "SplunkToolConfig",
]
