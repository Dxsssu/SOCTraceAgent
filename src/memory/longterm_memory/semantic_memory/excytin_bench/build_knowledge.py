from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from zipfile import ZipFile


PACKAGE_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_SOURCE = REPOSITORY_ROOT / "data/excytin-bench/huggingface/data.zip"
DEFAULT_OUTPUT = PACKAGE_DIR / "semantic_memory.json"

ENVIRONMENT_ID = "excytin-azure-mysql"
CATALOG_ID = "excytin-schema-v1"


TABLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "Alert": (
        "记录传统监控告警、告警规则、阈值、状态和关联资源。",
        "Legacy monitoring alerts, alert rules, thresholds, status, and related resources.",
    ),
    "AlertEvidence": (
        "记录告警关联的账号、设备、网络、文件、邮件、进程和注册表证据。",
        "Structured account, device, network, file, email, process, and registry evidence linked to alerts.",
    ),
    "AlertInfo": (
        "记录告警标识、标题、类别、严重性、检测来源和 ATT&CK 技术。",
        "Alert identifiers, titles, categories, severity, detection sources, and ATT&CK techniques.",
    ),
    "SecurityAlert": (
        "记录 Microsoft Sentinel 安全告警、受影响实体、分配人员、自动调查标识与状态，以及处置元数据；动态详情位于 ExtendedProperties。",
        "Microsoft Sentinel security alerts with affected entities, assigned analysts, automated investigation identifiers and state, and remediation metadata in ExtendedProperties.",
    ),
    "SecurityIncident": (
        "记录安全事件的汇总、生命周期、关联告警、负责人、分类和时间范围。",
        "Security incident summaries, lifecycle, related alerts, owners, classifications, and time ranges.",
    ),
    "SigninLogs": (
        "记录用户交互式登录、来源 IP、应用、认证方式、条件访问和风险状态。",
        "Interactive user sign-ins, source IPs, applications, authentication, conditional access, and risk.",
    ),
    "AADNonInteractiveUserSignInLogs": (
        "记录非交互式用户登录、令牌访问、应用、来源 IP 和认证结果。",
        "Non-interactive user sign-ins, token access, applications, source IPs, and authentication results.",
    ),
    "DeviceProcessEvents": (
        "记录终端进程创建、命令行、父进程、账户、设备和文件哈希。",
        "Endpoint process creation, command lines, parent processes, accounts, devices, and file hashes.",
    ),
    "DeviceNetworkEvents": (
        "记录终端网络连接及其设备、进程、本地和远程地址与端口。",
        "Endpoint network connections with devices, processes, local and remote addresses, and ports.",
    ),
    "DeviceFileEvents": (
        "记录终端文件创建、修改、删除及其进程、账户、设备和文件哈希。",
        "Endpoint file creation, modification, and deletion with process, account, device, and hash data.",
    ),
    "DeviceRegistryEvents": (
        "记录终端注册表操作及其键值、发起进程、账户和设备。",
        "Endpoint registry operations with keys, values, initiating processes, accounts, and devices.",
    ),
    "DeviceLogonEvents": (
        "记录终端登录活动、账户、设备、远程地址和发起进程。",
        "Endpoint logon activity with accounts, devices, remote addresses, and initiating processes.",
    ),
    "DeviceInfo": (
        "记录终端设备身份、操作系统、网络信息、健康状态和用户信息。",
        "Endpoint device identity, operating system, network information, health, and user data.",
    ),
    "EmailEvents": (
        "记录邮件发送、接收、投递、威胁检测、处置和消息标识。",
        "Email delivery, senders, recipients, threat detection, remediation, and message identifiers.",
    ),
    "EmailUrlInfo": (
        "记录邮件中提取的 URL 及其网络消息标识。",
        "URLs extracted from email messages and their network message identifiers.",
    ),
    "EmailAttachmentInfo": (
        "记录邮件附件、文件名、文件类型、哈希和网络消息标识。",
        "Email attachments, file names, types, hashes, and network message identifiers.",
    ),
    "EmailPostDeliveryEvents": (
        "记录邮件投递后的隔离、移动、修复等安全操作。",
        "Post-delivery email security actions such as quarantine, movement, and remediation.",
    ),
    "UrlClickEvents": (
        "记录用户点击 URL 的时间、账户、设备、IP 和关联邮件。",
        "User URL clicks with time, account, device, IP, and related email identifiers.",
    ),
    "OfficeActivity": (
        "记录 Microsoft 365 和 Office 工作负载中的用户、邮件箱、文件及管理操作。",
        "User, mailbox, file, and administrative activity across Microsoft 365 and Office workloads.",
    ),
    "CloudAppEvents": (
        "记录云应用活动、账户、对象、IP、设备和操作结果。",
        "Cloud application activity with accounts, objects, IPs, devices, and operation results.",
    ),
    "AuditLogs": (
        "记录 Microsoft Entra ID 的目录审计和管理操作。",
        "Microsoft Entra ID directory audit and administrative operations.",
    ),
}


