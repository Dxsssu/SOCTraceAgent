# 训练集调查片段目录

状态按 SQL 模板的训练支持判定；active 片段仅开放其中的 active 模板。candidate 默认不进入快通道。

| # | ID | 起点类型 / kind | 调查意图 | 表 | 状态 | 模板数 | 支持题目 / 事件 |
|---|---|---|---|---|---|---:|---:|
| 1 | `proc_mail_sender` | Email_ID / network_message_id | 查询邮件发件人 | EmailEvents | active | 2 | 3 / 2 |
| 2 | `proc_mail_recipient` | Email_ID / network_message_id | 查询邮件收件人 | EmailEvents | candidate | 2 | 2 / 2 |
| 3 | `proc_mail_sender_ip` | Email_ID / network_message_id | 查询邮件发送 IP | EmailEvents | active | 2 | 3 / 2 |
| 4 | `proc_header_mail_sender` | Email_ID / internet_message_id | 查询邮件发件人 | EmailEvents | candidate | 1 | 1 / 1 |
| 5 | `proc_header_to_network_id` | Email_ID / internet_message_id | 转换为平台邮件标识 | EmailPostDeliveryEvents | candidate | 2 | 5 / 2 |
| 6 | `proc_mail_urls` | Email_ID / network_message_id | 查询邮件内链接 | EmailUrlInfo | active | 2 | 12 / 4 |
| 7 | `proc_url_mail_ids` | URL / url | 定位包含链接的邮件 | EmailUrlInfo | active | 2 | 5 / 2 |
| 8 | `proc_sender_mails` | Email_Sender / header_from | 查找发件人的邮件 | EmailEvents | candidate | 1 | 1 / 1 |
| 9 | `proc_recipient_mails` | Email_Recipient / mailbox_address | 查找收件人的邮件 | EmailEvents | candidate | 1 | 1 / 1 |
| 10 | `proc_mail_postdelivery` | Email_ID / network_message_id | 查询邮件投递后处置 | EmailPostDeliveryEvents | candidate | 1 | 1 / 1 |
| 11 | `proc_url_clickers` | URL / url | 查询链接点击账户 | UrlClickEvents | active | 2 | 7 / 3 |
| 12 | `proc_account_clicks` | Account_UPN / upn | 查询账户点击链接 | UrlClickEvents | candidate | 2 | 1 / 1 |
| 13 | `proc_ip_clicks` | IP / click_ip | 查询 IP 对应点击事件 | UrlClickEvents | candidate | 1 | 1 / 1 |
| 14 | `proc_filename_commands` | File_Name / file_name | 查询程序命令行 | DeviceProcessEvents | active | 2 | 5 / 3 |
| 15 | `proc_filename_parents` | File_Name / file_name | 查询程序父进程 | DeviceProcessEvents | active | 2 | 5 / 3 |
| 16 | `proc_command_processes` | Process_Command / target_command | 定位执行命令的进程 | DeviceProcessEvents | active | 1 | 2 / 2 |
| 17 | `proc_pid_process_context` | Process_ID / target_pid | 查询进程上下文 | DeviceProcessEvents | candidate | 1 | 1 / 1 |
| 18 | `proc_filename_origins` | File_Name / file_name | 查询文件下载来源 | DeviceFileEvents | candidate | 2 | 2 / 2 |
| 19 | `proc_remote_ip_processes` | IP / remote_ip | 查询远端连接发起进程 | DeviceNetworkEvents | candidate | 1 | 1 / 1 |
| 20 | `proc_remote_url_network` | URL / remote_url | 查询远端地址网络连接 | DeviceNetworkEvents | candidate | 1 | 1 / 1 |
| 21 | `proc_alert_entities` | Alert_ID / defender_alert_id | 查询告警关联实体 | AlertEvidence | active | 3 | 46 / 8 |
| 22 | `proc_alert_accounts` | Alert_ID / defender_alert_id | 查询告警账户证据 | AlertEvidence | active | 3 | 46 / 8 |
| 23 | `proc_alert_processes` | Alert_ID / defender_alert_id | 查询告警进程证据 | AlertEvidence | active | 3 | 46 / 8 |
| 24 | `proc_alert_network` | Alert_ID / defender_alert_id | 查询告警网络证据 | AlertEvidence | active | 3 | 46 / 8 |
| 25 | `proc_remote_ip_alerts` | IP / remote_ip | 查找 IP 关联告警 | AlertEvidence | active | 2 | 3 / 3 |
| 26 | `proc_command_alerts` | Process_Command / target_command | 查询命令关联告警证据 | AlertEvidence | candidate | 1 | 1 / 1 |
| 27 | `proc_topic_alerts` | Alert_Topic / topic | 定位主题相关告警 | AlertInfo | active | 1 | 5 / 3 |
| 28 | `proc_topic_security_entities` | Alert_Topic / topic | 查询安全告警实体详情 | SecurityAlert | active | 1 | 2 / 2 |
| 29 | `proc_entity_security_alerts` | Entity_Text / text_fragment | 搜索包含实体的安全告警 | SecurityAlert | candidate | 1 | 1 / 1 |
| 30 | `proc_alert_details` | Alert_ID / defender_alert_id | 查询告警主题和类别 | AlertInfo | candidate | 1 | 1 / 1 |
| 31 | `proc_ip_signins` | IP / login_ip | 查询 IP 登录账户和应用 | SigninLogs | active | 1 | 6 / 2 |
| 32 | `proc_account_signins` | Account_UPN / upn | 查询账户登录来源和风险 | SigninLogs | candidate | 1 | 1 / 1 |
| 33 | `proc_upn_sid` | Account_UPN / upn | 查询账户 SID | IdentityLogonEvents | candidate | 1 | 1 / 1 |
| 34 | `proc_device_aad` | Device_ID / defender_device_id | 查询 AAD 设备标识 | DeviceInfo | candidate | 2 | 2 / 1 |
| 35 | `proc_aad_device` | Device_ID / aad_device_id | 查询 Defender 设备标识 | DeviceInfo | candidate | 1 | 1 / 1 |
| 36 | `proc_hostname_device_ids` | Device_Name / device_name | 查询主机设备标识 | DeviceInfo | candidate | 1 | 1 / 1 |
| 37 | `proc_ip_risk_events` | IP / risk_ip | 查询 IP 风险事件 | AADUserRiskEvents | candidate | 1 | 1 / 1 |
| 38 | `proc_user_id_risk_ips` | Account_ID / entra_user_id | 查询用户风险 IP 和位置 | AADUserRiskEvents | candidate | 1 | 1 / 1 |
| 39 | `proc_display_risky_user` | Account_Name / display_name | 定位风险用户账户 | AADRiskyUsers | candidate | 1 | 1 / 1 |
| 40 | `proc_office_operation_ips` | Operation / office_operation | 查询 Office 操作来源 | OfficeActivity | candidate | 1 | 1 / 1 |
