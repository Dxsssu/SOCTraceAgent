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


def load_descriptions() -> dict[str, Any]:
    return json.loads((PACKAGE_DIR / "descriptions.json").read_text(encoding="utf-8"))["tables"]


def build_tables(raw_schemas: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    descriptions = load_descriptions()
    if set(descriptions) != set(raw_schemas):
        raise ValueError("Description catalog must cover exactly the source tables")
    tables: list[dict[str, Any]] = []
    for table_name in sorted(raw_schemas):
        field_types: dict[str, list[str]] = defaultdict(list)
        for schema in raw_schemas[table_name]:
            for field_name, source_type in schema.items():
                field_types[field_name].append(source_type)
        description = descriptions[table_name]
        if set(description["field"]) != set(field_types):
            raise ValueError(f"Description coverage differs from schema: {table_name}")
        fields = []
        for field_name in sorted(field_types):
            field_description = description["field"][field_name]
            fields.append({
                "field_id": f"{CATALOG_ID}:{table_name}:{field_name}",
                "name": field_name,
                "data_type": Counter(field_types[field_name]).most_common(1)[0][0],
                "description": field_description["description"],
                "description_zh": field_description["description_zh"],
            })
        tables.append({
            "table_id": f"{CATALOG_ID}:{table_name}",
            "name": table_name,
            "description": description["description"],
            "description_zh": description["description_zh"],
            "field": fields,
        })
    return tables


def build_join_keys(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_join_key: dict[str, list[str]] = defaultdict(list)
    for table in tables:
        for field in table["field"]:
            semantic_type = infer_semantic_type(field["name"], field["data_type"])
            join_key, _ = infer_join_key(field["name"], semantic_type)
            if join_key:
                by_join_key[join_key].append(field["field_id"])

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
    total_fields = sum(len(table["field"]) for table in tables)
    knowledge = {
        "format_version": "2.0",
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
            "version": "2.0",
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
            "tables": [{"name": table["name"], "schema_hash": stable_hash({field["name"]: field["data_type"] for field in table["field"]})} for table in tables],
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
