from __future__ import annotations

from dataclasses import dataclass, field
import base64
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
    SourcetypeCatalogEntry("WinEventLog:Application", "Windows 应用日志", ("host", "source", "EventCode", "Message", "ComputerName")),
    SourcetypeCatalogEntry("WinEventLog:Security", "Windows 安全/认证日志，适合登录、账户、权限调查", ("host", "source", "EventCode", "Logon_Type", "Account_Name", "user", "src", "src_ip")),
    SourcetypeCatalogEntry("WinEventLog:System", "Windows 系统日志", ("host", "source", "EventCode", "Message")),
    SourcetypeCatalogEntry("XmlWinEventLog:Microsoft-Windows-Sysmon/Operational", "Sysmon 进程、网络、文件与注册表行为日志", ("host", "EventCode", "Image", "CommandLine", "ParentImage", "User", "SourceIp", "DestinationIp")),
    SourcetypeCatalogEntry("fgt_event", "FortiGate 事件日志", ("srcip", "dstip", "user", "action", "msg", "devname")),
    SourcetypeCatalogEntry("fgt_traffic", "FortiGate 流量日志", ("srcip", "dstip", "srcport", "dstport", "proto", "action")),
    SourcetypeCatalogEntry("fgt_utm", "FortiGate UTM/威胁检测日志", ("srcip", "dstip", "service", "attack", "severity", "action")),
    SourcetypeCatalogEntry("iis", "IIS Web 访问日志", ("c_ip", "cs_method", "cs_uri_stem", "cs_uri_query", "sc_status", "cs_username")),
    SourcetypeCatalogEntry("nessus:scan", "Nessus 漏洞扫描结果", ("host", "pluginName", "severity", "port", "protocol")),
    SourcetypeCatalogEntry("stream:dhcp", "DHCP 网络流日志", ("src", "dest", "mac_addr", "dhcp_message_type")),
    SourcetypeCatalogEntry("stream:dns", "DNS 查询日志", ("src", "dest", "query", "query_type", "answer", "rcode")),
    SourcetypeCatalogEntry("stream:http", "HTTP 请求日志", ("src", "dest", "method", "uri", "status", "user_agent", "http_content_type")),
    SourcetypeCatalogEntry("stream:icmp", "ICMP 流量日志", ("src", "dest", "icmp_type", "icmp_code")),
    SourcetypeCatalogEntry("stream:ip", "通用 IP 流量日志", ("src", "dest", "proto", "bytes_in", "bytes_out")),
    SourcetypeCatalogEntry("stream:ldap", "LDAP 目录访问日志", ("src", "dest", "binddn", "operation", "result")),
    SourcetypeCatalogEntry("stream:mapi", "MAPI/邮件客户端协议日志", ("src", "dest", "user", "subject")),
    SourcetypeCatalogEntry("stream:sip", "SIP 通信日志", ("src", "dest", "method", "call_id")),
    SourcetypeCatalogEntry("stream:smb", "SMB 文件共享日志", ("src", "dest", "file_name", "share_name", "action")),
    SourcetypeCatalogEntry("stream:snmp", "SNMP 访问日志", ("src", "dest", "community", "oid")),
    SourcetypeCatalogEntry("stream:tcp", "TCP 会话日志", ("src", "dest", "src_port", "dest_port", "tcp_flags")),
    SourcetypeCatalogEntry("suricata", "Suricata IDS 告警日志", ("src_ip", "dest_ip", "src_port", "dest_port", "signature", "category", "severity")),
    SourcetypeCatalogEntry("winregistry", "Windows 注册表活动日志", ("host", "registry_path", "registry_value_name", "action", "user")),
)


DATASET_CATALOG: dict[str, DatasetCatalogEntry] = {
    "botsv1": DatasetCatalogEntry(
        dataset="botsv1",
        description="Boss of the SOC v1 攻击数据集，包含 Windows、Sysmon、FortiGate、IIS、Stream、Suricata 等日志。",
        sourcetypes=BOTSV1_SOURCETYPES,
    ),
    "botsv2": DatasetCatalogEntry(
        dataset="botsv2",
        description="Boss of the SOC v2 攻击数据集，包含 Windows、Sysmon、Apache、IIS、PAN、Suricata、Stream 等日志。",
        sourcetypes=BOTSV1_SOURCETYPES,
    ),
    "botsv3": DatasetCatalogEntry(
        dataset="botsv3",
        description="Boss of the SOC v3 数据集，覆盖云、Windows、Sysmon、O365、AWS、Suricata、Stream 等日志。",
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
                "请把以下 TTT 调查意图转换为可执行的 Splunk 查询规格，只输出 YAML。",
                "只允许这些字段：dataset, index, sourcetype, earliest, latest, keywords, ip, host, source, field_filters, limit, fields。",
                "field_filters 的 value 只能是字符串或字符串数组。",
                "不要输出 SPL，不要输出额外解释。",
                "你必须结合下面给出的数据集、sourcetype 和字段背景信息来选择最合理的日志范围。",
                f"dataset 默认值: {dataset_name}",
                f"dataset_context: {json.dumps(dataset_context, ensure_ascii=False, indent=2)}",
                f"intent: {intent}",
                f"context: {context_text}",
            ]
        )
        response_text = call_llm(
            """
你是一个 Splunk SPL 规划器。
你的任务是把 TTT 的自然语言调查意图转换为结构化查询规格，并让该规格能够进一步生成可执行 SPL。
严禁输出 SPL、解释文字或未列出的字段。
你会收到完整的数据集背景，包括可用 sourcetype 和代表字段。
请优先选择最相关的 sourcetype，并尽量为 fields 补充最有调查价值的字段。
如果意图里没有给出字段，可以留空，但不要编造不存在的 sourcetype。
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
        if normalized.earliest:
            clauses.append(f'earliest="{_escape_quotes(normalized.earliest)}"')
        if normalized.latest:
            clauses.append(f'latest="{_escape_quotes(normalized.latest)}"')
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
            query = self.build_query(normalized_spec, dataset=dataset_cfg.name)
            raw_results = self._execute_search(dataset_cfg, query)
            sample_events = self._format_sample_events(raw_results, normalized_spec.fields)
            warnings: list[str] = []
            if not raw_results:
                warnings.append("No events matched the query")
            return {
                "success": True,
                "dataset": dataset_cfg.name,
                "query": query,
                "job_mode": "search_job_polling",
                "result_count": len(raw_results),
                "summary": self._build_summary(dataset_cfg, normalized_spec, raw_results, sample_events),
                "sample_events": sample_events,
                "warnings": warnings,
                "error_message": "",
                "query_spec": normalized_spec.to_dict(),
            }
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
        if search_result.get("success") and int(search_result.get("result_count") or 0) == 0:
            return {
                "success": False,
                "result": search_result,
                "error_message": "zero_results:No events matched the query",
                "tool_input": tool_input,
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
