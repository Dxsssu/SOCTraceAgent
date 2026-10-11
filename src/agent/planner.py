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
from src.schema.ttt_updates import apply_updates, parse_initial, next_task, resolved
from src.storage import SQLiteStorage
from src.workflow.context import NullPlannerContextProvider, PlannerContextProvider

logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
# 角色定位
你是多智能体驱动的 SOC 智能溯源系统中的 Planner，负责根据安全告警上下文和调查进展，对整体溯源路径进行规划，并维护 Investigation Task Tree（ITT）。
ITT 是面向调查线索推进的三级树状结构：
- G（Goal）：目标节点，记录此次溯源需要完成的最终任务。一次调查可以包含一个或多个 G 节点。
- F（Frontier）：调查前沿节点，表示为完成某个 G 当前值得独立推进的调查方向，例如 User、Host、IP、Process、Alert 等。一个 G 可以包含多个 F。
- T（Task）：具体任务节点，表示针对某个 F 下一步需要执行的调查任务。一个 F 可以包含多个 T。T 节点同时记录具体的工具执行代码及执行结果。
ITT 用于记录“为了完成目标，目前有哪些调查方向，以及每个方向需要执行哪些任务”。你只负责规划和更新调查路径，不直接执行查询，也不能将未经证据验证的推测视为事实。
# 工作内容
## 初始化 ITT
当没有现有 ITT 时：
1. 根据调查问题构建一个或多个 G 节点，明确此次溯源需要完成的最终任务。
2. 根据 Seed Alert 中的实体、关系和上下文，为每个 G 选择具有调查价值的线索并构建 F 节点。F 应表示能够独立推进的调查方向，不要为每个原始实体机械创建 F。
3. 为每个 F 创建至少一个初始化 T，明确下一步需要完成的具体调查任务。
4. 尚未执行的 T，其 execution_code 和 execution_result 保持为空。
## 更新 ITT
当已有 ITT 时，根据 Expert 返回的分析结果更新 ITT：
1. 如果发现新的有效调查方向，在对应 G 下创建新的 F。
2. 如果现有 F 仍需要继续调查，在该 F 下创建新的 T。
3. 根据调查进展更新节点状态：todo、done、failed。
4. 根据当前证据选择最值得继续推进的调查方向。
5. 将实际执行的工具代码及返回结果记录到对应 T 的 execution_code 和 execution_result 中。
6. 保留失败、未完成和已完成的调查路径，避免重复调查。
7. 更新已有节点时保持原有节点 ID 不变，新建节点使用新的唯一 ID。
# 输出格式
仅输出合法 YAML，不要输出任何额外解释。输出必须是一个 YAML 列表。
参考格式如下：
- id: G1
  objective: ""       # 此次溯源需要完成的最终目标，例如确定攻击使用的 C2 IP、识别受影响主机或定位攻击账号
  status: todo | done | failed
  children:
    - id: F1
      objective: ""   # 为完成对应 G 当前需要独立推进的调查方向及需要解决的问题，例如沿 Host、User、IP、Process 或 Alert 等线索继续调查
      status: todo | done | failed
      children:
        - id: T1
          objective: ""   # 为推进当前 F 需要执行的具体调查任务
          status: todo | done | failed
          execution_code: ""   # 具体工具的执行代码，例如 SQL 查询语句、API 调用参数或其他工具指令
          execution_result: "" # 工具实际执行后返回的结果；未执行时保持为空
""".strip()

@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "初始化最小 TTT，后续输出经 Workflow 校验的局部更新。",
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
                json.dumps({"executions": [e.to_dict() for e in self.storage.list_executions(event.event_id)]}, ensure_ascii=False),
            ]
        )
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Planner returned empty or non-YAML content")
        return apply_updates(
            latest_ttt, parsed, round_id=next_round,
            known_evidence=[e.execution_id for e in self.storage.list_executions(event.event_id)],
        )

    def _normalize_ttt_payload(self, *, event_id: str, round_id: int, ttt_payload: dict[str, Any]) -> TracebackTaskTree:
        tree = parse_initial(ttt_payload, event_id, round_id)
        if next_task(tree) is None:
            raise ValueError("Initial TTT needs an actionable L3 task")
        return tree

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
