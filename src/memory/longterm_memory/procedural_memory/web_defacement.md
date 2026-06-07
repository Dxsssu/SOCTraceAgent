---
title: Web Defacement Investigation Workflow
event_types:
  - web_defacement
  - web_compromise
  - apt_intrusion
tags:
  - web
  - defacement
  - ip
  - splunk
summary: 适用于从外部扫描源切入、最终导致网站被篡改的 Web 入侵与攻击溯源流程。
---

# Workflow

1. 先使用 `get_ip_report` 查询告警中的外部源 IP 威胁情报，确认其信誉、历史恶意检测、基础设施背景，以及是否具备扫描源或攻击源特征。
2. 然后查询目标站点相关的全局 Web/HTTP 流量，统计请求量异常高的外部源 IP，并结合请求头、User-Agent 和访问特征识别扫描器、探测工具或目标站点所使用的 CMS/应用框架。
3. 如果发现扫描后转入后台入口或认证接口，继续查询针对目标 Web 服务器的 HTTP POST、表单提交、后台登录和爆破痕迹，确认是否存在后台凭据破解或管理入口被利用。
4. 在确认后台利用后，继续查询上传、写文件、可执行文件落地、脚本执行和 Sysmon 进程痕迹，识别攻击者是否通过 Web 服务成功植入恶意载荷，并提取文件名、哈希或命令行指纹。
5. 然后围绕被篡改页面、外部图片、脚本、下载地址、动态域名、非标准端口或 staging 基础设施做关联查询，确认篡改内容的来源以及攻击者控制的外部资源。
6. 最后查询受影响 Web 服务器在篡改前后的外连、DNS、HTTP、代理、PowerShell、计划任务、服务创建和持续访问行为，评估是否存在后续下载、持久化或进一步扩散。
