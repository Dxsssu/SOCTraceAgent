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
summary: The current investigation environment is based on the Splunk BOTS datasets and is well suited for forensics and correlation across Windows, network, perimeter-device, and security-detection logs.
---

# Background Overview

The current enterprise investigation environment uses Splunk as the unified search entry point, with the BOTS datasets as the primary data source. When initializing the TTT, the Planner should treat Splunk search as the core evidence source and prioritize query actions that can be executed with the currently available logs and MCP tools.

# Data Environment Characteristics

- The default dataset coverage includes `botsv1`, `botsv2`, and `botsv3`, with `botsv1` serving as the current default example environment.
- The environment includes host logs, network-flow logs, proxy/mail/authentication/security-device logs, making it suitable for cross-source correlation.
- The primary investigation pattern should be: identify the relevant time window first, then correlate around IPs, hosts, accounts, and event types.

# Common Logs and Sourcetype Coverage

- Windows and authentication activity: `wineventlog`, `XmlWinEventLog:Microsoft-Windows-Sysmon/Operational`
- Network and protocol traffic: `stream:dns`, `stream:http`, `stream:tcp`, `stream:smb`, `stream:ip`
- Perimeter and security-device logs: `fgt_event`, `fgt_traffic`, `fgt_utm`, `suricata`
- Asset and service behavior: `iis`, `stream:ldap`, `stream:mapi`

# Typical Investigation Entities

- External IPs, internal IPs, hostnames, accounts, authentication events, network connections, and alert signatures
- Upstream and downstream behavior involving mail, web systems, Windows hosts, perimeter firewalls, and DNS resolution

# TTT Design Guidance

- If the event clearly involves multiple directions such as authenticity, source-IP risk, target impact, or lateral spread, split them into multiple L1 nodes instead of merging them into one generic root.
- L3 actions should state as explicitly as possible: which entity to inspect, which log/capability to use, and for what purpose.
- If a question requires multi-source validation, prefer multiple L3 actions instead of one vague large query.
- If external intelligence is needed, use the currently available IP-intelligence MCP tools and do not assume the system has unregistered capabilities.
- If the alert provides only a few entities, first design actions that validate entity authenticity, time window, and impact scope.

# Limits and Notes

- The current environment is a dataset-based example; do not assume the presence of a real CMDB, EDR, ticketing system, or extra asset platform.
- Some conclusions may be limited by data coverage, time windows, or log quality, so the Planner should allow the TTT to validate data existence first.
- When designing L3 nodes, stay close to the currently available MCP servers/tools to avoid generating actions that cannot be executed.
