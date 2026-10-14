"""Render mined patterns as short, prerequisite-gated investigation fragments."""

from .mining import TABLE_GOALS

MEANINGS = {
    "AlertId": "告警标识",
    "Categories": "告警类别",
    "Category": "告警类别",
    "Title": "标题线索",
    "DeviceName": "设备名称",
    "DeviceId": "设备标识",
    "FileName": "文件名称",
    "ProcessCommandLine": "目标进程命令内容",
    "RemoteIP": "远端网络地址",
    "ThreatFamily": "威胁家族",
    "AttackTechniques": "攻击技术",
    "RemoteUrl": "远端网址",
    "ActionType": "活动类型",
    "InitiatingProcessFileName": "发起进程文件名称",
    "ProcessCreationTime": "进程创建时间",
    "ProcessId": "目标进程编号",
    "InitiatingProcessCommandLine": "发起进程命令内容",
    "InternetMessageId": "邮件头中的消息标识",
    "NetworkMessageId": "邮件系统分配的消息标识",
    "SenderFromAddress": "发件人地址",
    "ThreatNames": "威胁名称",
    "ThreatTypes": "威胁类型",
    "Action": "投递后处置动作",
    "Url": "网址",
    "AlertName": "告警名称",
    "CompromisedEntity": "受影响实体",
    "Description": "描述线索",
    "DisplayName": "显示名称",
    "Tactics": "攻击战术",
    "Entities": "告警实体内容",
    "IPAddress": "登录来源地址",
    "UserDisplayName": "用户显示名称",
    "UserPrincipalName": "用户登录账号",
    "account_upn": "用户登录账号",
    "alert_id": "告警标识",
    "device_name": "设备名称",
    "internet_message_id": "邮件头中的消息标识",
    "network_message_id": "邮件系统分配的消息标识",
}


TABLE_LABELS = {
    "AlertInfo": "告警概要",
    "AlertEvidence": "告警证据",
    "SecurityAlert": "安全告警",
    "SecurityIncident": "安全事件",
    "DeviceProcessEvents": "进程活动",
    "DeviceNetworkEvents": "网络连接",
    "DeviceFileEvents": "文件活动",
    "DeviceInfo": "设备信息",
    "EmailEvents": "邮件详情",
    "EmailUrlInfo": "邮件链接",
    "EmailPostDeliveryEvents": "邮件投递后处置",
    "UrlClickEvents": "链接点击",
    "SigninLogs": "账号登录",
    "AADRiskyUsers": "风险账号",
}

# References describe query inputs/outputs; field meanings stay in Semantic Memory.
OUTPUTS = {
    "AlertInfo": "AlertId Title Category AttackTechniques",
    "AlertEvidence": "AlertId DeviceId DeviceName AccountUpn FileName ProcessCommandLine RemoteIP NetworkMessageId EntityType EvidenceRole",
    "SecurityAlert": "AlertName Description CompromisedEntity Entities ExtendedProperties",
    "SecurityIncident": "Title Description IncidentNumber",
    "DeviceProcessEvents": "ProcessId ProcessCommandLine InitiatingProcessId InitiatingProcessCommandLine AccountName DeviceId DeviceName ProcessCreationTime",
    "DeviceNetworkEvents": "DeviceId DeviceName RemoteIP RemotePort RemoteUrl InitiatingProcessId InitiatingProcessCommandLine",
    "DeviceFileEvents": "FileName FolderPath ActionType InitiatingProcessId InitiatingProcessCommandLine",
    "DeviceInfo": "DeviceId DeviceName OSPlatform",
    "EmailEvents": "NetworkMessageId SenderFromAddress RecipientEmailAddress ThreatTypes ThreatNames DeliveryAction DeliveryLocation",
    "EmailUrlInfo": "NetworkMessageId Url UrlDomain",
    "EmailPostDeliveryEvents": "NetworkMessageId InternetMessageId Action ActionType RecipientEmailAddress",
    "UrlClickEvents": "Url AccountUpn IPAddress ActionType",
    "SigninLogs": "UserPrincipalName IPAddress ResultType ResultDescription",
    "AADRiskyUsers": "UserPrincipalName RiskLevel RiskState",
}


