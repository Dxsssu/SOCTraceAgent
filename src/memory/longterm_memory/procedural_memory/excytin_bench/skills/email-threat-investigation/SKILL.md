---
name: email-threat-investigation
description: Investigate email alerts through message identifiers, URLs, participants, delivery state, and post-delivery remediation.
memory_type: procedural
status: active
---

# Email Threat Investigation

## Use when

Use after alert evidence exposes a URL, email address, or message identifier.

## Procedure

1. Anchor the email alert and its structured evidence.
2. Resolve the URL or address to a stable message identifier.
3. Identify sender, recipient, delivery state, and message time.
4. Inspect post-delivery actions such as removal or quarantine when relevant.
5. Validate that the message, participants, and remediation evidence refer to the same alert and time window.

## Semantic requirements

- Prefer `network_message_id` for cross-table message correlation.
- Use `internet_message_id` only where the active schema and evidence support it.
- Resolve URL, sender, recipient, and action fields from current Semantic Memory.

## Episodic retrieval

Retrieve query examples targeting email tables chosen by Semantic Memory.
Include one successful example and, when available, one invalid-field or
empty-result example with its next corrective attempt.

## Completion criteria

- The message identifier is supported by evidence.
- Sender and recipient belong to the same message.
- Delivery or remediation state is time aligned.
- The claimed email entity reconnects to the initial alert.
