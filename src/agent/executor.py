from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, Execution, ExecutionStatus, TTTNodeStatus
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.tools import get_registered_tool, list_registered_tools
from src.workflow.context import ExecutorContextProvider, NullExecutorContextProvider

logger = logging.getLogger(__name__)


EXECUTOR_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Executor。
你的职责是读取 TTT 中下一个待执行的叶子节点，理解节点意图，选择合适工具并执行。

你的职责只有一类：
1. 消费 TTT 的 L3 叶子节点，并完成证据检索或动作执行。

你的边界：
- 你不负责全局规划，不更新整棵 TTT。
- 你不负责最终总结，不代替 Reviewer。
- 你只能围绕当前叶子节点执行，不得自行扩展任务范围。
- 如果不存在合适工具，必须明确返回无法执行的原因，不能编造工具结果。

长期记忆边界：
- Workflow 只会向你提供与当前 L3 相关的 Semantic Memory 和 Episodic Memory。
- Semantic Memory 是表、字段和连接键的硬约束，不得使用其中不存在的 Schema 对象。
- Episodic Memory 只提供脱敏的历史查询结构、结果状态和错误修复参考。
- Episodic Memory 中的历史任务和查询模板不是当前事件证据，不得把占位符或历史值当作答案。
- 你不查询 Procedural Memory；调查方向由 Planner 通过 TTT 传递。

你的输出必须严格使用 YAML，且只能输出以下两种 response_type：
- ROGER
- EXECUTION_RESULT

执行要求：
- 必须先理解当前叶子节点的目标对象、时间范围、证据类型，再决定工具。
- 输出中要明确本次执行的节点、所选工具、执行结果和失败原因。
- 若无可用工具，必须如实说明缺失能力。

