"""Reviewed investigation intents; SQL and support are mined from train, not authored here."""

# Exact physical-field contracts for the next entity/frontier. This avoids
# asking Planner to guess whether a returned mail ID is a header or service ID.
OUTPUT_ENTITIES = {
    'NetworkMessageId': ('Email_ID', 'network_message_id'),
    'InternetMessageId': ('Email_ID', 'internet_message_id'),
    'SenderFromAddress': ('Email_Sender', 'header_from'),
    'RecipientEmailAddress': ('Email_Recipient', 'mailbox_address'),
    'SenderIPv4': ('IP', 'sender_ipv4'),
    'DeviceId': ('Device_ID', 'defender_device_id'),
    'AadDeviceId': ('Device_ID', 'aad_device_id'),
    'DeviceName': ('Device_Name', 'device_name'),
    'AccountUpn': ('Account_UPN', 'upn'),
    'UserPrincipalName': ('Account_UPN', 'upn'),
    'AccountSid': ('Account_SID', 'sid'),
    'AlertId': ('Alert_ID', 'defender_alert_id'),
    'SystemAlertId': ('Alert_ID', 'sentinel_system_alert_id'),
    'RemoteIP': ('IP', 'remote_ip'),
    'LocalIP': ('IP', 'local_ip'),
    'RemoteUrl': ('URL', 'remote_url'),
    'Url': ('URL', 'url'),
    'FileName': ('File_Name', 'file_name'),
    'FolderPath': ('Folder_Path', 'folder_path'),
    'ProcessId': ('Process_ID', 'target_pid'),
    'ProcessCommandLine': ('Process_Command', 'target_command'),
    'InitiatingProcessId': ('Initiating_Process_ID', 'initiating_pid'),
    'InitiatingProcessCommandLine': ('Initiating_Process_Command', 'initiating_command'),
    'CreatedDateTime': ('Login_Time', 'login_time'),
    'TimeGenerated': ('Event_Time', 'log_time'),
}


def output_contract(table, field, description):
    concept, kind = OUTPUT_ENTITIES.get(field, (None, None))
    if field == 'IPAddress':
        concept, kind = 'IP', {'SigninLogs': 'login_ip', 'UrlClickEvents': 'click_ip'}.get(table, 'unspecified_ip')
    if field == 'IpAddress' and table == 'AADUserRiskEvents':
        concept, kind = 'IP', 'risk_ip'
    return {'description': description, 'entity_concept': concept, 'entity_kind': kind}

def rule(id, entity, kind, intent, aliases, table, field, outputs, caution):
    return dict(id=id, trigger_entity={"concept": entity, "kind": kind}, intent=intent,
                intent_aliases=aliases, table=table, field=field, outputs=outputs.split(), caution=caution)

