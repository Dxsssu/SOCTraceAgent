"""Reviewed schema-specific recovery guidance; provenance stays in build_audit.json.

Each rule requires a later executed evidence query using a listed field in the
same training trajectory. This is evidence of executable recovery, not proof
that all original predicates or the original question were resolved.
"""

# (failed table, error subject): (recovery table, supporting fields, guidance)
RULES = {
    ("SecurityAlert", "Title"): (
        "SecurityAlert",
        ["AlertName", "DisplayName"],
        "查询告警名称时使用 AlertName；展示名称使用 DisplayName。Title 不存在。按字段含义选择，保留原时间、实体范围与布尔分组；同时检查查询中的其他不存在字段。",
    ),
    ("SecurityAlert", "AlertId"): (
        "SecurityAlert",
        ["SystemAlertId"],
        "读取 SecurityAlert 的 SystemAlertId。它不能未经验证直接当作 AlertInfo/AlertEvidence 的 AlertId；后续关联须确认标识命名空间与实际值。",
    ),
    ("SecurityAlert", "DeviceName"): (
        "SecurityAlert",
        ["CompromisedEntity"],
        "检查 CompromisedEntity 是否实际表示目标设备，再用当前设备线索定位；必要时检查 Entities。CompromisedEntity 是受影响实体，不保证总是设备，不能无条件替换。保留时间范围并核对结果。",
    ),
    ("SecurityAlert", "HostName"): (
        "SecurityAlert",
        ["CompromisedEntity"],
        "先核对 CompromisedEntity 的实体类型与实际内容；表示目标主机时可据此过滤，必要时检查 Entities。不要把所有受影响实体都解释为主机。",
    ),
    ("AlertInfo", "Description"): (
        "AlertInfo",
        ["Title"],
        "AlertInfo 没有 Description；告警名称按 Title 查询。需要类别或技术信息时分别核对 Category、AttackTechniques。若任务确需描述，需另查具有描述的日志并验证关联，不能把删除 Description 条件视为等价修复。",
    ),
    ("EmailEvents", "Sender"): (
        "EmailEvents",
        ["SenderFromAddress", "SenderMailFromAddress"],
        "根据问题区分 SenderFromAddress（From 地址）、SenderMailFromAddress（信封发件人）和 SenderDisplayName（显示名）。保留 NetworkMessageId/InternetMessageId、收件人及时间范围。",
    ),
    ("EmailEvents", "Recipient"): (
        "EmailEvents",
        ["RecipientEmailAddress"],
        "将收件地址条件改为 RecipientEmailAddress，并保留消息标识、发件人和时间限制；一封邮件可有多个收件人。",
    ),
    ("EmailEvents", "RecipientToAddress"): (
        "EmailEvents",
        ["RecipientEmailAddress"],
        "使用 RecipientEmailAddress 表示收件地址，保留原 SenderFromAddress、Subject、消息标识与时间条件，不因修复扩大收件人范围。",
    ),
    ("EmailEvents", "SenderIP"): (
        "EmailEvents",
        ["SenderIPv4", "SenderIPv6"],
        "按地址族使用 SenderIPv4 或 SenderIPv6；需要读取发送地址时可分别读取两列。保留邮件消息标识及收件人范围。",
    ),
    ("UrlClickEvents", "User"): (
        "UrlClickEvents",
        ["AccountUpn"],
        "点击账户使用 AccountUpn；用当前已核实的账户值过滤，保留 Url、消息标识与时间条件。不要再次猜测 UserPrincipalName。",
    ),
    ("UrlClickEvents", "UserPrincipalName"): (
        "UrlClickEvents",
        ["AccountUpn"],
        "使用 AccountUpn 表示点击账户，保留原 URL、消息和时间条件，并核对账户是否属于当前调查。",
    ),
    ("DeviceProcessEvents", "CommandLine"): (
        "DeviceProcessEvents",
        ["ProcessCommandLine"],
        "目标进程命令行使用 ProcessCommandLine；父/发起进程命令行使用 InitiatingProcessCommandLine。按问题区分两者，保留 ProcessId、设备与时间约束。",
    ),
    ("DeviceProcessEvents", "AadDeviceId"): (
        "DeviceInfo",
        ["AadDeviceId", "DeviceId"],
        "先在 DeviceInfo 中用已知 AadDeviceId 查询 DeviceId，再用取得的 DeviceId 查询 DeviceProcessEvents。两个设备标识不能直接互换；核对租户、设备与时间，保留原进程条件。",
    ),
    ("EmailUrlInfo", "InternetMessageId"): (
        "EmailPostDeliveryEvents",
        ["InternetMessageId", "NetworkMessageId"],
        "先在当前确实存在的 EmailPostDeliveryEvents 中按 InternetMessageId 查询 NetworkMessageId，再用 NetworkMessageId 查询 EmailUrlInfo.Url；按当前 SM 与实际结果验证两端标识及收件人范围，不能把 InternetMessageId 直接用于 EmailUrlInfo。",
    ),
    ("AlertEvidence", "EvidenceValue"): (
        "AlertEvidence",
        ["RemoteUrl"],
        "按目标实体类型选择具体字段。历史 URL 场景使用 RemoteUrl，不能用不存在的通用 EvidenceValue。保留告警标识和已知 URL 集合；其他实体类型需重新核对 SM。",
    ),
    ("AlertEvidence", "EvidenceDetails"): (
        "AlertEvidence",
        ["ProcessCommandLine"],
        "历史命令行场景改查 ProcessCommandLine；EvidenceDetails 不是可用字段。其他证据类型需按 SM 选择实际结构化字段，不能用命令行字段代替所有详细信息。",
    ),
    ("AlertEvidence", "EvidenceType"): (
        "AlertEvidence",
        ["RemoteIP", "FileName", "ProcessCommandLine"],
        "先按 SM 确认 EntityType 及具体实体字段的含义，按当前目标读取或过滤 RemoteIP、RemoteUrl、FileName、ProcessCommandLine 等实际字段。历史轨迹曾改用具体实体字段；保留 AlertId、设备与时间范围，不能直接删除实体类型条件后宣称同等查证完成。",
    ),
    ("AuditLogs", "UserPrincipalName"): (
        "AuditLogs",
        ["InitiatedBy"],
        "操作发起者信息位于 InitiatedBy；先检查实际结构与样本，确认对应账户的位置后构造筛选。历史查询对 InitiatedBy 做文本筛选仅是候选定位，仍需核实结构化身份；不编造 JSON 路径。保留 OperationName 和时间范围。",
    ),
    ("SigninLogs", "Description"): (
        "SigninLogs",
        ["RiskEventTypes", "RiskLevel", "RiskState"],
        "历史风险调查使用 RiskEventTypes、RiskLevel、RiskState 读取风险信息；不存在通用 Description。根据当前问题选择具体字段并保留账户、IP 与时间范围；不能把描述文本直接当作风险枚举。",
    ),
}
