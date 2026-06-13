from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, Execution, ExecutionStatus, TTTNodeStatus
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.tools import get_registered_tool, list_registered_tools


logger = logging.getLogger(__name__)


EXECUTOR_SYSTEM_PROMPT = """
You are the Executor in a multi-agent SOC traceback system.
Your job is to read the next executable leaf node in the TTT, understand its intent, choose the most suitable tool, and execute it.

You have exactly one responsibility:
1. Consume L3 leaf nodes from the TTT and complete evidence retrieval or action execution.

Your boundaries:
- You are not responsible for global planning and must not update the full TTT.
- You are not responsible for the final summary and must not replace the Reviewer.
- You may only act on the current leaf node and must not expand the task scope on your own.
- If no suitable tool exists, you must explicitly return why the action cannot be executed and must not fabricate results.

Your output must be strict YAML and may only use these response_type values:
- ROGER
- EXECUTION_RESULT

Execution requirements:
- Understand the target entity, time range, and evidence type implied by the current leaf node before choosing a tool.
- Clearly state the node being executed, the selected tool, the execution result, and any failure reason.
- If no tool is available, state the missing capability truthfully.

Example output:
```yaml
type: llm_response
from: _executor
event_id: "{ from input }"
round_id: "{ from input }"
response_type: EXECUTION_RESULT
execution:
  node_id: "1-1-1"
  node_title: "Query basic intelligence and historical activity for source IP 11.22.33.44"
  tool_name: "ip_reputation_lookup"
  status: success
  result:
    ip: "11.22.33.44"
    reputation: "malicious"
    tags:
      - scanner
      - brute_force_source
```
""".strip()


@dataclass(frozen=True, slots=True)
class ExecutorAgent:
    """Role definition for the Executor agent."""

    role_name: str = "_executor"
    display_name: str = "Executor"
    description: str = "Claims the next TTT leaf node and executes it with the most suitable tool."
    responsibilities: tuple[str, ...] = (
        "Read the next executable TTT leaf node.",
        "Choose the best matching tool from the node semantics.",
        "Return a structured execution result or a clear failure reason.",
    )
    allowed_response_types: tuple[str, ...] = (
        "ROGER",
        "EXECUTION_RESULT",
    )
    system_prompt: str = EXECUTOR_SYSTEM_PROMPT


