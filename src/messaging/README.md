# Messaging Design

## Goal

For the current three-role SOCAgent architecture (`Planner / Executor / Reviewer`), communication should be divided into two layers:

1. The business coordination layer
2. The observable message layer

The first implementation should prioritize simplicity, stability, and traceability for the business coordination path.

## Recommended approach

`Planner`, `Executor`, and `Reviewer` should not coordinate by directly consuming each other's free-form chat messages.

Use this model instead:

1. Shared persistent state as the primary coordination mechanism
2. Structured messages as an auxiliary auditing and observability mechanism

Concretely:

- `Planner` reads `Event` and the latest `RoundReview`, then writes a new `TTT` snapshot
- `Executor` reads the latest `TTT`, claims an executable leaf node, runs a tool, and writes an `Execution`
- `Reviewer` reads current-round `Execution` results, summarizes them, and writes a `RoundReview`

The real business handoff chain is:

```text
Event -> TTT -> Execution -> RoundReview -> TTT
```

## Why this fits better than directly copying DeepSOC

The real collaboration mechanism in `deepsoc` is also database-state driven. RabbitMQ is mainly used for front-end notification.

For the simplified three-role architecture here:

- We do not need intermediate layers such as `Task -> Action -> Command`
- The first version does not need RabbitMQ
- We do need explicit, persistent state transitions

This aligns better with the traceback workflow in this repository and is easier to implement and maintain.

## Scope of `src/messaging`

`src/messaging` only defines message contracts and event types. It does not implement the workflow itself.

Recommended contents:

1. `message_types.py`
   Defines internal message or event types, such as:
   - `TTT_INITIALIZED`
   - `LEAF_CLAIMED`
   - `EXECUTION_COMPLETED`
   - `ROUND_REVIEW_CREATED`
   - `TTT_UPDATED`

2. `models.py`
   Defines lightweight message-envelope structures for auditing and tracing, such as:
   - `event_id`
   - `round_id`
   - `from_role`
   - `to_role`
   - `message_type`
   - `payload`
   - `created_at`

3. `bus.py`
   Defines a minimal message-bus interface, such as:
   - `publish(message)`
   - `list_messages(event_id, round_id=None)`

The first version of `bus.py` should prioritize persistence-backed behavior rather than queue-driven behavior.

## Business communication rules

Recommended rules:

1. `Planner` does not issue execution instructions directly to `Reviewer`
2. `Reviewer` does not rewrite the `TTT` directly
3. `Executor` is not responsible for replanning
4. Every cross-agent handoff must correspond to a persistent state object

This means:

- `Planner -> Executor`: handoff through `TTT` leaf nodes
- `Executor -> Reviewer`: handoff through `Execution`
- `Reviewer -> Planner`: handoff through `RoundReview`

## Observable-message rules

Structured messages are still valuable, but they should only be used for:

- auditing
- debugging
- replay
- future front-end presentation

For example:

- `Planner` publishes `TTT_INITIALIZED`
- `Executor` publishes `LEAF_CLAIMED` and `EXECUTION_COMPLETED`
- `Reviewer` publishes `ROUND_REVIEW_CREATED`

These messages must not become the only source of truth. The source of truth should always be the shared persistent state.

## First-phase implementation advice

In the first phase, `src/messaging` should only do three things:

1. Define message-type enums
2. Define lightweight message-envelope structures
3. Define a minimal message-bus interface

Do not introduce RabbitMQ at this stage.

Only consider adding a message queue later if you truly need:

- streaming output to external consumers
- real-time front-end push at a larger scale
- decoupled communication across multiple machines