def render_fragments(procedures, audit, semantic, records):
    """Render natural-language fragments with validated, explicit schema links."""
    entries = {p["name"]: p for p in audit["procedures"]}
    attempts = {r["attempt_id"]: r for r in records}
    tables_by_name = {t["name"]: t for t in semantic["tables"]}
    keys_by_name = {k["name"]: k for k in semantic["join_keys"]}
    for p in procedures:
        old_name = p["name"]
        entry = entries[old_name]
        family = entry["family"]
        required = {}
        if family == "lookup":
            table, fields = old_name.split("：按 ")
            fields = fields.removesuffix(" 查证").split("、")
            labels = [MEANINGS[f] for f in fields]
            clues = "、".join(labels)
            p["name"] = f"根据{clues}查证{TABLE_LABELS[table]}"
            p["goal"] = f"根据已知的{clues}，{TABLE_GOALS[table]}。"
            p["input_slots"] = {
                label: f"本次调查已提供或已查得{label}的具体内容。" for label in labels
            }
            p["operations"] = (
                f"用已知的{clues}筛选{TABLE_LABELS[table]}，在当前调查范围内读取问题所需的信息。"
            )
            if table == "DeviceProcessEvents":
                p["operations"] = (
                    f"用已知的{clues}及已有设备、时间线索定位进程，按问题读取目标进程或父进程的信息。"
                )
            if table == "EmailEvents":
                p["operations"] = (
                    f"用已知的{clues}定位邮件，在当前收件人范围内读取所需的发件人、收件人或投递情况。"
                )
            required[table] = set(fields)
        elif family in {"pivot", "chain"}:
            route, keys = old_name.split(" (")
            tables = route.split(" → ")
            keys = keys.removesuffix(")").split(" / ")
            label = MEANINGS[keys[0]]
            readable_route = "关联".join(TABLE_LABELS[t] for t in tables)
            p["name"] = f"通过{label}从{readable_route}"
            p["goal"] = f"从已有的{label}出发，{TABLE_GOALS[tables[-1]]}。"
            p["input_slots"] = {
                label: f"本次调查已取得{label}的具体值，且能确认它属于当前调查对象。"
            }
            p["operations"] = (
                f"用已核实的{label}关联{TABLE_LABELS[tables[-1]]}，在当前调查范围内读取问题所需的信息。"
            )
            if family == "chain":
                p["operations"] = (
                    "用已知的邮件标识关联邮件链接与邮件详情，读取当前问题所需的邮件信息。"
                )
            if keys[0] == "device_name":
                p["operations"] = (
                    "用已知设备查找网络连接，结合发起进程和时间核对哪些连接属于目标进程。"
                )
            for t in tables:
                required[t] = set()
            for index, key in enumerate(keys):
                for t in tables[index : index + 2]:
                    required[t].update(
                        f["name"]
                        for f in tables_by_name[t]["fields"]
                        if f["field_id"] in keys_by_name[key]["field_ids"]
                    )
        elif family == "sample":
            table = old_name.split("：")[0]
            p["name"] = f"抽样查看{TABLE_LABELS[table]}以寻找线索"
            p["goal"] = f"缺少定位线索时，查看少量样本以便{TABLE_GOALS[table]}。"
            p["input_slots"] = {
                "调查对象": "已明确要查看哪类活动记录。",
                "缺少定位线索": "当前尚无可直接筛选目标记录的具体线索。",
            }
            p["operations"] = (
                f"读取少量{TABLE_LABELS[table]}样本，从中提取可用于定位目标的线索。"
            )
            required[table] = set()
        elif family == "schema":
            p["name"] = "确认调查所需的记录结构"
            p["goal"] = "不清楚记录结构或信息含义时，确认完成当前查询需要哪些信息。"
            p["input_slots"] = {
                "调查目标": "已明确当前要查明的事实。",
                "结构未知": "当前缺少相关记录的可靠结构说明。",
            }
            p["operations"] = (
                "查阅当前调查涉及的记录结构，确认目标信息与筛选线索的位置和含义。"
            )
        elif family == "error_repair":
            p["name"] = "修正查询中不存在的信息项"
            p["goal"] = "查询提示所用信息项不存在时，修正查询后继续原调查。"
            p["input_slots"] = {
                "失败查询": "已保留本次失败查询及其调查目标。",
                "实际错误": "数据库已明确返回所用信息项不存在的错误。",
            }
            p["operations"] = "对照结构说明修正报错的信息项，保留原调查条件重新查询。"
        elif family == "empty_repair":
            focus = {
                "空结果：核对值与匹配方式": "线索内容和匹配方式",
                "空结果：检查筛选字段映射": "线索与记录信息的对应关系",
                "空结果：逐项核查过度约束": "不适用的筛选条件",
            }[old_name]
            p["name"] = f"查询无结果时核查{focus}"
            p["goal"] = f"查询成功但没有记录时，检查{focus}并继续查证。"
            p["input_slots"] = {
                "空结果": "本次查询已成功执行且返回零条记录。",
                "原查询条件": "已保留原查询及其使用的线索、范围和调查目标。",
            }
            p["operations"] = f"依据当前证据修正{focus}，保留有效的调查范围重新查询。"
        if not required:
            # Generic recovery/schema fragments reference only tables and valid fields
            # observed in their supporting training attempts, never invented fields.
            for ev in entry["evidence"]:
                for aid in ev["attempt_ids"]:
                    r = attempts[aid]
                    if r["table"] in tables_by_name:
                        required.setdefault(r["table"], set()).update(r["fields"])
        p["goal"] = p["goal"].replace(" ID", "标识").replace("URL", "网址")
        p["semantic_refs"] = []
        for table, fields in sorted(required.items()):
            available = {f["name"] for f in tables_by_name[table]["fields"]}
            desired = (
                fields
                | set(OUTPUTS.get(table, "").split())
                | {"TenantId", "Timestamp", "TimeGenerated"}
            )
            p["semantic_refs"].append(
                {"table": table, "fields": sorted(desired & available)}
            )
        entry["source_pattern"] = old_name
        entry["name"] = p["name"]
    procedures.sort(key=lambda p: p["name"])
    audit["procedures"].sort(key=lambda p: p["name"])
    return procedures