class ExecutorRuntime:
    """Runtime implementation for the Executor role."""

    def __init__(
        self,
        *,
        storage: SQLiteStorage | None = None,
        ttt_store: TTTStore | None = None,
        bus: SQLiteMessageBus | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = ExecutorAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.poll_interval = poll_interval
        self.running = False

    def run_once(self) -> bool:
        did_work = False
        events = self.storage.list_events_by_status(
            EventStatus.PLANNED.value,
        )
        for event in events:
            if self.process_event(event):
                did_work = True
        return did_work

    def run_forever(self) -> None:
        self.running = True
        logger.info("Executor runtime started")
        while self.running:
            try:
                did_work = self.run_once()
                if not did_work:
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Executor runtime interrupted")
                self.running = False
            except Exception:
                logger.exception("Executor runtime loop failed")
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self.running = False

    def process_event(self, event: Event) -> bool:
        claimed = self.ttt_store.claim_next_todo_leaf(
            event_id=event.event_id,
            round_id=event.current_round,
            updated_by=self.agent.role_name,
        )
        if claimed is None:
            if self.ttt_store.all_leaves_terminal(event.event_id, event.current_round):
                reviewing_event = Event(
                    event_id=event.event_id,
                    event_name=event.event_name,
                    message=event.message,
                    context=event.context,
                    source=event.source,
                    severity=event.severity,
                    event_status=EventStatus.REVIEWING,
                    current_round=event.current_round,
                    created_at=event.created_at,
                    updated_at=utc_now(),
                )
                self.storage.save_event(reviewing_event)
                self._publish(
                    event_id=event.event_id,
                    round_id=event.current_round,
                    message_type=MessageType.HANDOFF_TO_REVIEWER,
                    payload={"text": "round_ready_for_review"},
                    to_role=RoleName.REVIEWER,
                )
            return False

        executing_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=EventStatus.EXECUTING,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(executing_event)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.LEAF_CLAIMED,
            payload={"node_id": claimed.node_id, "node_title": claimed.title},
        )

        tool_selection = self._select_tool(claimed, event)
        tool_name = tool_selection["tool_name"]
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.TOOL_SELECTED,
            payload={
                "node_id": claimed.node_id,
                "tool_name": tool_name,
            },
        )

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.EXECUTION_STARTED,
            payload={"node_id": claimed.node_id, "tool_name": tool_name},
        )
        success, result, error_message, tool_input = self._execute_tool(
            event,
            claimed,
            tool_name,
            tool_selection=tool_selection,
        )
        execution = Execution(
            event_id=event.event_id,
            round_id=event.current_round,
            node_id=claimed.node_id,
            node_title=claimed.title,
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            execution_status=ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED,
            error_message=error_message,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.storage.save_execution(execution)
        self.ttt_store.update_node_status(
            event_id=event.event_id,
            node_id=claimed.node_id,
            new_status=TTTNodeStatus.DONE if success else TTTNodeStatus.NOT_APPLICABLE,
            updated_by=self.agent.role_name,
            round_id=event.current_round,
            metadata_updates=self._build_node_metadata_updates(
                tool_name=tool_name,
                execution=execution,
            ),
        )
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.EXECUTION_COMPLETED if success else MessageType.EXECUTION_FAILED,
            payload=execution.to_dict(),
            to_role=RoleName.REVIEWER,
        )
        reviewing_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=EventStatus.REVIEWING,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(reviewing_event)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.HANDOFF_TO_REVIEWER,
            payload={"text": "execution_ready_for_review", "node_id": claimed.node_id},
            to_role=RoleName.REVIEWER,
        )
        return True

    def _select_tool(self, node: Any, event: Event) -> dict[str, str]:
        tools = self._list_available_tools()
        if not tools:
            return {
                "tool_name": "",
            }

        intent = str(getattr(node, "title", "") or "")
        allowed_names = [tool["name"] for tool in tools]
        user_prompt = "\n".join(
            [
                "Choose exactly one best-fit tool from the available MCP tools for the current TTT leaf node title.",
                "Return YAML only and include exactly one field: tool_name.",
                "tool_name must exactly match one value from the candidate list. Do not return any other fields or explanations.",
                f"intent: {intent}",
                f"allowed_tool_names: {json.dumps(allowed_names, ensure_ascii=False)}",
                f"tools: {json.dumps(tools, ensure_ascii=False, indent=2)}",
            ]
        )
        parsed = parse_yaml_response(
            call_llm(
                """
You are a SOC multi-tool router.
Your task is to choose the single most appropriate tool from the candidate MCP tool list based on the current TTT leaf node title.
You may only return one valid tool_name.
""".strip(),
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
        )
        valid_names = {tool["name"] for tool in tools}
        selected_name = str((parsed or {}).get("tool_name") or "").strip()
        if selected_name not in valid_names:
            selected_name = ""
        return {
            "tool_name": selected_name,
        }

    def _execute_tool(
        self,
        event: Event,
        node: Any,
        tool_name: str,
        *,
        tool_selection: dict[str, str],
    ) -> tuple[bool, dict[str, Any], str, dict[str, Any]]:
        tool = get_registered_tool(tool_name)
        if tool is None:
            return (
                False,
                {
                    "tool_name": tool_name,
                    "node_id": getattr(node, "node_id", ""),
                    "note": "No matching tool definition was found.",
                },
                f"tool_not_found:{tool_name}",
                {
                    "node_title": getattr(node, "title", ""),
                    "selected_by": "executor_builtin_router",
                },
            )

        tool_response = tool.execute(
            intent=str(getattr(node, "title", "") or ""),
        )
        tool_input = {
            "node_title": getattr(node, "title", ""),
            "selected_by": "executor_builtin_router",
            **dict(tool_response.get("tool_input") or {}),
        }
        result = dict(tool_response.get("result") or {})
        error_message = str(tool_response.get("error_message") or "")
        success = bool(tool_response.get("success"))
        return success, result, error_message, tool_input

    @staticmethod
    def _build_node_metadata_updates(
        *,
        tool_name: str,
        execution: Execution,
    ) -> dict[str, Any]:
        result = dict(execution.result or {})
        return {
            "tool_name": tool_name,
            "last_execution_id": execution.execution_id,
            "last_execution_status": execution.execution_status.value,
            "no_data_found": bool(result.get("no_data_found")),
            "search_stage": str(result.get("search_stage") or ""),
        }

    @staticmethod
    def _list_available_tools() -> list[dict[str, Any]]:
        return [
            tool.to_dict()
            for tool in list_registered_tools(include_non_routable=False)
        ]

    def _publish(
        self,
        *,
        event_id: str,
        round_id: int,
        message_type: MessageType,
        payload: dict[str, Any],
        to_role: RoleName | None = None,
    ) -> None:
        self.bus.publish(
            MessageEnvelope(
                event_id=event_id,
                round_id=round_id,
                from_role=RoleName.EXECUTOR,
                to_role=to_role,
                message_type=message_type,
                payload=payload,
            )
        )


def run_executor(poll_interval: float = 5.0) -> None:
    ExecutorRuntime(poll_interval=poll_interval).run_forever()


__all__ = [
    "EXECUTOR_SYSTEM_PROMPT",
    "ExecutorAgent",
    "ExecutorRuntime",
    "run_executor",
]
