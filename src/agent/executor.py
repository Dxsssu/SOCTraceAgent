from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time
from typing import Any

from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, Execution, ExecutionStatus, TTTNodeStatus
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.tools import SplunkQuerySpec, SplunkSearchTool


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
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = ExecutorAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.splunk_tool = SplunkSearchTool()
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

        tool_name = self._select_tool_name(claimed)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.TOOL_SELECTED,
            payload={"node_id": claimed.node_id, "tool_name": tool_name},
        )

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.EXECUTION_STARTED,
            payload={"node_id": claimed.node_id, "tool_name": tool_name},
        )
        success, result, error_message, tool_input = self._execute_tool(event, claimed, tool_name)
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
            metadata_updates={
                "tool_name": tool_name,
                "last_execution_id": execution.execution_id,
                "last_execution_status": execution.execution_status.value,
            },
        )
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.EXECUTION_COMPLETED if success else MessageType.EXECUTION_FAILED,
            payload=execution.to_dict(),
            to_role=RoleName.REVIEWER if not self.ttt_store.has_open_work(event.event_id, event.current_round) else None,
        )
        return True

    def _select_tool_name(self, node: Any) -> str:
        title = (getattr(node, "title", "") or "").lower()
        if "上下文" in title or "告警" in title:
            return "event_context_lookup"
        if "ip" in title or "情报" in title:
            return "threat_intel_lookup"
        if "资产" in title:
            return "asset_inventory_lookup"
        if "日志" in title or "认证" in title or "登录" in title:
            return "log_search"
        return "investigation_notebook"

    def _execute_tool(
        self,
        event: Event,
        node: Any,
        tool_name: str,
    ) -> tuple[bool, dict[str, Any], str, dict[str, Any]]:
        if tool_name == "event_context_lookup":
            return (
                True,
                {
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "message": event.message,
                    "context": event.context,
                    "source": event.source,
                    "note": "从当前事件记录中提取已有上下文。",
                },
                "",
                {"node_title": getattr(node, "title", "")},
            )
        if tool_name == "log_search":
            query_spec, strategy, translation_error = self._derive_log_search_spec(event, node)
            search_result = self.splunk_tool.search(
                spec=query_spec,
                additional_context={
                    "event": event.to_dict(),
                    "node": {
                        "node_id": getattr(node, "node_id", ""),
                        "title": getattr(node, "title", ""),
                    },
                },
            )
            tool_input = {
                "node_title": getattr(node, "title", ""),
                "translation_strategy": strategy,
                "translation_error": translation_error,
                "query_spec": search_result.get("query_spec", query_spec.to_dict()),
                "query": search_result.get("query", ""),
            }
            error_message = str(search_result.get("error_message") or "")
            if search_result.get("success"):
                if search_result.get("result_count", 0) == 0:
                    return (
                        False,
                        search_result,
                        "zero_results:No events matched the query",
                        tool_input,
                    )
                return True, search_result, "", tool_input
            return False, search_result, error_message, tool_input
        return (
            False,
            {
                "tool_name": tool_name,
                "node_id": getattr(node, "node_id", ""),
                "note": "当前项目尚未接入真实外部工具。",
            },
            f"tool_not_implemented:{tool_name}",
            {"node_title": getattr(node, "title", "")},
        )

    def _derive_log_search_spec(self, event: Event, node: Any) -> tuple[SplunkQuerySpec, str, str]:
        fallback_spec = self._build_log_search_spec(event, node)
        dataset = fallback_spec.dataset or "botsv1"
        additional_context = {
            "event": event.to_dict(),
            "node": {
                "node_id": getattr(node, "node_id", ""),
                "title": getattr(node, "title", ""),
            },
            "fallback_spec": fallback_spec.to_dict(),
        }
        try:
            spec = self.splunk_tool.interpret_intent(
                str(getattr(node, "title", "") or ""),
                dataset=dataset,
                additional_context=additional_context,
            )
            merged_spec = SplunkQuerySpec(
                dataset=spec.dataset or fallback_spec.dataset,
                index=spec.index or fallback_spec.index,
                sourcetype=spec.sourcetype or fallback_spec.sourcetype,
                earliest=spec.earliest or fallback_spec.earliest,
                latest=spec.latest or fallback_spec.latest,
                keywords=spec.keywords or fallback_spec.keywords,
                ip=spec.ip or fallback_spec.ip,
                host=spec.host or fallback_spec.host,
                source=spec.source or fallback_spec.source,
                field_filters=spec.field_filters or fallback_spec.field_filters,
                limit=spec.limit or fallback_spec.limit,
                fields=spec.fields or fallback_spec.fields,
            )
            return merged_spec, "llm_intent_translation", ""
        except Exception as exc:
            logger.warning("LLM log-search translation failed for node=%s: %s", getattr(node, "node_id", ""), exc)
            return fallback_spec, "rule_based_fallback", str(exc)

    def _build_log_search_spec(self, event: Event, node: Any) -> SplunkQuerySpec:
        title = str(getattr(node, "title", "") or "").strip()
        context = dict(event.context or {})
        dataset = str(
            context.get("splunk_dataset")
            or context.get("dataset")
            or context.get("log_dataset")
            or "botsv1"
        ).strip()
        sourcetype = self._infer_sourcetype(title, event.message, context)
        earliest = self._first_non_empty(
            context.get("splunk_earliest"),
            context.get("earliest"),
            context.get("time_range_start"),
        )
        latest = self._first_non_empty(
            context.get("splunk_latest"),
            context.get("latest"),
            context.get("time_range_end"),
        )
        source = self._first_non_empty(context.get("source_filter"), context.get("splunk_source"))
        host = self._first_non_empty(context.get("host"), context.get("asset_host"), context.get("hostname"))
        ip = self._extract_ip(title) or self._extract_ip(event.message)
        keywords = self._build_log_search_keywords(title, event.message, context)
        fields = tuple(
            field
            for field in self._normalize_sequence(
                context.get("splunk_fields") or context.get("fields")
            )
        )
        return SplunkQuerySpec(
            dataset=dataset or None,
            sourcetype=sourcetype,
            earliest=earliest,
            latest=latest,
            keywords=keywords,
            ip=ip,
            host=host,
            source=source,
            field_filters=self._extract_field_filters(context),
            fields=fields,
        )

    @staticmethod
    def _extract_ip(text: str) -> str | None:
        match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text or "")
        return match.group(0) if match else None

    @staticmethod
    def _infer_sourcetype(title: str, message: str, context: dict[str, Any]) -> str | None:
        explicit = str(context.get("sourcetype") or context.get("splunk_sourcetype") or "").strip()
        if explicit:
            return explicit
        combined = f"{title} {message}".lower()
        if "sysmon" in combined:
            return "XmlWinEventLog:Microsoft-Windows-Sysmon/Operational"
        if "dns" in combined:
            return "stream:dns"
        if "http" in combined or "web" in combined:
            return "stream:http"
        if "认证" in combined or "登录" in combined or "security" in combined:
            return "WinEventLog:Security"
        if "iis" in combined:
            return "iis"
        if "suricata" in combined:
            return "suricata"
        return None

    @staticmethod
    def _build_log_search_keywords(title: str, message: str, context: dict[str, Any]) -> tuple[str, ...]:
        keywords: list[str] = []
        for entry in ExecutorRuntime._normalize_sequence(context.get("keywords")):
            if entry not in keywords:
                keywords.append(entry)

        combined = f"{title} {message}".lower()
        for candidate in ("登录", "认证", "失败", "成功", "powershell", "cmd.exe", "rundll32", "suricata", "dns", "http"):
            if candidate.lower() in combined and candidate not in keywords:
                keywords.append(candidate)
        return tuple(keywords)

    @staticmethod
    def _extract_field_filters(context: dict[str, Any]) -> dict[str, str | tuple[str, ...]]:
        raw = context.get("field_filters")
        if isinstance(raw, dict):
            normalized: dict[str, str | tuple[str, ...]] = {}
            for key, value in raw.items():
                if isinstance(value, (list, tuple, set)):
                    cleaned = tuple(str(item).strip() for item in value if str(item).strip())
                    if cleaned:
                        normalized[str(key).strip()] = cleaned
                else:
                    text = str(value).strip()
                    if text:
                        normalized[str(key).strip()] = text
            return normalized
        return {}

    @staticmethod
    def _normalize_sequence(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items = [value]
        else:
            items = list(value)
        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _first_non_empty(*values: Any) -> str | None:
        for value in values:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
        return None

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