TABLE_RETRIEVAL_HINTS: dict[str, tuple[str, ...]] = {
    "SecurityAlert": (
        "automated investigation",
        "manual investigation",
        "manually initiated",
        "manually started",
        "assigned analyst",
        "compromised entity",
        "remediation status",
    ),
}


FIELD_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "TenantId": ("日志所属租户标识。", "Identifier of the tenant that owns the log record."),
    "TimeGenerated": ("日志记录生成时间。", "Time when the log record was generated."),
    "Timestamp": ("事件发生或被记录的时间。", "Time when the event occurred or was recorded."),
    "AlertId": ("告警标识，可用于连接告警信息与告警证据。", "Alert identifier used to connect alert information and evidence."),
    "AlertName": ("告警或检测规则的名称。", "Name of the alert or detection."),
    "CompromisedEntity": (
        "告警的主要受影响实体；设备告警中通常保存主机名或完全限定域名。",
        "Primary affected entity of an alert; device alerts commonly store a hostname or fully qualified domain name.",
    ),
    "ExtendedProperties": (
        "告警扩展 JSON 元数据，可包含分配人员、自动调查标识、调查状态和其他处置信息。",
        "Extended alert JSON metadata that can contain assignment, automated-investigation identifiers, investigation state, and remediation details.",
    ),
    "SystemAlertId": ("安全平台生成的系统告警标识。", "System alert identifier generated by the security platform."),
    "IncidentName": ("安全事件名称或标识。", "Security incident name or identifier."),
    "IncidentNumber": ("安全事件编号。", "Numeric security incident identifier."),
    "DeviceId": ("终端设备标识。", "Endpoint device identifier."),
    "DeviceName": ("终端设备名称。", "Endpoint device name."),
    "AccountSid": ("Windows 账户安全标识符。", "Windows account security identifier."),
    "AccountObjectId": ("目录中的账户对象标识。", "Account object identifier in the directory."),
    "AccountUpn": ("账户用户主体名称。", "Account user principal name."),
    "UserPrincipalName": ("用户主体名称，通常采用邮件地址形式。", "User principal name, commonly formatted as an email address."),
    "IPAddress": ("事件关联的 IP 地址。", "IP address associated with the event."),
    "RemoteIP": ("远程端 IP 地址。", "Remote endpoint IP address."),
    "LocalIP": ("本地端 IP 地址。", "Local endpoint IP address."),
    "NetworkMessageId": ("Microsoft 365 邮件网络消息标识。", "Microsoft 365 network message identifier."),
    "InternetMessageId": ("邮件 Internet Message-ID。", "Email Internet Message-ID."),
    "ProcessId": ("进程标识符；关联时通常还需设备和时间范围。", "Process identifier, normally scoped by device and time."),
    "InitiatingProcessId": ("发起当前事件的进程标识符。", "Identifier of the process that initiated the event."),
    "ProcessCommandLine": ("被执行进程的完整命令行。", "Full command line of the executed process."),
    "InitiatingProcessCommandLine": ("发起进程的完整命令行。", "Full command line of the initiating process."),
    "SHA1": ("文件的 SHA-1 哈希。", "SHA-1 hash of a file."),
    "SHA256": ("文件的 SHA-256 哈希。", "SHA-256 hash of a file."),
    "SourceSystem": ("生成或承载日志的源系统。", "Source system that produced or carries the log."),
    "Type": ("日志记录的来源表或记录类型。", "Source table or record type of the log entry."),
}


