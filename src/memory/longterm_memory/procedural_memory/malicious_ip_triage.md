---
title: Malicious IP Triage Workflow
event_types:
  - malicious_ip
  - suspicious_network_activity
tags:
  - ip
  - threat_intelligence
  - network
summary: Initial triage workflow for alerts centered on a suspicious IP, scan source, or threat-intelligence hit.
---

# Applicable Scenarios

Suitable for incidents where the source IP is suspicious, a threat-intelligence match fired, scanning behavior is observed, outbound access looks abnormal, or perimeter devices discovered suspicious traffic.

# Investigation Workflow

1. First determine whether the IP truly has a malicious background or an anomalous profile.
2. Then confirm which internal assets, services, and time windows it interacted with.
3. Finally, determine whether successful access, follow-on host anomalies, or broader impact has already occurred.

# Suggested L1 Goals

- You can split the tree into multiple L1s, such as "Assess external IP risk", "Confirm interaction scope with internal assets", and "Inspect follow-on anomalies on the target asset".

# Candidate L2 Questions

- Does the external IP have clear malicious tags, abnormal reputation, or attack history?
- Which internal assets, ports, and protocols did the IP touch within the current time window?
- Is there evidence of successful access, escalated alerts, or follow-on host anomalies tied to this IP?
- Does the activity look more like scanning, probing, exploitation attempts, or legitimate business traffic?

# Candidate L3 Actions

- Query basic intelligence, geolocation, ASN, and ownership details for the external IP in the alert.
- Query external threat-intelligence, reputation tags, and malicious-detection results for the external IP in the alert.
- Query network access logs related to the external IP in the alert to identify target assets, ports, protocols, and time distribution.
- Query perimeter, IDS/IPS, or firewall logs to confirm blocking, alerts, or attack signatures.
- Query host, authentication, or application logs for the accessed asset to determine whether follow-on anomalies occurred.

# Evidence Sources / Tool Hints

- Network-flow logs, firewall logs, and security detections in Splunk are useful for validating access scope and behavior patterns.
- Basic IP intelligence and threat-intelligence tools are useful for enriching the external profile.
- If you need to determine whether the activity reached the host layer, split out separate L3 actions for the target host.

# Convergence and Next-Step Guidance

- If the IP has high-risk reputation and real access to internal assets is confirmed, expand first into the target host and follow-on behavior.
- If there is only a single low-value access and no malicious-intelligence support, first validate whether it was noise or a false positive.
- If the access outcome cannot yet be confirmed, fill in the time window, target asset, and network evidence before deciding whether to branch out further.
