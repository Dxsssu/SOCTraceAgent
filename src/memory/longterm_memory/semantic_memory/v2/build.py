"""Offline, deterministic compilation of reviewed mappings into two flat files.

No incident answers, log values, substring field inference, or LLM calls.
Existing L1/L2 are source material only; the runtime reads just the two JSONs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from .store import PACKAGE, SemanticMemoryV2

SOURCE = PACKAGE.parent / "excytin_bench"

# Explicit table scopes prevent confusing target and initiating-process fields,
# email header IDs and service IDs, or users and mailboxes with similar names.
DEVICE = ["DeviceProcessEvents", "DeviceNetworkEvents", "DeviceFileEvents", "DeviceEvents", "DeviceLogonEvents", "DeviceImageLoadEvents", "DeviceRegistryEvents"]
MAIL = ["EmailEvents", "EmailAttachmentInfo", "EmailUrlInfo", "EmailPostDeliveryEvents", "UrlClickEvents"]
SIGNIN = ["SigninLogs", "AADNonInteractiveUserSignInLogs", "AADServicePrincipalSignInLogs", "AADManagedIdentitySignInLogs"]
FIREWALL = ["AZFWNetworkRule", "AZFWApplicationRule", "AZFWNatRule", "AZFWThreatIntel"]

# concept, aliases, tables, exact field, semantic kind, concise meaning
RULES = [
    ("Email_ID", ["邮件ID", "邮件标识", "mail_id"], MAIL, "NetworkMessageId", "network_message_id", "服务生成的邮件标识"),
    ("Email_ID", ["邮件ID", "邮件标识", "mail_id"], ["EmailEvents", "EmailPostDeliveryEvents"], "InternetMessageId", "internet_message_id", "邮件头 Message-ID"),
    ("Email_Sender", ["发件人", "发件人地址"], ["EmailEvents", "EmailAttachmentInfo"], "SenderFromAddress", "header_from", "From 信头地址"),
    ("Email_Sender", ["发件人", "发件人地址"], ["EmailEvents"], "SenderMailFromAddress", "envelope_from", "信封发件人地址"),
    ("Email_Recipient", ["收件人", "收件人地址"], ["EmailEvents", "EmailAttachmentInfo", "EmailPostDeliveryEvents"], "RecipientEmailAddress", "mailbox_address", "收件人邮箱地址"),
    ("Email_Subject", ["邮件主题", "主题"], ["EmailEvents"], "Subject", "subject", "邮件主题"),
    ("File_Hash", ["文件哈希", "文件Hash"], ["EmailAttachmentInfo", "DeviceFileEvents", "DeviceProcessEvents", "DeviceImageLoadEvents"], "SHA256", "sha256", "目标文件 SHA-256"),
    ("File_Hash", ["文件哈希", "文件Hash"], ["DeviceFileEvents", "DeviceProcessEvents", "DeviceImageLoadEvents"], "SHA1", "sha1", "目标文件 SHA-1"),
    ("File_Hash", ["文件哈希", "文件Hash"], ["DeviceFileEvents", "DeviceProcessEvents", "DeviceImageLoadEvents"], "MD5", "md5", "目标文件 MD5"),
    ("File_Name", ["文件名", "附件名"], ["EmailAttachmentInfo", "DeviceFileEvents", "DeviceProcessEvents", "DeviceImageLoadEvents"], "FileName", "file_name", "目标文件名"),
    ("Folder_Path", ["文件夹路径", "目录路径"], ["DeviceFileEvents", "DeviceProcessEvents", "DeviceImageLoadEvents"], "FolderPath", "folder_path", "目标文件所在目录"),
    ("Device_Name", ["主机名", "设备名", "hostname"], DEVICE, "DeviceName", "device_name", "事件所属设备名称"),
    ("Device_ID", ["设备ID", "主机ID"], DEVICE, "DeviceId", "defender_device_id", "Defender 设备标识"),
    ("Process_ID", ["进程ID", "PID"], ["DeviceProcessEvents", "DeviceEvents"], "ProcessId", "target_pid", "目标进程 PID；须结合主机与时间"),
    ("Process_Command", ["进程命令行", "命令行"], ["DeviceProcessEvents", "DeviceEvents"], "ProcessCommandLine", "target_command", "目标进程命令行"),
    ("Process_Start", ["进程创建时间"], ["DeviceProcessEvents", "DeviceEvents"], "ProcessCreationTime", "target_start", "目标进程创建时间"),
    ("Initiating_Process_ID", ["发起进程ID"], DEVICE, "InitiatingProcessId", "initiating_pid", "发起此事件的进程 PID"),
    ("Initiating_Process_Command", ["发起进程命令行"], DEVICE, "InitiatingProcessCommandLine", "initiating_command", "发起进程命令行"),
    ("Account_Name", ["账户名", "用户名"], ["DeviceProcessEvents", "DeviceLogonEvents", "DeviceEvents"], "AccountName", "account_name", "目标账户名；需考虑域"),
    ("Account_UPN", ["账户UPN", "用户主体名称"], ["DeviceProcessEvents", "DeviceLogonEvents", "UrlClickEvents"], "AccountUpn", "upn", "事件账户 UPN"),
    ("Account_UPN", ["账户UPN", "用户主体名称"], ["SigninLogs", "AADNonInteractiveUserSignInLogs", "AADUserRiskEvents", "AADRiskyUsers"], "UserPrincipalName", "upn", "事件用户 UPN"),
    ("Source_IP", ["源IP", "来源IP"], FIREWALL, "SourceIp", "source_ip", "防火墙流量源 IP"),
    ("Destination_IP", ["目的IP", "目标IP"], ["AZFWNetworkRule", "AZFWNatRule", "AZFWThreatIntel"], "DestinationIp", "destination_ip", "防火墙流量目的 IP"),
    ("Remote_IP", ["远端IP", "远程IP"], ["DeviceNetworkEvents"], "RemoteIP", "remote_ip", "连接远端 IP；不自动等于目的 IP"),
    ("Local_IP", ["本地IP"], ["DeviceNetworkEvents"], "LocalIP", "local_ip", "连接本地 IP"),
    ("Login_IP", ["登录IP"], SIGNIN, "IPAddress", "login_ip", "身份验证客户端 IP"),
    ("Login_Result", ["登录结果"], SIGNIN, "ResultType", "result_code", "身份验证结果码"),
    ("URL", ["网址", "链接"], ["EmailUrlInfo"], "Url", "email_url", "邮件内链接；不证明点击"),
    ("URL", ["网址", "链接"], ["UrlClickEvents"], "Url", "clicked_url", "点击事件关联 URL"),
    ("Click_Account", ["点击用户", "点击账户"], ["UrlClickEvents"], "AccountUpn", "upn", "点击事件账户 UPN"),
]


# Retrieval roles are hints. The IP family keeps exact physical semantics per row.
IP_NAMES = {"Source_IP", "Destination_IP", "Remote_IP", "Local_IP", "Login_IP"}
IP_ALIASES = ["IP地址", "IP address", "IP_Address", "源IP", "来源IP", "目的IP", "目标IP", "远端IP", "远程IP", "本地IP", "登录IP", *sorted(IP_NAMES)]
INTENTS = {
    "authentication": ["登录", "登陆", "认证", "身份验证", "login", "logon", "sign-in", "sign in", "signin", "authentication", "authenticate"],
    "click": ["点击", "click", "clicked", "clicking"],
    "email": ["邮件", "发件人", "收件人", "email", "mail", "sender", "recipient"],
    "process": ["进程", "执行", "命令行", "process", "command", "execution", "executed", "pid"],
    "network": ["连接", "网络", "network", "connection", "connect", "traffic"],
    "firewall": ["防火墙", "firewall", "nat"],
    "file": ["文件", "附件", "file", "attachment", "hash"],
    "risk": ["风险检测", "risk detection", "risky user"],
}
TABLE_INTENTS = {
    **{t: ["authentication"] for t in SIGNIN + ["DeviceLogonEvents"]},
    "UrlClickEvents": ["click", "email"],
    **{t: ["email"] for t in MAIL if t != "UrlClickEvents"},
    "DeviceProcessEvents": ["process"], "DeviceEvents": ["process"],
    "DeviceNetworkEvents": ["network"],
    **{t: ["firewall", "network"] for t in FIREWALL},
    "DeviceFileEvents": ["file"], "DeviceImageLoadEvents": ["file"],
    "AADUserRiskEvents": ["risk"], "AADRiskyUsers": ["risk"],
}
RULES.extend([
    ("IP", IP_ALIASES, ["UrlClickEvents"], "IPAddress", "click_ip", "点击事件的客户端 IP；不等同邮件发送 IP"),
    ("IP", IP_ALIASES, ["AADUserRiskEvents"], "IpAddress", "risk_ip", "风险检测关联 IP；不证明发生登录"),
    ("Application", ["应用", "应用名", "应用名称", "application", "App_Name"], SIGNIN, "AppDisplayName", "application_name", "登录目标应用显示名称"),
    ("Login_Time", ["认证时间", "登录时间", "authentication_time"], SIGNIN, "CreatedDateTime", "login_time", "身份验证事件创建时间；区别于日志生成时间"),
])



# Exact-name extensions reviewed for entity identity. Meanings are always taken
# from each table's source card, never inferred from a substring of the name.
ENTITY_FIELDS = {
    "Email_ID": {"NetworkMessageId": "network_message_id", "InternetMessageId": "internet_message_id"},
    "Email_Subject": {"EmailSubject": "subject"},
    "Device_Name": {"DeviceName": "device_name"},
    "Device_ID": {"DeviceId": "defender_device_id"},
    "Account_Name": {"AccountName": "account_name"},
    "Account_UPN": {"AccountUpn": "upn", "UserPrincipalName": "upn"},
    "File_Hash": {"SHA256": "sha256", "SHA1": "sha1", "MD5": "md5"},
    "File_Name": {"FileName": "file_name"},
    "Folder_Path": {"FolderPath": "folder_path"},
    "Process_Command": {"ProcessCommandLine": "target_command"},
    "Application": {"Application": "application_name"},
    "Alert_ID": {"AlertId": "alert_id", "SystemAlertId": "system_alert_id"},
    "Alert_Title": {"Title": "title", "AlertName": "alert_name"},
    "Action_Type": {"ActionType": "action_type", "Operation": "operation"},
}
NEW_ALIASES = {
    "Alert_ID": ["告警ID", "警报ID"],
    "Alert_Title": ["告警标题", "警报名称", "Title", "AlertName"],
    "Action_Type": ["操作类型", "活动类型"],
}


def field_meaning(column):
    # Remove only the repeated storage preamble, retaining semantic caveats.
    return column.get("desc", "字段语义未标注；需核实后使用").split("存储。", 1)[-1]


def build(source: Path = SOURCE) -> tuple[dict, dict]:
    cards = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((source / "l2").glob("*.yaml"))}
    annotations = json.loads((source / "catalog_annotations.json").read_text(encoding="utf-8"))["tables"]
    concepts, tables = {}, {}
    for concept, aliases, names, column, kind, meaning in RULES:
        if concept in IP_NAMES:
            concept, aliases = "IP", IP_ALIASES
        item = concepts.setdefault(concept, {"aliases": list(aliases), "fields": []})
        if concept == "IP":
            item["kind_policy"] = "hint"
        for table in names:
            card = cards[table]
            columns = {c["name"]: c for c in card["core_columns"]}
            # Table-specific variation is explicit in the checked-in dictionary.
            if column not in columns:
                continue
            item["fields"].append({"table": table, "field": column, "type": columns[column]["type"], "kind": kind, "meaning": meaning})
            if table not in tables:
                time = columns.get("TimeGenerated")
                tables[table] = {
                    "description": annotations[table]["desc"],
                    "intent_keywords": [word for tag in TABLE_INTENTS.get(table, []) for word in INTENTS[tag]],
                    "intent_priority": 1 if table in {"SigninLogs", "DeviceProcessEvents", "DeviceNetworkEvents"} else 0,
                    "time_field": {"field": "TimeGenerated", "type": time["type"], "meaning": "日志记录生成时间 UTC；不等同实际操作发生时间"} if time else None,
                }
    # Register the entire environment independently of curated concept coverage.
    for table, card in cards.items():
        columns = {c["name"]: c for c in card["core_columns"]}
        if table not in tables:
            time = columns.get("TimeGenerated")
            tables[table] = {
                "description": annotations[table]["desc"],
                "intent_keywords": [], "intent_priority": 0,
                "time_field": {"field": "TimeGenerated", "type": time["type"],
                               "meaning": "日志记录生成时间 UTC；不等同实际操作发生时间"} if time else None,
            }
        for concept, names in ENTITY_FIELDS.items():
            for field, kind in names.items():
                if field not in columns:
                    continue
                # Title is an alert title only in these two tables.
                if concept == "Alert_Title" and table not in {"AlertInfo", "AlertEvidence", "SecurityAlert"}:
                    continue
                item = concepts.setdefault(concept, {"aliases": NEW_ALIASES.get(concept, []), "fields": []})
                if not any(r["table"] == table and r["field"] == field for r in item["fields"]):
                    item["fields"].append({"table": table, "field": field, "type": columns[field]["type"],
                                           "kind": kind, "meaning": field_meaning(columns[field])})
        # All columns have an exact physical-field entry, hidden from the initial
        # vocabulary. Query Field:<name> (or an otherwise unknown exact name).
        # These are structural mappings, not assertions of cross-table equivalence.
        for field, col in columns.items():
            physical_name = next((n for n in concepts if n.casefold() == ("Field:" + field).casefold()), "Field:" + field)
            item = concepts.setdefault(physical_name, {"aliases": [], "expose_in_vocabulary": False, "fields": []})
            item["fields"].append({"table": table, "field": field, "type": col["type"],
                                   "kind": "physical_field_or_scope", "meaning": field_meaning(col)})
    for concept, aliases in NEW_ALIASES.items():
        concepts[concept]["aliases"] = aliases
    # IP observation roles stay distinct; an evidence IP is not a login event.
    for table, field, kind in [
        ("AlertEvidence", "RemoteIP", "evidence_remote_ip"),
        ("AlertEvidence", "LocalIP", "evidence_local_ip"),
        ("CloudAppEvents", "IPAddress", "cloud_activity_ip"),
        ("DeviceInfo", "PublicIP", "device_public_ip"),
        ("IdentityLogonEvents", "IPAddress", "login_ip"),
        ("IdentityDirectoryEvents", "IPAddress", "directory_activity_ip"),
        ("IdentityQueryEvents", "IPAddress", "identity_query_ip"),
        ("OfficeActivity", "ClientIP", "office_client_ip"),
        ("OfficeActivity", "ActorIpAddress", "office_actor_ip"),
    ]:
        columns = {c["name"]: c for c in cards[table]["core_columns"]}
        col = columns[field]
        concepts["IP"]["fields"].append({"table": table, "field": field, "type": col["type"],
                                           "kind": kind, "meaning": field_meaning(col)})
    # Event_Time exposes table-specific time metadata without equating it with login/process creation.
    concepts["Event_Time"] = {"aliases": ["时间", "事件时间", "日志时间", "Time", "timestamp"], "fields": [
        {"table": name, **brief["time_field"], "kind": "log_time"}
        for name, brief in tables.items() if brief["time_field"]]}
    if any(not c["fields"] for c in concepts.values()):
        raise ValueError("A declared concept has no source fields")
    for item in concepts.values():
        item["fields"].sort(key=lambda r: (r["table"], r["field"]))
    payload = json.dumps([concepts, tables], ensure_ascii=False, sort_keys=True).encode()
    metadata = {
        "version": "semantic-flat-v2", "environment_id": "excytin-azure-mysql",
        "catalog_id": hashlib.sha256(payload).hexdigest()[:16],
        "provenance": "excytin_bench/l2 + catalog_annotations.json; explicit v2 mappings; no log rows or answers",
    }
    documents = ({**metadata, "concepts": concepts}, {**metadata, "tables": tables})
    SemanticMemoryV2(*documents)
    return documents


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PACKAGE / "excytin")
    args = parser.parse_args()
    concepts, tables = build()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, doc in (("concepts.json", concepts), ("tables.json", tables)):
        (args.output / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"concepts": len(concepts["concepts"]), "tables": len(tables["tables"]), "mappings": sum(len(c["fields"]) for c in concepts["concepts"].values()), "catalog_id": concepts["catalog_id"]}))


if __name__ == "__main__":
    main()
