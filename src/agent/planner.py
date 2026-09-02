from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import (
    Event,
    EventStatus,
    RoundReview,
    TracebackTaskTree,
    TTTNode,
    TTTNodeStatus,
)
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.workflow.context import NullPlannerContextProvider, PlannerContextProvider

logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Planner。
你是整个溯源流程的总规划者，负责接收告警、进行初始研判，并维护共享黑板 TTT（Traceback Task Tree）。

你的职责只有两类：
1. 在收到新告警时，初始化 TTT。
2. 在每一轮结束后，根据 Reviewer 返回的总结更新 TTT。

你不直接执行工具，不伪造日志，不编造企业中不存在的能力。

长期记忆边界：
- Workflow 只会向你提供 Procedural Memory 和 Semantic Memory。
- Procedural Memory 用于选择整体调查流程、阶段和溯源方向。
- Semantic Memory 用于确认当前环境中真实存在的日志表、字段、实体类型和连接能力。
- 你不得访问或引用 Episodic Memory；历史查询案例、SQL 模板和错误修复属于 Executor 的职责。
- 你不得生成 SQL、WHERE 条件、JOIN 表达式或具体工具参数。
- 如果 Workflow 未提供长期记忆上下文，应基于现有事件谨慎规划，不得假设某张表或字段存在。

你的输出必须严格使用 YAML，且只能输出以下三种 response_type：
- ROGER
- TTT_PLAN
- TTT_UPDATE

TTT 约束：
- TTT 必须是完整快照，而不是增量片段。
- TTT 必须严格三层：L1（战略层） -> L2（战术层） -> L3（执行层）。
- L1 只描述单一核心目标。
- L2 只描述待验证的子问题或假设。
- L3 才是可执行的证据搜集意图。
- 仅允许三层，禁止出现第 4 层及以上层级。
- L3 必须是叶子节点，children 必须为空数组。
- node_id 必须使用纯数字分层编号，如 `1`、`1-2`、`1-2-3`。
- 更新 TTT 时必须优先做最小改动，避免无必要重写整棵树。
- 已经完成的节点应视为冻结节点，除非有强证据，否则不要改写其语义。
- L3 只描述原子证据搜集意图和成功条件，不描述具体查询实现。
- TTT 必须尽量精简；每个节点只输出 node_id、title、status 和 children。
- 不要输出 metadata、Memory 引用、候选表、优先级、成功条件或停止条件。
- L3 的 title 应完整表达要收集的证据，但不要包含具体 SQL 实现。

