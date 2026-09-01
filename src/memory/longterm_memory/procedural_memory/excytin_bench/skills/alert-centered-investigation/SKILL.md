---
name: alert-centered-investigation
description: Plan an alert-led investigation from initial alert through scoped entities, domain evidence, timeline, and conclusion validation.
memory_type: procedural
status: active
---

# Alert-Centered Investigation

## Use when

Use as the default workflow for an investigation that starts from a security
alert, incident, or alert-linked entity. Select a domain skill after structured
alert evidence reveals email, endpoint, identity, or network entities.

## Procedure

1. Ground the environment. Ask Semantic Memory for the current catalog and do not assume fields from prior episodes still exist.
2. Locate the initial alert and establish its time range.
3. Extract structured entities from alert evidence before searching broad raw-log tables.
4. Select the domain branch from supported entity types, not from superficial task keywords.
5. Correlate events only through Semantic join keys and respect each key's scope.
6. Build a bounded timeline around the alert.
7. Validate that the final claim reconnects to the initial alert through entities and time.

## Semantic requirements

- Prefer alert identifiers for the first pivot.
- Treat `process_id` as device-and-time scoped.
- Treat IP addresses as time-window scoped.
- Resolve current table and field names from Semantic Memory.

## Episodic retrieval

For the active phase, retrieve successful and failed attempts that target the
Semantic tables selected for that phase. Use examples as implementation hints,
never as authoritative schema or as a source of final answers.

## Completion criteria

- Every important entity has an evidence source.
- Every cross-log pivot uses a valid scoped key.
- Empty results are not treated as proof of absence.
- The final conclusion has an explicit evidence chain back to the alert.
