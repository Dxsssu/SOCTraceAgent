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
summary: Traceback workflow for web intrusions that start from external scanning and eventually lead to website defacement.
---

# Workflow

1. First use `get_ip_report` to query threat intelligence for the alert's external source IP, validating its reputation, historical malicious detections, infrastructure background, and whether it behaves like a scanner or attack source.
2. Then query broad Web/HTTP traffic related to the target site, identify external source IPs with unusually high request volume, and use headers, user-agent strings, and access patterns to recognize scanners, probing tools, or the site's likely CMS/application framework.
3. If the activity moves from scanning into admin paths or authentication interfaces, continue by querying HTTP POST traffic, form submissions, admin logins, and brute-force traces against the target web server to confirm whether admin credentials were cracked or a management entry point was exploited.
4. After confirming backend exploitation, continue querying uploads, file writes, dropped executables, script execution, and Sysmon process traces to determine whether the attacker successfully implanted a malicious payload via the web service, and extract filenames, hashes, or command-line fingerprints.
5. Then pivot around the defaced page, external images, scripts, download URLs, dynamic DNS, non-standard ports, or staging infrastructure to confirm the source of the defacement content and the attacker-controlled external resources.
6. Finally, query outbound connections, DNS, HTTP, proxy, PowerShell, scheduled-task, service-creation, and repeated-access behavior on the affected web server before and after defacement to assess whether follow-on download, persistence, or further spread occurred.
