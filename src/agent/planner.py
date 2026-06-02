from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, RoundReview, TTTNode, TTTNodeLevel, TTTNodeStatus, TracebackTaskTree
from src.schema.event import utc_now
from src.storage import SQLiteStorage


logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Planner。
你是整个溯源流程的总规划者，负责接收告警、进行初始研判，并维护共享黑板 TTT（Traceback Task Tree）。

你的职责只有两类：
1. 在收到新告警时，初始化 TTT。
2. 在每一轮结束后，根据 Reviewer 返回的总结更新 TTT。

你不直接执行工具，不伪造日志，不编造企业中不存在的能力。

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
- 每个 L3 节点必须包含 task_type 和 assignee。
- 更新 TTT 时必须优先做最小改动，避免无必要重写整棵树。
- 已经完成的节点应视为冻结节点，除非有强证据，否则不要改写其语义。

输出示例：
```yaml
type: llm_response
from: _planner
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: TTT_PLAN
response_text: 对告警的初始分析。
ttt:
  schema_version: "1.0"
  event_id: "{ 来自输入 }"
  round_id: "{ 来自输入 }"
  root_nodes:
    - node_id: "phase-1"
      title: "阶段一：确认告警真实性与攻击范围"
      status: todo
      children:
        - node_id: "phase-1:l2-1"
          title: "子目标1.1：确认告警中的源与目标是否可信"
          status: todo
          children:
            - node_id: "phase-1:l2-1:l3-1"
              title: "执行意图1.1.1：查询源 IP 基础情报与历史行为"
              status: todo
              task_type: query
              assignee: _executor
              children: []
```
""".strip()


@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = (
        "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    )
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
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = PlannerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
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
            payload={"text": "planner_start_initial_planning", "event_name": event.event_name},
        )

        candidate_tree = self._generate_initial_ttt(event)
        snapshot = self.ttt_store.save_snapshot(candidate_tree)

        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
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
            payload={"ttt": snapshot.to_dict(), "event_status": updated_event.event_status.value},
        )
        return updated_event

    def process_replanning(self, event: Event) -> Event:
        logger.info("Planner processing replanning for event=%s round=%s", event.event_id, event.current_round)
        latest_review = self.storage.get_round_review(event.event_id, event.current_round)
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        if latest_review is None or latest_ttt is None:
            logger.warning("Planner replanning skipped for event=%s due to missing review or ttt", event.event_id)
            return event

        next_round = event.current_round + 1
        candidate_tree = self._generate_updated_ttt(
            event=event,
            latest_review=latest_review,
            latest_ttt=latest_ttt,
            next_round=next_round,
        )
        snapshot = self.ttt_store.save_snapshot(candidate_tree)
        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
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
            payload={"ttt": snapshot.to_dict(), "based_on_review": latest_review.to_dict()},
        )
        return updated_event

    def _generate_initial_ttt(self, event: Event) -> TracebackTaskTree:
        user_prompt = "\n".join(
            [
                "请根据以下安全告警初始化 TTT，并只返回 YAML。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        try:
            response_text = call_llm(
                self.agent.system_prompt,
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
            parsed = parse_yaml_response(response_text)
            if parsed:
                ttt_payload = parsed.get("ttt") or parsed.get("tree")
                if isinstance(ttt_payload, dict):
                    return self._normalize_ttt_payload(
                        event_id=event.event_id,
                        round_id=event.current_round,
                        ttt_payload=ttt_payload,
                    )
        except Exception:
            logger.exception("Planner initial TTT generation failed, using fallback")
        return self._build_fallback_ttt(event, event.current_round)

    def _generate_updated_ttt(
        self,
        *,
        event: Event,
        latest_review: RoundReview,
        latest_ttt: TracebackTaskTree,
        next_round: int,
    ) -> TracebackTaskTree:
        user_prompt = "\n".join(
            [
                "请根据以下事件、上一轮 TTT 和 Reviewer 总结，更新下一轮 TTT，并只返回 YAML。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_review.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        try:
            response_text = call_llm(
                self.agent.system_prompt,
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
            parsed = parse_yaml_response(response_text)
            if parsed:
                ttt_payload = parsed.get("ttt") or parsed.get("tree")
                if isinstance(ttt_payload, dict):
                    return self._normalize_ttt_payload(
                        event_id=event.event_id,
                        round_id=next_round,
                        ttt_payload=ttt_payload,
                    )
        except Exception:
            logger.exception("Planner TTT update failed, using fallback")
        return self._build_fallback_ttt(event, next_round, latest_review=latest_review)

    def _normalize_ttt_payload(
        self,
        *,
        event_id: str,
        round_id: int,
        ttt_payload: dict[str, Any],
    ) -> TracebackTaskTree:
        roots = ttt_payload.get("root_nodes") or ttt_payload.get("nodes") or []
        normalized_roots = tuple(
            self._normalize_node(node, depth=1, path=f"phase-{index}")
            for index, node in enumerate(roots, start=1)
        )
        return TracebackTaskTree(
            event_id=event_id,
            round_id=round_id,
            root_nodes=normalized_roots,
            schema_version=str(ttt_payload.get("schema_version") or "1.0"),
            updated_by=self.agent.role_name,
        )

    def _normalize_node(self, node: dict[str, Any], *, depth: int, path: str) -> TTTNode:
        level = {
            1: TTTNodeLevel.PHASE,
            2: TTTNodeLevel.SUB_GOAL,
            3: TTTNodeLevel.ATOMIC_INTENT,
        }.get(depth, TTTNodeLevel.ATOMIC_INTENT)
        status = TTTNodeStatus(str(node.get("status") or TTTNodeStatus.TODO.value))
        children_raw = node.get("children") or []
        children = tuple(
            self._normalize_node(
                child,
                depth=min(depth + 1, 3),
                path=f"{path}:{child_index}",
            )
            for child_index, child in enumerate(children_raw, start=1)
        )
        task_type = node.get("task_type")
        assignee = node.get("assignee")
        if level == TTTNodeLevel.ATOMIC_INTENT:
            task_type = str(task_type or "query")
            assignee = str(assignee or "_executor")
        return TTTNode(
            node_id=str(node.get("node_id") or path),
            title=str(node.get("title") or path),
            node_level=level,
            status=status,
            task_type=str(task_type) if task_type is not None else None,
            assignee=str(assignee) if assignee is not None else None,
            children=children,
            metadata=dict(node.get("metadata") or {}),
        )

    def _build_fallback_ttt(
        self,
        event: Event,
        round_id: int,
        latest_review: RoundReview | None = None,
    ) -> TracebackTaskTree:
        focus = "确认告警真实性与攻击路径"
        if latest_review and latest_review.recommendations:
            focus = latest_review.recommendations[0]

        roots = (
            TTTNode(
                node_id=f"event-{event.event_id}-phase-1",
                title="阶段一：确认告警真实性与核心对象",
                node_level=TTTNodeLevel.PHASE,
                status=TTTNodeStatus.TODO,
                children=(
                    TTTNode(
                        node_id=f"event-{event.event_id}-phase-1:l2-1",
                        title="子目标1.1：确认攻击源的基础画像",
                        node_level=TTTNodeLevel.SUB_GOAL,
                        status=TTTNodeStatus.TODO,
                        children=(
                            TTTNode(
                                node_id=f"event-{event.event_id}-phase-1:l2-1:l3-1",
                                title="执行意图1.1.1：查询源 IP 基础情报",
                                node_level=TTTNodeLevel.ATOMIC_INTENT,
                                status=TTTNodeStatus.TODO,
                                task_type="query",
                                assignee="_executor",
                            ),
                        ),
                    ),
                    TTTNode(
                        node_id=f"event-{event.event_id}-phase-1:l2-2",
                        title="子目标1.2：确认告警涉及的关键上下文",
                        node_level=TTTNodeLevel.SUB_GOAL,
                        status=TTTNodeStatus.TODO,
                        children=(
                            TTTNode(
                                node_id=f"event-{event.event_id}-phase-1:l2-2:l3-1",
                                title="执行意图1.2.1：提取当前事件已有上下文",
                                node_level=TTTNodeLevel.ATOMIC_INTENT,
                                status=TTTNodeStatus.TODO,
                                task_type="query",
                                assignee="_executor",
                            ),
                        ),
                    ),
                ),
            ),
            TTTNode(
                node_id=f"event-{event.event_id}-phase-2",
                title=f"阶段二：围绕“{focus}”推进后续判断",
                node_level=TTTNodeLevel.PHASE,
                status=TTTNodeStatus.TODO,
                children=(
                    TTTNode(
                        node_id=f"event-{event.event_id}-phase-2:l2-1",
                        title="子目标2.1：关联目标资产和日志线索",
                        node_level=TTTNodeLevel.SUB_GOAL,
                        status=TTTNodeStatus.TODO,
                        children=(
                            TTTNode(
                                node_id=f"event-{event.event_id}-phase-2:l2-1:l3-1",
                                title="执行意图2.1.1：查询目标资产或日志能力缺口",
                                node_level=TTTNodeLevel.ATOMIC_INTENT,
                                status=TTTNodeStatus.TODO,
                                task_type="query",
                                assignee="_executor",
                            ),
                        ),
                    ),
                ),
            ),
        )
        return TracebackTaskTree(
            event_id=event.event_id,
            round_id=round_id,
            root_nodes=roots,
            schema_version="1.0",
            updated_by=self.agent.role_name,
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
