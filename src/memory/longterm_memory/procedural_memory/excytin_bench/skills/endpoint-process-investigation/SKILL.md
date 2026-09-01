---
name: endpoint-process-investigation
description: Investigate endpoint alerts through device-scoped processes, commands, files, network activity, registry activity, and timeline evidence.
memory_type: procedural
status: active
---

# Endpoint Process Investigation

## Use when

Use after alert evidence exposes a device, process, command line, file, or hash.

## Procedure

1. Establish the device identity and incident time range.
2. Locate the process, command line, account, and parent execution context.
3. Expand only relevant branches into file, network, registry, or logon evidence.
4. Build a coherent parent/child and downstream-activity timeline.
5. Reconnect the attributed device and execution chain to the alert evidence.

## Semantic requirements

- Never correlate on ProcessId alone; require device and time scope.
- Use stable file hashes where available and apply their normalization rules.
- Resolve process, initiating-process, device, and time fields from Semantic Memory.

## Episodic retrieval

Retrieve examples for the selected endpoint tables and entity types. Prefer
examples with returned rows, but include relevant failed-field repairs so the
Executor does not repeat schema guesses.

## Completion criteria

- The process is device-and-time scoped.
- Parent, child, file, and network events form a coherent timeline.
- Shared names or PIDs are not used as sole attribution evidence.
- The execution chain reconnects to the initial alert.