SEMANTIC_LABELS_ZH = {
    "TIMESTAMP": "时间",
    "TENANT_ID": "租户标识",
    "ALERT_ID": "告警标识",
    "INCIDENT_ID": "事件标识",
    "ACCOUNT_ID": "账户标识",
    "ACCOUNT_NAME": "账户名称",
    "EMAIL_ADDRESS": "邮件地址",
    "DEVICE_ID": "设备标识",
    "DEVICE_NAME": "设备名称",
    "IP_ADDRESS": "IP 地址",
    "URL": "URL",
    "DOMAIN": "域名或域",
    "FILE_NAME": "文件名",
    "FILE_PATH": "文件路径",
    "FILE_HASH": "文件哈希",
    "PROCESS_ID": "进程标识",
    "COMMAND_LINE": "命令行",
    "MESSAGE_ID": "消息标识",
    "APPLICATION_ID": "应用标识",
    "ACTION": "操作类型",
    "STATUS": "状态",
    "SEVERITY": "严重性",
    "MITRE_TECHNIQUE": "MITRE ATT&CK 战术或技术",
    "PORT": "网络端口",
    "PROTOCOL": "网络协议",
    "RESOURCE_ID": "资源标识",
    "CORRELATION_ID": "关联标识",
    "JSON": "动态 JSON 数据",
    "TEXT": "文本信息",
}