输出示例：
```yaml
type: llm_response
from: _planner
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: TTT_PLAN
response_text: 对告警的初始分析。
ttt:
  event_id: "{ 来自输入 }"
  round_id: "{ 来自输入 }"
  root_nodes:
    - node_id: "1"
      title: "阶段一：确认告警真实性与攻击范围"
      status: todo
      children:
        - node_id: "1-1"
          title: "子目标1.1：确认告警中的源与目标是否可信"
          status: todo
          children:
            - node_id: "1-1-1"
              title: "执行意图1.1.1：查询源 IP 基础情报与历史行为"
              status: todo
              children: []
```
""".strip()


@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "输出完整 TTT 快照，作为系统共享黑板。",
        "根据 Reviewer 的轮次总结更新节点状态、结构和下一步重点。",
    )
    allowed_response_types: tuple[str, ...] = (
        "ROGER",
        "TTT_PLAN",
        "TTT_UPDATE",
    )
    system_prompt: str = PLANNER_SYSTEM_PROMPT


class PlannerRuntime:
    """Planner 角色运行时实现。"""

    def __init__(
        self,
        *,
        storage: SQLiteStorage | None = None,
        ttt_store: TTTStore | None = None,
        bus: SQLiteMessageBus | None = None,
        context_provider: PlannerContextProvider | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = PlannerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.context_provider = context_provider or NullPlannerContextProvider()
        self.poll_interval = poll_interval
        self.running = False

    def run_once(self) -> bool:
        did_work = False

        pending_events = self.storage.list_events_by_status(EventStatus.PENDING.value)
        for event in pending_events:
            self.process_initial_plan(event)
            did_work = True

        replan_events = self.storage.list_events_by_status(EventStatus.REPLANNING.value)
        for event in replan_events:
            self.process_replanning(event)
            did_work = True

        return did_work

    def run_forever(self) -> None:
        self.running = True
        logger.info("Planner runtime started")
        while self.running:
            try:
                did_work = self.run_once()
                if not did_work:
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Planner runtime interrupted")
                self.running = False
            except Exception:
                logger.exception("Planner runtime loop failed")
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self.running = False

    def process_initial_plan(self, event: Event) -> Event:
        logger.info("Planner processing initial plan for event=%s", event.event_id)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_INFO,
            payload={
                "text": "planner_start_initial_planning",
                "event_name": event.event_name,
            },
        )

        try:
            candidate_tree = self._generate_initial_ttt(event)
        except Exception as exc:
            logger.exception("Planner initial TTT generation failed")
            return self._fail_event(
                event, f"Planner initial TTT generation failed: {exc}"
            )
        snapshot = self.ttt_store.save_snapshot(candidate_tree)

        next_status = (
            EventStatus.PLANNED
            if self.ttt_store.has_open_work(event.event_id, snapshot.round_id)
            else EventStatus.COMPLETED
        )
        updated_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=next_status,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(updated_event)
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_INITIALIZED,
            payload={
                "ttt": snapshot.to_dict(),
                "event_status": updated_event.event_status.value,
            },
        )
        return updated_event

    def process_replanning(self, event: Event) -> Event:
        logger.info(
            "Planner processing replanning for event=%s round=%s",
            event.event_id,
            event.current_round,
        )
        latest_review = self.storage.get_round_review(
            event.event_id, event.current_round
        )
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        if latest_review is None or latest_ttt is None:
            logger.warning(
                "Planner replanning skipped for event=%s due to missing review or ttt",
                event.event_id,
            )
            return event

        next_round = event.current_round + 1
        try:
            candidate_tree = self._generate_updated_ttt(
                event=event,
                latest_review=latest_review,
                latest_ttt=latest_ttt,
                next_round=next_round,
            )
        except Exception as exc:
            logger.exception("Planner TTT update failed")
            return self._fail_event(event, f"Planner TTT update failed: {exc}")
        snapshot = self.ttt_store.save_snapshot(candidate_tree)
        next_status = (
            EventStatus.PLANNED
            if self.ttt_store.has_open_work(event.event_id, snapshot.round_id)
            else EventStatus.COMPLETED
        )
        updated_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=next_status,
            current_round=next_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(updated_event)
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_UPDATED,
            payload={
                "ttt": snapshot.to_dict(),
                "based_on_review": latest_review.to_dict(),
            },
        )
        return updated_event

    def _generate_initial_ttt(self, event: Event) -> TracebackTaskTree:
        memory_context = self.context_provider.build(event)
        user_prompt = "\n".join(
            [
                "请根据以下安全告警初始化 TTT，并只返回 YAML。",
                "以下 workflow_memory_context 已经过角色权限过滤；你只能使用其中提供的 Procedural Memory 和 Semantic Memory。",
                json.dumps(
                    {"workflow_memory_context": memory_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Planner returned empty or non-YAML content")
        ttt_payload = parsed.get("ttt") or parsed.get("tree")
        if not isinstance(ttt_payload, dict):
            raise ValueError("Planner response missing `ttt`/`tree` object")
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=event.current_round,
            ttt_payload=ttt_payload,
        )

    def _generate_updated_ttt(
        self,
        *,
        event: Event,
        latest_review: RoundReview,
        latest_ttt: TracebackTaskTree,
        next_round: int,
    ) -> TracebackTaskTree:
        memory_context = self.context_provider.build(
            event,
            review=latest_review,
            ttt=latest_ttt,
        )
        user_prompt = "\n".join(
            [
                "请根据以下事件、上一轮 TTT 和 Reviewer 总结，更新下一轮 TTT，并只返回 YAML。",
                "以下 workflow_memory_context 已经过角色权限过滤；你只能使用其中提供的 Procedural Memory 和 Semantic Memory。",
                json.dumps(
                    {"workflow_memory_context": memory_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_review.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Planner returned empty or non-YAML content")
        ttt_payload = parsed.get("ttt") or parsed.get("tree")
        if not isinstance(ttt_payload, dict):
            raise ValueError("Planner response missing `ttt`/`tree` object")
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=next_round,
            ttt_payload=ttt_payload,
        )

    def _normalize_ttt_payload(
        self,
        *,
        event_id: str,
        round_id: int,
        ttt_payload: dict[str, Any],
    ) -> TracebackTaskTree:
        roots = ttt_payload.get("root_nodes") or ttt_payload.get("nodes") or []
        normalized_roots = tuple(
            self._normalize_node(node, path=str(index))
            for index, node in enumerate(roots, start=1)
        )
        return TracebackTaskTree(
            event_id=event_id,
            round_id=round_id,
            root_nodes=normalized_roots,
        )

    def _normalize_node(self, node: dict[str, Any], *, path: str) -> TTTNode:
        status = TTTNodeStatus(str(node.get("status") or TTTNodeStatus.TODO.value))
        children_raw = node.get("children") or []
        children = tuple(
            self._normalize_node(
                child,
                path=f"{path}-{child_index}",
            )
            for child_index, child in enumerate(children_raw, start=1)
        )
        return TTTNode(
            node_id=path,
            title=str(node.get("title") or path),
            status=status,
            children=children,
            metadata={},
        )

    def _fail_event(self, event: Event, reason: str) -> Event:
        failed_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=EventStatus.FAILED,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(failed_event)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_ERROR,
            payload={"text": reason},
        )
        return failed_event

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
                from_role=RoleName.PLANNER,
                to_role=to_role,
                message_type=message_type,
                payload=payload,
            )
        )


def run_planner(poll_interval: float = 5.0) -> None:
    PlannerRuntime(poll_interval=poll_interval).run_forever()


__all__ = [
    "PLANNER_SYSTEM_PROMPT",
    "PlannerAgent",
    "PlannerRuntime",
    "run_planner",
]
