from __future__ import annotations

import logging
import yaml
import time
from dataclasses import dataclass
from typing import Any

from src.agent.llm import call_llm
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import (
    Event,
    EventStatus,
    RoundReview,
    TracebackTaskTree,
)
from src.schema.event import utc_now
from src.schema.ttt_updates import next_task, resolved
from src.schema.investigation import task_input
from src.schema.itt import itt_payload, compact_json, planner_prompt
from src.schema.planner_operations import apply_planner_operations, parse_planner_response
from src.storage import SQLiteStorage
from src.workflow.context import NullPlannerContextProvider, PlannerContextProvider

logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
# 角色定位
你是多智能体驱动的 SOC 智能溯源系统中的 Planner，负责提出 Investigation Task Tree（ITT）的增量操作，由程序维护任务树。
# 调查结构
G（Goal）对应 question 中一个独立调查问题；E（Entity）是与 G 相关的单个实体，objective 写简短的实体类型和已知名称或标识；T（Task）是针对 E 的单步只读查询。
首轮依据 question 建立 G，从 context 中选择直接相关的已知实体建立 E，每个 E 先建立一个优先 T。任务应包含执行所需的已知参数和范围，不合并多个相互依赖的查询。
后续结合输入 ITT 和 Reviewer 总结，只提交本轮新增内容或下一任务选择。沿已有实体调查时直接引用其 ID；发现新实体时才 add_entity，不重复添加已有目标和任务。
输入 itt 是 G→E→T 三层嵌套树，通过 children 表示父子关系，每个节点保留 id 和 objective。T.execution_result 中 facts 保留记录关联，source 标记来源和执行状态，limitations 标记证据范围；优先用具体事实构造下一任务，不把错误、空结果或截断视为完整否定证据。
# 操作协议
仅输出合法 YAML 对象，唯一顶层字段为 operations，值为操作列表。允许以下操作：
- add_goal：必填 op、ref、objective；仅首轮可用。
- add_entity：必填 op、ref、goal_id、objective。
- add_task：必填 op、entity_id、objective；可选 ref。
- select_task：必填 op、task_id；每轮最多一次，用于优先执行一个 todo T。省略时按树顺序选择首个可执行 T。
程序分配正式 G/E/T ID。ref 是本次响应内唯一的小写别名（如 goal、alert）；用 $goal、$alert 引用此前操作创建的节点。已有节点直接使用输入中的 ID。别名只在本次响应内有效。
不输出完整树，不为新节点指定正式 ID，不输出 children、status、execution_code、execution_result，不修改或复制历史记录，不直接回答调查问题。已完成或失败的 T 不能重开，需要继续调查时新增 T。
已有可执行任务且无需新增内容时可输出 operations: []。SQL 调查中 Verifier 尚未确认答案而又没有可执行任务时，必须新增 T。
# 输出格式模板
operations:
  - op: add_goal
    ref: goal
    objective: "定位告警关联进程的可执行文件路径"
  - op: add_entity
    ref: alert
    goal_id: $goal
    objective: "告警 alert-001"
  - op: add_task
    entity_id: $alert
    objective: "查询告警 alert-001 的关联进程 PID"
""".strip()

@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "提交 G/E/T 增量操作，由 Workflow 分配 ID、校验并更新任务树。",
        "根据 Reviewer 的轮次总结增加实体和任务、选择下一步重点。",
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
        snapshot = self.ttt_store.save_snapshot(candidate_tree, expected_version=0)

        next_status = (
            EventStatus.PLANNED
            if self.ttt_store.has_open_work(event.event_id, snapshot.round_id)
            else (EventStatus.COMPLETED if resolved(snapshot) else EventStatus.FAILED)
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
        snapshot = self.ttt_store.save_snapshot(candidate_tree, expected_version=latest_ttt.version)
        next_status = (
            EventStatus.PLANNED
            if self.ttt_store.has_open_work(event.event_id, snapshot.round_id)
            else (EventStatus.COMPLETED if resolved(snapshot) else EventStatus.FAILED)
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
        memory = self.context_provider.build(event)
        response = call_llm(
            planner_prompt(self.agent.system_prompt, memory),
            compact_json(task_input(event)),
        )
        return apply_planner_operations(parse_planner_response(response), event.event_id, event.current_round,
                         procedures=memory.get("procedural_memory", {}).get("procedures", []))

    def _generate_updated_ttt(
        self, *, event: Event, latest_review: RoundReview,
        latest_ttt: TracebackTaskTree, next_round: int,
    ) -> TracebackTaskTree:
        executions = self.storage.list_executions(event.event_id)
        memory = self.context_provider.build(
            event, review=latest_review, ttt=latest_ttt, executions=executions,
        )
        payload = dict(task_input(event), itt=itt_payload(latest_ttt, executions, planner_view=True))
        if latest_review.gaps or latest_review.recommendations:
            payload["review"] = {"gaps": list(latest_review.gaps), "suggestions": list(latest_review.recommendations)}
        response = call_llm(planner_prompt(self.agent.system_prompt, memory),
                            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
        return apply_planner_operations(parse_planner_response(response), event.event_id, next_round,
                         previous=latest_ttt, executions=executions,
                         procedures=memory.get("procedural_memory", {}).get("procedures", []))

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
