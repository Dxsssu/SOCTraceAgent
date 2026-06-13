---
title: Lateral Movement Triage Workflow
event_types:
  - lateral_movement
  - suspicious_internal_access
tags:
  - east_west
  - privilege
  - host
summary: Initial triage workflow for suspicious host-to-host access, credential reuse, and lateral movement indicators.
---

# Applicable Scenarios

Use this workflow when the alert involves unusual internal host-to-host connections, remote execution, administrator account reuse, privileged authentication, or expanding cross-host access.

# Investigation Workflow

1. First confirm whether the lateral access actually happened, and identify the source host, destination host, and involved account.
2. Then determine which protocol, authentication method, and execution path were used.
3. Finally, inspect whether the destination host shows follow-up execution, persistence, data access, or continued lateral spread.

# Suggested L1 Goals

- You can split the tree into multiple L1s, such as "Confirm the lateral path is real", "Assess account and privilege risk", and "Inspect follow-on execution and spread on the target host".

# Candidate L2 Questions

- Was there a successful authentication or remote execution from the source host to the destination host?
- Does the involved account show elevated privileges, unusual login patterns, or signs of credential reuse?
- Did the destination host produce new suspicious processes, services, scheduled tasks, or outbound connections?
- Did the activity spread to more hosts, accounts, or protocol paths?

# Candidate L3 Actions

- Query authentication, SMB, RDP, WinRM, or other remote-access logs between the source host and destination host named in the alert.
- Query authentication history, privilege level, and cross-host use of the involved account within the alert time window.
- Query process creation, service installation, scheduled task, registry, or network connection logs on the alert's destination host.
- Query whether the destination host subsequently accessed other hosts.
- Query security detection logs related to the lateral activity to confirm blocking, alerts, or supporting evidence.

# Evidence Sources / Tool Hints

- Windows logon logs, Sysmon, and SMB/RDP/network-flow logs are useful for validating the lateral path.
- If the current environment mainly relies on Splunk log queries, L3 nodes should state the entities, protocols, and host scope as explicitly as possible.
- If you need to distinguish follow-up activity on the source host versus the destination host, split the work into multiple granular L3 nodes instead of one vague query.

# Convergence and Next-Step Guidance

- If successful lateral authentication or remote execution is confirmed, prioritize the destination host and any continued spread paths.
- If there are only connection traces but no authentication or execution evidence, first determine whether this was legitimate administration or a false positive.
- If the destination host lacks follow-up anomaly evidence, confirm data coverage before widening the investigation.
