---
title: Suspicious Login Investigation Workflow
event_types:
  - suspicious_login
  - account_compromise
tags:
  - identity
  - authentication
  - login
summary: Initial traceback workflow for suspicious login, brute-force, and account takeover alerts.
---

# Applicable Scenarios

Use this workflow when the event description mentions abnormal logins, impossible-travel logins, brute-force attempts, suspicious mail-gateway authentication, or signs of possible account compromise.

# Investigation Workflow

1. First confirm whether the login activity in the alert actually happened and whether it includes successful authentication.
2. Then determine whether the source IP, account, and target system show clear risk or anomalous characteristics.
3. Finally, widen the scope to confirm whether follow-on access, privilege abuse, or lateral activity occurred.

# Suggested L1 Goals

- You can split the tree into multiple L1s, such as "Confirm login authenticity", "Assess source IP risk", and "Assess account and target-system impact".

# Candidate L2 Questions

- Did the account actually perform a successful login within the alert time window?
- Does the source IP have malicious tags, unusual geolocation, or a history of high-risk activity?
- Is there follow-on access or sensitive activity on the target system by the same account?
- Are there bulk failed-login patterns involving the same source IP, the same account, or adjacent time windows?

# Candidate L3 Actions

- Query successful and failed authentication logs between the alert source IP and the target mail gateway within the alert time window to confirm whether a real login occurred.
- Query logs from the target mail gateway, VPN, or SSO entry point to confirm the authentication channel and target system.
- Query the alert source IP's basic intelligence, geolocation, ASN, and organization.
- Query threat-intelligence tags, reputation, and historical malicious records for the alert source IP.
- Query follow-on access, host logins, or sensitive-resource activity for the relevant account within the same time window.
- Query whether the same source IP or the same account triggered bulk failed logins, password spraying, or lateral-login indicators.

# Evidence Sources / Tool Hints

- Authentication logs, mail-gateway logs, and VPN/SSO logs are useful for validating login authenticity and time windows.
- Basic IP intelligence and external threat-intelligence tools are useful for judging the source IP's risk background.
- If the current system mainly relies on Splunk, prefer L3 nodes that can be executed directly as concrete log-search actions.

# Convergence and Next-Step Guidance

- If a successful login is confirmed and the source IP is high risk, prioritize the affected account, host, and follow-on access for expansion.
- If there are only failed logins and no success evidence, first narrow to brute-force/password-spraying validation before deciding whether to expand into asset impact.
- If the data is insufficient, fill in the time window, authentication entry point, and target-system context before planning further TTT work.
