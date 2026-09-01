---
name: network-activity-investigation
description: Investigate network indicators through direction, device, process, firewall, DNS, bastion, threat-intelligence, and timeline evidence.
memory_type: procedural
status: active
---

# Network Activity Investigation

## Use when

Use after alert evidence exposes an IP, URL, domain, port, protocol, or connection.

## Procedure

1. Establish the indicator, local/remote direction, protocol, port, and time range.
2. Identify the responsible device and process when endpoint evidence exists.
3. Correlate endpoint observations with relevant firewall, DNS, bastion, or threat-intelligence evidence.
4. Build a cross-source network timeline.
5. Reconnect the indicator, direction, device, and process to the initial alert.

## Semantic requirements

- Treat IP addresses as time-window scoped and distinguish local from remote fields.
- Require device and process context before attributing an endpoint connection.
- Resolve current network fields and permitted join keys from Semantic Memory.

## Episodic retrieval

Retrieve examples for the selected endpoint-network or firewall tables and the
current indicator type. Do not reuse concrete indicators from prior episodes.

## Completion criteria

- Traffic direction and indicator meaning are explicit.
- Endpoint attribution is device-and-time scoped.
- Network-control evidence refers to the same indicator and window.
- The network chain reconnects to the initial alert.
