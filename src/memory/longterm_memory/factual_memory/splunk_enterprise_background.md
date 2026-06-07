---
title: Splunk BOTS Enterprise Background
categories:
  - enterprise_background
  - dataset_context
tags:
  - splunk
  - bots
  - windows
  - network
summary: 当前调查环境基于 Splunk BOTS 数据集，适合围绕 Windows、网络流量、边界设备和安全检测日志进行取证与关联分析。
---

# 背景概览

当前企业调查环境以 Splunk 为统一检索入口，主要数据来源采用 BOTS 数据集。Planner 在初始化 TTT 时，应默认把 Splunk 检索能力视为核心证据来源，并优先设计可被现有日志与 MCP 工具落地执行的查询动作。

# 数据环境特点

- 默认数据集可覆盖 `botsv1`、`botsv2`、`botsv3`，其中 `botsv1` 可视为当前默认示例环境。
- 环境中同时存在主机日志、网络流日志、代理/邮件/认证/安全设备日志，适合做跨源关联。
- 主要调查方式应以“先定位相关时间窗，再围绕 IP / 主机 / 账号 / 事件类型做关联查询”为主。

# 常见日志与 sourcetype 范围

- Windows 与认证行为：`wineventlog`、`XmlWinEventLog:Microsoft-Windows-Sysmon/Operational`
- 网络与协议流量：`stream:dns`、`stream:http`、`stream:tcp`、`stream:smb`、`stream:ip`
- 边界与安全设备：`fgt_event`、`fgt_traffic`、`fgt_utm`、`suricata`
- 资产与服务行为：`iis`、`stream:ldap`、`stream:mapi`

# 典型调查对象

- 外部 IP、内部 IP、主机名、账号、认证事件、网络连接、告警签名
- 与邮件、Web、Windows 主机、边界防火墙、DNS 解析相关的上下游行为

# 设计 TTT 时的建议

- 如果事件涉及真实性、攻击源风险、目标资产影响、横向扩散等多个明显方向，应拆成多个 L1，而不是合并成一个总根节点。
- L3 查询动作应尽量明确到“查什么实体 + 什么日志/能力 + 什么目的”。
- 如果一个问题需要多源验证，优先把多个 L3 动作拆开，而不是写成模糊的大查询。
- 如果需要外部情报，应结合当前可用的 IP 情报类 MCP 工具，不要假设系统有未注册的能力。
- 如果告警只给出少量实体，先设计用于确认实体真实性、时间窗和影响范围的查询动作。

# 限制与注意事项

- 当前环境是数据集示例，不应假设存在真实 CMDB、EDR、工单系统或额外资产平台。
- 某些结论可能受限于数据覆盖、时间窗或日志质量，Planner 应允许 TTT 先验证数据是否存在。
- 若要设计 L3，优先贴近当前 MCP server/tool 能力，避免生成无法执行的动作。