RULES = [
    rule('mail_sender', 'Email_ID', 'network_message_id', '查询邮件发件人', ['邮件发件人','发件人','sender','sending address'], 'EmailEvents','NetworkMessageId','SenderFromAddress', 'From 信头地址不等于信封地址，也不证明真实操作者身份。'),
    rule('mail_recipient', 'Email_ID', 'network_message_id', '查询邮件收件人', ['邮件收件人','收件人','recipient'], 'EmailEvents','NetworkMessageId','RecipientEmailAddress', '收件人不等于点击者；保留各收件人的独立记录。'),
    rule('mail_sender_ip', 'Email_ID', 'network_message_id', '查询邮件发送 IP', ['发送IP','发送 IP','sender ip','sending ip'], 'EmailEvents','NetworkMessageId','SenderIPv4', '发送 IPv4 不等于用户登录 IP；IPv6 需要另查相应字段。'),
    rule('header_mail_sender', 'Email_ID', 'internet_message_id', '查询邮件发件人', ['邮件发件人','发件人','sender'], 'EmailEvents','InternetMessageId','SenderFromAddress SenderDisplayName', '仅接受邮件头 Message-ID；不得把 NetworkMessageId 填入此参数。'),
    rule('header_to_network_id', 'Email_ID', 'internet_message_id', '转换为平台邮件标识', ['平台邮件标识','network message id','服务邮件ID'], 'EmailPostDeliveryEvents','InternetMessageId','NetworkMessageId', '此表仅记录投递后事件；空结果不能证明邮件不存在，应回退邮件日志。'),
    rule('mail_urls', 'Email_ID', 'network_message_id', '查询邮件内链接', ['邮件链接','邮件内链接','mail url','email links'], 'EmailUrlInfo','NetworkMessageId','Url', '邮件包含链接不代表有人点击，也不证明该链接恶意。'),
    rule('url_mail_ids', 'URL', 'url', '定位包含链接的邮件', ['邮件标识','包含链接的邮件','message id','email containing'], 'EmailUrlInfo','Url','NetworkMessageId', '同一 URL 可以关联多封邮件；按完整 URL 匹配，不把域名当作完整链接。'),
    rule('sender_mails', 'Email_Sender', 'header_from', '查找发件人的邮件', ['发出的邮件','发件人的邮件','messages sent','sent emails'], 'EmailEvents','SenderFromAddress','NetworkMessageId RecipientEmailAddress Subject', 'From 地址可能伪造；保留每封邮件与收件人的对应关系。'),
    rule('recipient_mails', 'Email_Recipient', 'mailbox_address', '查找收件人的邮件', ['收到的邮件','收件人的邮件','received emails'], 'EmailEvents','RecipientEmailAddress','NetworkMessageId SenderFromAddress Subject', '收件记录不等于用户实际打开或点击。'),
    rule('mail_postdelivery', 'Email_ID', 'network_message_id', '查询邮件投递后处置', ['投递后处置','移除邮件','post delivery','removed','quarantine'], 'EmailPostDeliveryEvents','NetworkMessageId','Action RecipientEmailAddress', '按照实际 Action 判断处置；出现记录不自动等于已成功隔离。'),
    rule('url_clickers', 'URL', 'url', '查询链接点击账户', ['点击账户','点击者','clicked','clicker'], 'UrlClickEvents','Url','AccountUpn IPAddress', '点击事件账户不等于邮件收件人；不将点击 IP 当成代理日志的 IP。'),
    rule('account_clicks', 'Account_UPN', 'upn', '查询账户点击链接', ['点击链接','clicked links','click history'], 'UrlClickEvents','AccountUpn','Url IPAddress', '保留各次点击及对应 IP；不能仅由点击判定主机被攻陷。'),
    rule('ip_clicks', 'IP', 'click_ip', '查询 IP 对应点击事件', ['点击事件','click events'], 'UrlClickEvents','IPAddress','Url AccountUpn', '此处 IP 是点击客户端地址；NAT 或代理可能对应多个用户。'),
    rule('filename_commands', 'File_Name', 'file_name', '查询程序命令行', ['程序命令行','进程命令行','command line','commandline'], 'DeviceProcessEvents','FileName','ProcessCommandLine ProcessId DeviceName', '同名文件可在多台设备运行；PID 需结合设备和创建时间核对。'),
    rule('filename_parents', 'File_Name', 'file_name', '查询程序父进程', ['父进程','parent process'], 'DeviceProcessEvents','FileName','InitiatingProcessCommandLine InitiatingProcessId', 'InitiatingProcess 是发起方，不能当作目标进程；跨行关联需设备和时间。'),
    rule('command_processes', 'Process_Command', 'target_command', '定位执行命令的进程', ['执行命令的进程','定位执行进程','process executing','execution process'], 'DeviceProcessEvents','ProcessCommandLine','ProcessCommandLine ProcessId DeviceName', '模式匹配只是候选发现；保留完整命令行，不改变大小写或编码内容。'),
    rule('pid_process_context', 'Process_ID', 'target_pid', '查询进程上下文', ['进程上下文','process context'], 'DeviceProcessEvents','ProcessId','ProcessCommandLine DeviceName', 'PID 可重用且跨主机不唯一；此查询返回候选，需主机和创建时间才能唯一定位。'),
    rule('filename_origins', 'File_Name', 'file_name', '查询文件下载来源', ['下载来源','文件来源','download origin','file origin'], 'DeviceFileEvents','FileName','FileOriginUrl FileOriginIP DeviceName', '同名文件不保证同一内容；来源字段可为空，需哈希确认文件身份。'),
    rule('remote_ip_processes', 'IP', 'remote_ip', '查询远端连接发起进程', ['连接发起进程','network initiating process','connection process'], 'DeviceNetworkEvents','RemoteIP','InitiatingProcessCommandLine InitiatingProcessFileName RemoteUrl', 'RemoteIP 是远端地址；返回的进程是连接发起方，不自动判定连接方向。'),
    rule('remote_url_network', 'URL', 'remote_url', '查询远端地址网络连接', ['网络连接','network connection','proxy connection'], 'DeviceNetworkEvents','RemoteUrl','LocalIP RemoteIP RemoteUrl', '终端网络事件不等于代理服务器日志；核对 RemoteUrl 存储的是域名还是完整 URL。'),
    rule('alert_entities', 'Alert_ID', 'defender_alert_id', '查询告警关联实体', ['关联实体','alert entities','evidence entities'], 'AlertEvidence','AlertId','EntityType EvidenceRole DeviceName FileName AccountSid RemoteIP NetworkMessageId', '告警实体可能分散在多行；不同实体类型的空字段不能视为证据缺失。'),
    rule('alert_accounts', 'Alert_ID', 'defender_alert_id', '查询告警账户证据', ['账户证据','alert accounts'], 'AlertEvidence','AlertId','AccountName AccountSid AccountDomain', 'SID 与名称需保留域信息；告警关联账户不自动等于攻击者。'),
    rule('alert_processes', 'Alert_ID', 'defender_alert_id', '查询告警进程证据', ['进程证据','alert processes'], 'AlertEvidence','AlertId','FileName ProcessCommandLine DeviceName', '告警摘要可能只包含部分进程字段；缺失信息应到终端进程日志补查。'),
    rule('alert_network', 'Alert_ID', 'defender_alert_id', '查询告警网络证据', ['网络证据','alert network'], 'AlertEvidence','AlertId','RemoteIP RemoteUrl', '告警中远端地址是关联线索，不能替代完整网络连接记录。'),
    rule('remote_ip_alerts', 'IP', 'remote_ip', '查找 IP 关联告警', ['关联告警','related alerts'], 'AlertEvidence','RemoteIP','AlertId Title', '同一 IP 可关联不同事件；需要时间、设备和告警主题进一步筛选。'),
    rule('command_alerts', 'Process_Command', 'target_command', '查询命令关联告警证据', ['命令关联告警','command alert evidence'], 'AlertEvidence','ProcessCommandLine','FileName FolderPath ProcessCommandLine', '命令片段匹配可能误命中；核对完整命令及文件路径。'),
    rule('topic_alerts', 'Alert_Topic', 'topic', '定位主题相关告警', ['主题相关告警','find alerts','alert topic'], 'AlertInfo','Title','AlertId Title Category AttackTechniques', '标题关键词只用于发现候选；不能由关键词断定攻击技术已发生。'),
    rule('topic_security_entities', 'Alert_Topic', 'topic', '查询安全告警实体详情', ['安全告警实体','security alert entities'], 'SecurityAlert','AlertName','Entities ExtendedProperties SystemAlertId', 'Entities 与 ExtendedProperties 需按实际结构解析；SystemAlertId 不假定等于 AlertId。'),
    rule('entity_security_alerts', 'Entity_Text', 'text_fragment', '搜索包含实体的安全告警', ['包含实体的安全告警','entity in security alert'], 'SecurityAlert','Entities','Entities AlertName', '文本包含匹配不是结构化实体等值匹配；必须解析返回实体再核对。'),
    rule('alert_details', 'Alert_ID', 'defender_alert_id', '查询告警主题和类别', ['主题和类别','alert title','alert category'], 'AlertInfo','AlertId','Title Category AttackTechniques', '告警分类和技术标签是检测结论；仍需日志证据确认实际行为。'),
    rule('ip_signins', 'IP', 'login_ip', '查询 IP 登录账户和应用', ['登录账户','认证时间','登录应用','sign in','signin','authentication','login'], 'SigninLogs','IPAddress','UserPrincipalName AppDisplayName CreatedDateTime', 'CreatedDateTime 是认证时间，TimeGenerated 是日志时间；不得互换。'),
    rule('account_signins', 'Account_UPN', 'upn', '查询账户登录来源和风险', ['登录来源','登录风险','login source','signin risk'], 'SigninLogs','UserPrincipalName','IPAddress TimeGenerated RiskLevel', '此片段返回日志时间，不将它声称为精确认证发生时间。'),
    rule('upn_sid', 'Account_UPN', 'upn', '查询账户 SID', ['账户SID','账户 SID','account sid'], 'IdentityLogonEvents','AccountUpn','AccountSid', 'UPN 与 SID 是不同标识；保留多条映射，不假定全局一对一。'),
    rule('device_aad', 'Device_ID', 'defender_device_id', '查询 AAD 设备标识', ['AAD设备','AAD 设备','aad device'], 'DeviceInfo','DeviceId','AadDeviceId', 'Defender 设备 ID 与 AAD 设备 ID 不可直接互换。'),
    rule('aad_device', 'Device_ID', 'aad_device_id', '查询 Defender 设备标识', ['Defender设备','Defender 设备','defender device'], 'DeviceInfo','AadDeviceId','DeviceId', '反向映射可能有历史记录，保留候选并确认设备身份。'),
    rule('hostname_device_ids', 'Device_Name', 'device_name', '查询主机设备标识', ['设备标识','device identifiers'], 'DeviceInfo','DeviceName','DeviceId AadDeviceId', '短主机名可能不等于完整域名；精确匹配为空时回退并核对名称形式。'),
    rule('ip_risk_events', 'IP', 'risk_ip', '查询 IP 风险事件', ['风险事件','risk events'], 'AADUserRiskEvents','IpAddress','RiskEventType RiskLevel', '风险检测关联 IP 不等于已经确认的登录来源。'),
    rule('user_id_risk_ips', 'Account_ID', 'entra_user_id', '查询用户风险 IP 和位置', ['风险IP','风险 IP','risk ip','risk location'], 'AADUserRiskEvents','UserId','IpAddress Location ActivityDateTime', '风险位置来自日志，不据此推断攻击者的真实物理位置。'),
    rule('display_risky_user', 'Account_Name', 'display_name', '定位风险用户账户', ['风险用户账户','risky user account'], 'AADRiskyUsers','UserDisplayName','UserPrincipalName', '显示名可重复；结果仅用于寻找候选 UPN。'),
    rule('office_operation_ips', 'Operation', 'office_operation', '查询 Office 操作来源', ['Office操作来源','Office 操作来源','office operation source','inbox rule'], 'OfficeActivity','Operation','ClientIP Operation', '操作名称模式匹配可能涉及多种动作；ClientIP 可含端口，保留原始值。'),
]
