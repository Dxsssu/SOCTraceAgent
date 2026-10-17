from __future__ import annotations

import json
import logging
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
from src.schema.itt import parse_itt, parse_itt_response, itt_payload, planner_prompt
from src.storage import SQLiteStorage
from src.workflow.context import NullPlannerContextProvider, PlannerContextProvider

logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
# 角色定位
你是多智能体驱动的 SOC 智能溯源系统中的 Planner，负责将调查目标分解为逐步推进的查询任务，并维护 Investigation Task Tree（ITT），使后续调查能够利用已经获得的上下文和结果。
# ITT 结构
ITT 由 G、E、T 三层组成：
- G（Goal）：需要回答的调查问题。一个 G 对应一个独立问题。
- E（Entity）：与 G 相关的单个调查实体。objective 只写简短的实体类型和名称或标识。
- T（Task）：针对所属 E 的单步查询任务，由 Executor 通过一次 MySQL 只读查询完成。T 应解决通向 G 的一个具体子问题，不直接复述最终目标，避免将多个相互依赖的步骤合并为一个任务。
# 首轮调查
根据问题建立 G，从背景中选择直接相关的已知实体建立 E。每个 E 仅创建一个当前优先的 T。
# 后续调查
结合已有 ITT 和 Reviewer 总结，输出更新后的完整的 ITT 结构，根据情况增加 E 节点和 T 节点。
# 输出格式模板
严格按照以下模板输出完整 ITT，仅输出合法 YAML 列表，不添加解释或模板以外的字段。模板中的目标和实体仅为示例，须替换为本次输入的实际信息。节点id编号要唯一。
- id: G1
  objective: "定位告警关联进程的可执行文件路径"
  children:
    - id: E1
      objective: "告警 alert-001"
      children:
        - id: T1
          objective: "查询告警 alert-001 的关联进程 PID"
          status: todo｜done｜error
          execution_code: ""   # 由其他角色补充实际执行代码；Planner 创建任务时只需置空
          execution_result: "" # 由程序写入 Reviewer 总结；Planner 创建任务时置空
""".strip()

@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "初始化并更新 G/E/T 调查树，由 Workflow 校验后保存。",
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
            json.dumps(task_input(event), ensure_ascii=False, indent=2),
        )
        return parse_itt(parse_itt_response(response), event.event_id, event.current_round,
                         procedures=memory.get("procedural_memory", {}).get("procedures", []))

    def _generate_updated_ttt(
        self, *, event: Event, latest_review: RoundReview,
        latest_ttt: TracebackTaskTree, next_round: int,
    ) -> TracebackTaskTree:
        executions = self.storage.list_executions(event.event_id)
        memory = self.context_provider.build(
            event, review=latest_review, ttt=latest_ttt, executions=executions,
        )
        payload = dict(task_input(event), itt=itt_payload(latest_ttt, executions, planner_view=True),
                       review={"summary": "；".join((*latest_review.findings, *latest_review.gaps)),
                               "suggestion": "；".join(latest_review.recommendations)})
        response = call_llm(planner_prompt(self.agent.system_prompt, memory),
                            json.dumps(payload, ensure_ascii=False, indent=2))
        return parse_itt(parse_itt_response(response), event.event_id, next_round,
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
