---
name: identity-signin-investigation
description: Investigate identity alerts through scoped accounts, sign-ins, risk events, directory actions, applications, devices, and source IPs.
memory_type: procedural
status: active
---

# Identity and Sign-in Investigation

## Use when

Use after alert evidence exposes an account, UPN, SID, sign-in IP, or identity-risk signal.

## Procedure

1. Establish account identifiers, tenant, source IP, and incident time range.
2. Inspect interactive and non-interactive sign-ins and their outcomes.
3. Inspect risk events, directory actions, audit operations, and cloud activity where relevant.
4. Correlate account, IP, device, application, tenant, and time.
5. Reconnect the attributed identity activity to the initial alert.

## Semantic requirements

- Prefer stable account object IDs, SIDs, and normalized UPNs over display names.
- Treat shared IP addresses as scoped evidence rather than identity proof.
- Resolve account and sign-in fields from the current catalog.

## Episodic retrieval

Retrieve examples that use the Semantic account fields and identity tables for
the active phase. Include a repair example when an account-field guess failed.

## Completion criteria

- The account is identified with a stable identifier where possible.
- Sign-ins and actions are time and tenant scoped.
- Shared names or IPs are not used as sole attribution evidence.
- The identity evidence reconnects to the alert.