输出示例：
```yaml
type: llm_response
from: _executor
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: EXECUTION_RESULT
execution:
  node_id: "1-1-1"
  node_title: "执行意图1.1.1：查询源 IP 基础情报与历史行为"
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
    description: str = "负责检索 TTT 的下一个叶子节点，并调用合适工具执行。"
    responsibilities: tuple[str, ...] = (
        "读取当前待执行的 TTT 叶子节点。",
        "基于节点语义选择最匹配的工具。",
        "输出结构化执行结果或明确的失败原因。",
    )
    allowed_response_types: tuple[str, ...] = (
        "ROGER",
        "EXECUTION_RESULT",
    )
    system_prompt: str = EXECUTOR_SYSTEM_PROMPT


class ExecutorRuntime:
    """Executor 角色运行时实现。"""

    def __init__(
        self,
        *,
        storage: SQLiteStorage | None = None,
        ttt_store: TTTStore | None = None,
        bus: SQLiteMessageBus | None = None,
        context_provider: ExecutorContextProvider | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = ExecutorAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.context_provider = context_provider or NullExecutorContextProvider()
        self.poll_interval = poll_interval
        self.running = False

    def run_once(self) -> bool:
        did_work = False
        events = self.storage.list_events_by_status(
            EventStatus.PLANNED.value,
            EventStatus.EXECUTING.value,
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

        prior_executions = self.storage.list_executions(event.event_id)
        workflow_context = self.context_provider.build(
            event,
            node=claimed,
            executions=prior_executions,
        )
        tool_selection = self._select_tool(
            claimed, event, workflow_context=workflow_context
        )
        tool_name = tool_selection["tool_name"]
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.TOOL_SELECTED,
            payload={
                "node_id": claimed.node_id,
                "tool_name": tool_name,
                "reason": tool_selection.get("reason", ""),
                "confidence": tool_selection.get("confidence", ""),
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
            workflow_context=workflow_context,
        )
        execution = Execution(
            event_id=event.event_id,
            round_id=event.current_round,
            node_id=claimed.node_id,
            node_title=claimed.title,
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            execution_status=ExecutionStatus.COMPLETED
            if success
            else ExecutionStatus.FAILED,
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
            metadata_updates={
                "tool_name": tool_name,
                "last_execution_id": execution.execution_id,
                "last_execution_status": execution.execution_status.value,
            },
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
            message_type=MessageType.EXECUTION_COMPLETED
            if success
            else MessageType.EXECUTION_FAILED,
            payload=execution.to_dict(),
            to_role=RoleName.REVIEWER,
        )
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.HANDOFF_TO_REVIEWER,
            payload={
                "text": "single_leaf_ready_for_review",
                "node_id": claimed.node_id,
            },
            to_role=RoleName.REVIEWER,
        )
        return True

    def _select_tool(
        self,
        node: Any,
        event: Event,
        *,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        tools = self._list_available_tools()
        if not tools:
            return {
                "tool_name": "",
                "reason": "No MCP tools are currently registered for routing.",
                "confidence": "low",
            }

        intent = str(getattr(node, "title", "") or "")
        user_prompt = "\n".join(
            [
                "请从可用 MCP 工具中选择最适合执行当前 TTT 叶子节点任务的工具，只输出 YAML。",
                "输出字段只允许：tool_name, reason, confidence。",
                "confidence 只允许：high, medium, low。",
                "不得输出未列出的工具名。",
                f"intent: {intent}",
                f"event: {json.dumps(event.to_dict(), ensure_ascii=False, indent=2)}",
                f"node: {json.dumps({'node_id': getattr(node, 'node_id', ''), 'title': intent}, ensure_ascii=False, indent=2)}",
                f"context: {json.dumps(event.context or {}, ensure_ascii=False, indent=2)}",
                f"workflow_memory_context: {json.dumps(workflow_context or {}, ensure_ascii=False, indent=2)}",
                f"tools: {json.dumps(tools, ensure_ascii=False, indent=2)}",
            ]
        )
        parsed = parse_yaml_response(
            call_llm(
                """
你是一个 SOC 多工具路由器。
你的任务是根据当前调查意图，从候选 MCP 工具列表中选择唯一一个最合适的工具。
必须基于工具用途和限制做选择，不能编造工具名。
如果当前只有一个工具，就在解释原因后直接选择它。
""".strip(),
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
        )
        valid_names = {tool["name"] for tool in tools}
        selected_name = str((parsed or {}).get("tool_name") or "").strip()
        if selected_name not in valid_names:
            selected_name = tools[0]["name"]
            reason = "Tool router returned an invalid tool name, so the first available MCP tool was selected as fallback."
            confidence = "low"
        else:
            reason = str((parsed or {}).get("reason") or "").strip()
            confidence = (
                str((parsed or {}).get("confidence") or "medium").strip().lower()
            )
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        return {
            "tool_name": selected_name,
            "reason": reason,
            "confidence": confidence,
        }

    def _execute_tool(
        self,
        event: Event,
        node: Any,
        tool_name: str,
        *,
        tool_selection: dict[str, str],
        workflow_context: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any], str, dict[str, Any]]:
        tool = get_registered_tool(tool_name)
        if tool is None:
            return (
                False,
                {
                    "tool_name": tool_name,
                    "node_id": getattr(node, "node_id", ""),
                    "note": "未找到对应工具定义。",
                },
                f"tool_not_found:{tool_name}",
                {
                    "node_title": getattr(node, "title", ""),
                    "selected_by": "executor_builtin_router",
                    "selection_reason": tool_selection.get("reason", ""),
                    "selection_confidence": tool_selection.get("confidence", ""),
                },
            )

        tool_response = tool.execute(
            intent=self._build_tool_intent(
                event, node, workflow_context=workflow_context
            ),
        )
        tool_input = {
            "node_title": getattr(node, "title", ""),
            "selected_by": "executor_builtin_router",
            "selection_reason": tool_selection.get("reason", ""),
            "selection_confidence": tool_selection.get("confidence", ""),
            **dict(tool_response.get("tool_input") or {}),
        }
        result = dict(tool_response.get("result") or {})
        error_message = str(tool_response.get("error_message") or "")
        success = bool(tool_response.get("success"))
        return success, result, error_message, tool_input

    @staticmethod
    def _list_available_tools() -> list[dict[str, Any]]:
        return [
            tool.to_dict() for tool in list_registered_tools(include_non_routable=False)
        ]

    @staticmethod
    def _build_tool_intent(
        event: Event,
        node: Any,
        *,
        workflow_context: dict[str, Any] | None = None,
    ) -> str:
        node_title = str(getattr(node, "title", "") or "").strip()
        node_id = str(getattr(node, "node_id", "") or "").strip()
        context_text = json.dumps(event.context or {}, ensure_ascii=False, indent=2)
        return "\n".join(
            [
                "请基于以下事件背景与待执行任务，完成本次调查查询。",
                f"事件名称: {event.event_name}",
                f"事件描述: {event.message}",
                f"事件来源: {event.source}",
                f"事件严重级别: {event.severity.value}",
                f"TTT 节点 ID: {node_id}",
                f"TTT 叶子节点任务: {node_title}",
                "事件上下文:",
                context_text,
                "Workflow 提供的角色受限长期记忆上下文:",
                json.dumps(workflow_context or {}, ensure_ascii=False, indent=2),
            ]
        )

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