JOIN_KEY_CONFIG: dict[str, dict[str, Any]] = {
    "tenant_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "global"},
    "alert_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "global"},
    "system_alert_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "global"},
    "device_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "aad_device_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "device_name": {"confidence": 0.95, "match_method": "normalized", "join_scope": "tenant"},
    "account_sid": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "account_object_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "account_upn": {"confidence": 0.95, "match_method": "case_insensitive", "join_scope": "tenant"},
    "email_address": {"confidence": 0.95, "match_method": "case_insensitive", "join_scope": "tenant"},
    "ip_address": {"confidence": 0.9, "match_method": "normalized", "join_scope": "time_window"},
    "network_message_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "internet_message_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "sha1": {"confidence": 1.0, "match_method": "lowercase", "join_scope": "global"},
    "sha256": {"confidence": 1.0, "match_method": "lowercase", "join_scope": "global"},
    "process_id": {"confidence": 0.75, "match_method": "exact", "join_scope": "device_and_time"},
    "correlation_id": {"confidence": 1.0, "match_method": "exact", "join_scope": "tenant"},
    "resource_id": {"confidence": 0.9, "match_method": "case_insensitive", "join_scope": "tenant"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def logical_table_name(path: PurePosixPath) -> str:
    return re.sub(r"_\d+$", "", path.stem)


def camel_words(value: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", value)]


def classify_table(name: str) -> str:
    if name in {"Alert", "AlertEvidence", "AlertInfo", "SecurityAlert", "SecurityIncident"}:
        return "incident_alert"
    if name.startswith("AAD") or "Signin" in name or name.startswith("Identity") or name in {"Anomalies"}:
        return "identity_signin"
    if "Process" in name or "ImageLoad" in name or name == "DeviceEvents":
        return "endpoint_process"
    if "File" in name or "Registry" in name:
        return "endpoint_file"
    if name in {"DeviceInfo", "DeviceLogonEvents", "DeviceNetworkEvents", "DeviceNetworkInfo"}:
        return "endpoint_network"
    if name.startswith("Email") or name in {"UrlClickEvents", "OfficeActivity"}:
        return "email"
    if name.startswith("AZFW") or name in {"MicrosoftAzureBastionAuditLogs", "ThreatIntelligenceIndicator"}:
        return "firewall_network"
    if name.startswith("Intune") or name in {"AuditLogs", "CloudAppEvents", "MicrosoftGraphActivityLogs"}:
        return "cloud_application"
    return "platform_operation"


def table_layer(log_type: str) -> str:
    if log_type == "incident_alert":
        return "alert"
    if log_type == "platform_operation":
        return "operational"
    return "raw"


def source_product(name: str, log_type: str) -> str:
    if name.startswith("Device"):
        return "Microsoft Defender for Endpoint"
    if name.startswith("Email") or name == "UrlClickEvents":
        return "Microsoft Defender for Office 365"
    if name.startswith("AAD") or name == "SigninLogs":
        return "Microsoft Entra ID"
    if name.startswith("Identity"):
        return "Microsoft Defender for Identity"
    if name.startswith("AZFW"):
        return "Azure Firewall"
    if name.startswith("Intune"):
        return "Microsoft Intune"
    if name in {"AlertEvidence", "AlertInfo"}:
        return "Microsoft Defender XDR"
    if name in {"SecurityAlert", "SecurityIncident", "SentinelAudit", "SentinelHealth"}:
        return "Microsoft Sentinel"
    if name == "CloudAppEvents":
        return "Microsoft Defender for Cloud Apps"
    if name == "OfficeActivity":
        return "Microsoft 365"
    if log_type == "firewall_network":
        return "Microsoft Azure"
    return "Azure Monitor"


def describe_table(name: str, log_type: str) -> tuple[str, str]:
    if name in TABLE_DESCRIPTIONS:
        return TABLE_DESCRIPTIONS[name]
    descriptions = {
        "incident_alert": ("安全事件与告警信息", "security incident and alert information"),
        "identity_signin": ("身份、目录、登录和风险活动", "identity, directory, sign-in, and risk activity"),
        "endpoint_process": ("终端进程、镜像加载和通用设备活动", "endpoint process, image-load, and general device activity"),
        "endpoint_file": ("终端文件、证书和注册表活动", "endpoint file, certificate, and registry activity"),
        "endpoint_network": ("终端设备、登录和网络活动", "endpoint device, logon, and network activity"),
        "email": ("邮件、URL、附件和协作活动", "email, URL, attachment, and collaboration activity"),
        "cloud_application": ("云应用、目录、Graph 和设备管理活动", "cloud application, directory, Graph, and device-management activity"),
        "firewall_network": ("Azure 网络、防火墙、DNS 和威胁情报活动", "Azure network, firewall, DNS, and threat-intelligence activity"),
        "platform_operation": ("Azure 平台运行、健康、查询和诊断信息", "Azure platform operation, health, query, and diagnostic information"),
    }
    zh, en = descriptions[log_type]
    return f"{name} 表，记录{zh}。", f"{name} table containing {en}."


def infer_semantic_type(name: str, source_type: str) -> str:
    lower = name.lower()
    if name in {"TimeGenerated", "Timestamp", "CreatedDateTime"} or re.search(
        r"(time|timestamp|datetime|date)$", lower
    ):
        return "TIMESTAMP"
    if name in {"TenantId", "AADTenantId", "OfficeTenantId", "ResourceTenantId", "HomeTenantId"}:
        return "TENANT_ID"
    if name in {"AlertId", "SystemAlertId", "VendorOriginalId"}:
        return "ALERT_ID"
    if "incident" in lower and ("id" in lower or "name" in lower or "number" in lower):
        return "INCIDENT_ID"
    if name in {"NetworkMessageId", "InternetMessageId", "MessageId"}:
        return "MESSAGE_ID"
    if "commandline" in lower or lower == "command":
        return "COMMAND_LINE"
    if re.search(r"(^|initiating)processid$", lower):
        return "PROCESS_ID"
    if lower in {"sha1", "sha256", "md5"} or lower.endswith(("sha1", "sha256", "md5")):
        return "FILE_HASH"
    if "filepath" in lower or "folderpath" in lower or lower.endswith("path"):
        return "FILE_PATH"
    if "filename" in lower or lower.endswith("file"):
        return "FILE_NAME"
    if "deviceid" in lower or lower in {"machineid", "sourcecomputerid"}:
        return "DEVICE_ID"
    if "devicename" in lower or lower in {"hostname", "computer", "clientmachinename"}:
        return "DEVICE_NAME"
    if any(token in lower for token in ("ipaddress", "remoteip", "localip", "senderipv", "clientip", "sourceip", "destinationip")):
        return "IP_ADDRESS"
    if "url" in lower or "uri" in lower or lower.endswith("link"):
        return "URL"
    if "emailaddress" in lower or "mailfromaddress" in lower or lower.endswith("smtp"):
        return "EMAIL_ADDRESS"
    if "upn" in lower or name == "UserPrincipalName":
        return "EMAIL_ADDRESS"
    if "account" in lower or lower in {"userid", "identity", "userkey", "userdisplayname"}:
        if any(token in lower for token in ("id", "sid", "object", "key")):
            return "ACCOUNT_ID"
        return "ACCOUNT_NAME"
    if "domain" in lower:
        return "DOMAIN"
    if lower.endswith("port") or "portnumber" in lower:
        return "PORT"
    if "protocol" in lower:
        return "PROTOCOL"
    if "applicationid" in lower or lower in {"appid", "oauthapplicationid", "serviceprincipalid"}:
        return "APPLICATION_ID"
    if "resourceid" in lower:
        return "RESOURCE_ID"
    if "correlationid" in lower or lower == "correlationkey":
        return "CORRELATION_ID"
    if "technique" in lower or "tactic" in lower:
        return "MITRE_TECHNIQUE"
    if "severity" in lower or "priority" in lower:
        return "SEVERITY"
    if "status" in lower or "state" in lower or "resulttype" in lower:
        return "STATUS"
    if "action" in lower or "operation" in lower or lower == "activity":
        return "ACTION"
    if source_type == "dynamic":
        return "JSON"
    return "TEXT"


def infer_join_key(name: str, semantic_type: str) -> tuple[str | None, str | None]:
    lower = name.lower()
    if semantic_type == "TENANT_ID":
        return "tenant_id", None
    if name == "AlertId":
        return "alert_id", None
    if name == "SystemAlertId":
        return "system_alert_id", None
    if name == "DeviceId":
        return "device_id", None
    if name == "AadDeviceId":
        return "aad_device_id", None
    if name in {"DeviceName", "HostName", "Computer", "ClientMachineName"}:
        return "device_name", "normalize_fqdn"
    if lower.endswith("accountsid") or name in {"LogonUserSid", "MailboxOwnerSid", "DestMailboxOwnerSid"}:
        return "account_sid", None
    if name in {
        "AccountObjectId",
        "InitiatingProcessAccountObjectId",
        "RecipientObjectId",
        "SenderObjectId",
        "AadUserId",
    }:
        return "account_object_id", None
    if name in {"AccountUpn", "InitiatingProcessAccountUpn", "UserPrincipalName"}:
        return "account_upn", "lowercase"
    if semantic_type == "EMAIL_ADDRESS":
        return "email_address", "lowercase"
    if semantic_type == "IP_ADDRESS":
        return "ip_address", "strip_port_and_normalize_ip"
    if name == "NetworkMessageId":
        return "network_message_id", None
    if name == "InternetMessageId":
        return "internet_message_id", None
    if lower.endswith("sha1"):
        return "sha1", "lowercase"
    if lower.endswith("sha256"):
        return "sha256", "lowercase"
    if name in {"ProcessId", "InitiatingProcessId"}:
        return "process_id", None
    if name == "CorrelationId":
        return "correlation_id", None
    if name == "ResourceId":
        return "resource_id", "lowercase"
    return None, None


def describe_field(table_name: str, field_name: str, semantic_type: str) -> tuple[str, str]:
    if field_name in FIELD_DESCRIPTIONS:
        return FIELD_DESCRIPTIONS[field_name]
    zh_label = SEMANTIC_LABELS_ZH[semantic_type]
    return (
        f"{table_name} 中的{zh_label}字段（{field_name}）。",
        f"{field_name} field in {table_name}, representing {semantic_type.lower().replace('_', ' ')}.",
    )


def default_time_field(fields: list[dict[str, Any]]) -> str | None:
    names = {field["name"] for field in fields}
    for candidate in ("TimeGenerated", "Timestamp", "CreatedDateTime", "StartTime", "EventTime"):
        if candidate in names:
            return candidate
    return next((field["name"] for field in fields if field["is_time_field"]), None)


def read_schemas(source: Path) -> tuple[dict[str, list[dict[str, str]]], int]:
    schemas: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_file_count = 0
    with ZipFile(source) as archive:
        for entry_name in sorted(archive.namelist()):
            path = PurePosixPath(entry_name)
            if path.suffix != ".meta" or path.name.startswith("._"):
                continue
            if len(path.parts) < 4 or path.parts[:2] != ("data", "csv_files"):
                continue
            payload = json.loads(archive.read(entry_name))
            if not isinstance(payload, dict) or not payload:
                continue
            table_name = logical_table_name(path)
            schemas[table_name].append({str(name): str(data_type) for name, data_type in payload.items()})
            source_file_count += 1
    if not schemas:
        raise ValueError(f"No ExCyTIn-Bench schema metadata found in {source}")
    return dict(schemas), source_file_count


def build_field(table_name: str, field_name: str, source_types: list[str]) -> dict[str, Any]:
    source_type = Counter(source_types).most_common(1)[0][0]
    semantic_type = infer_semantic_type(field_name, source_type)
    join_key, normalization = infer_join_key(field_name, semantic_type)
    description_zh, description_en = describe_field(table_name, field_name, semantic_type)
    field_id = f"{CATALOG_ID}:{table_name}:{field_name}"
    return {
        "field_id": field_id,
        "name": field_name,
        "data_type": source_type,
        "source_types": sorted(set(source_types)),
        "semantic_type": semantic_type,
        "description_zh": description_zh,
        "description_en": description_en,
        "semantic_text": " | ".join(
            [table_name, field_name, semantic_type, description_zh, description_en]
        ),
        "is_identifier": join_key is not None,
        "join_key": join_key,
        "normalization": normalization,
        "is_time_field": semantic_type == "TIMESTAMP",
        "is_dynamic": source_type == "dynamic",
        "is_technical": field_name == "rn",
        "aliases": [],
    }


def build_tables(raw_schemas: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table_name in sorted(raw_schemas):
        variants = raw_schemas[table_name]
        field_types: dict[str, list[str]] = defaultdict(list)
        variant_hashes = Counter(stable_hash(schema) for schema in variants)
        for schema in variants:
            for field_name, source_type in schema.items():
                field_types[field_name].append(source_type)

        fields = [
            build_field(table_name, field_name, field_types[field_name])
            for field_name in sorted(field_types)
        ]
        log_type = classify_table(table_name)
        description_zh, description_en = describe_table(table_name, log_type)
        semantic_types = sorted({field["semantic_type"] for field in fields})
        keywords = sorted(set(camel_words(table_name) + log_type.split("_") + [item.lower() for item in semantic_types]))
        schema_signature = {field["name"]: field["data_type"] for field in fields}
        tables.append(
            {
                "table_id": f"{CATALOG_ID}:{table_name}",
                "name": table_name,
                "log_type": log_type,
                "log_layer": table_layer(log_type),
                "source_product": source_product(table_name, log_type),
                "description_zh": description_zh,
                "description_en": description_en,
                "default_time_field": default_time_field(fields),
                "field_count": len(fields),
                "schema_hash": stable_hash(schema_signature),
                "source_schema_count": len(variants),
                "schema_variant_count": len(variant_hashes),
                "keywords": keywords,
                "retrieval_hints": list(TABLE_RETRIEVAL_HINTS.get(table_name, ())),
                "semantic_text": " | ".join(
                    [table_name, description_zh, description_en, " ".join(keywords)]
                ),
                "fields": fields,
            }
        )
    return tables


def build_join_keys(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_join_key: dict[str, list[str]] = defaultdict(list)
    for table in tables:
        for field in table["fields"]:
            if field["join_key"]:
                by_join_key[field["join_key"]].append(field["field_id"])

    join_keys: list[dict[str, Any]] = []
    for join_key, members in sorted(by_join_key.items()):
        config = JOIN_KEY_CONFIG[join_key]
        join_keys.append(
            {
                "join_key_id": f"{CATALOG_ID}:join-key:{join_key}",
                "name": join_key,
                "match_method": config["match_method"],
                "join_scope": config["join_scope"],
                "confidence": config["confidence"],
                "field_count": len(members),
                "field_ids": sorted(members),
                "source": "schema_semantics",
            }
        )
    return join_keys


def build_knowledge(source: Path) -> dict[str, Any]:
    raw_schemas, source_schema_files = read_schemas(source)
    tables = build_tables(raw_schemas)
    join_keys = build_join_keys(tables)
    total_fields = sum(table["field_count"] for table in tables)
    knowledge = {
        "format_version": "1.0",
        "generated_at": utc_now(),
        "generation_policy": {
            "scenario_independent": True,
            "contains_log_values": False,
            "contains_incident_labels": False,
            "source_of_truth": "ExCyTIn-Bench .meta files",
        },
        "environment": {
            "environment_id": ENVIRONMENT_ID,
            "name": "ExCyTIn-Bench Azure Security Environment",
            "platform": "Microsoft Azure / Microsoft Sentinel",
            "database_type": "MySQL",
            "query_language": "SQL",
            "description_zh": "用于网络威胁调查的 Azure 安全日志环境。",
            "description_en": "Azure security-log environment for cyber-threat investigation.",
        },
        "catalog": {
            "catalog_id": CATALOG_ID,
            "name": "ExCyTIn-Bench Log Catalog",
            "version": "1.0",
            "source": "data/excytin-bench/huggingface/data.zip",
            "source_sha256": file_hash(source),
            "source_schema_files": source_schema_files,
            "table_count": len(tables),
            "field_count": total_fields,
            "join_key_count": len(join_keys),
        },
        "tables": tables,
        "join_keys": join_keys,
    }
    knowledge["catalog"]["schema_hash"] = stable_hash(
        {
            "tables": [{"name": table["name"], "schema_hash": table["schema_hash"]} for table in tables],
            "join_keys": join_keys,
        }
    )
    return knowledge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ExCyTIn-Bench semantic-memory JSON from actual .meta schemas.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Path to the Hugging Face data.zip archive.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Schema archive not found: {source}")
    knowledge = build_knowledge(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    catalog = knowledge["catalog"]
    print(
        f"Wrote {output} with {catalog['table_count']} tables, "
        f"{catalog['field_count']} fields, and {catalog['join_key_count']} join keys."
    )


if __name__ == "__main__":
    main()
