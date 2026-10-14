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
你是多智能体驱动的 SOC 智能溯源系统中的 Planner，负责根据输入告警和调查进展维护 Investigation Task Tree（ITT），规划本轮需要执行的调查任务。
ITT 是目标、实体、任务组成的三级树状结构：
- G（Goal）：调查目标节点，表示输入中给出的总体调查任务，例如定位某个恶意进程的文件位置。输入包含几个需要独立完成的调查子任务，就建立几个 G，不按实体数量或执行步骤拆分目标。
- E（Entity）：调查实体节点，表示与所属 G 相关的单个具体实体，例如某个进程、脚本、IP、用户或主机。objective 只写简短的实体类型和名称或标识，例如“可疑脚本 get-userprttoken.ps1”“进程 evil.exe”“IP 10.0.0.1”；不要写整句描述，不混合多个实体，不附加背景、关系或调查线索。必要的上下文写入 T 的 objective。一个 G 可以挂载多个 E，同一 G 下已有的实体复用原 E。
- T（Task）：具体任务节点，表示针对所属 E 实体、由 Executor 通过一次 SQL 交互即可获取结果的单步查询任务，并记录实际执行代码和返回结果。一个 T 只对应一条 SQL，可一次返回多个相关字段，也可使用已知表结构构造 JOIN 或子查询，但不能要求先获取结果、再据此发起另一条查询。不得把查表结构后查询数据、先定位进程再追踪其子进程等多步骤问题合并为一个 T；证据分析和结论判断交给 Expert。首轮每个 E 只创建一个 T，选择当前最优先的调查任务；后续根据执行结果逐轮追加 T，保留历史任务，不预先展开整条调查链。
只规划输入要求的调查目标，不直接执行查询，不将推测或计划当作已验证的实体信息。
# 工作内容
首轮调查：根据输入告警初始化总体目标 G 节点与初始实体 E 节点，并为每个 E 仅创建一个待执行的 T，不一次生成多个任务，后续步骤留待执行结果返回后再规划。仅使用输入中的已知实体信息；实体标识未知时明确标注待确认，并安排识别任务。T 的 objective 应自包含，写清具体实体、已知查询条件、必要范围及一次查询所需返回的字段。若缺少必要实体标识或表结构，当前 T 只负责查询该前置信息，等待真实结果后再创建后续 T，不把依赖未知结果的步骤写入同一任务；尚未执行的 execution_code 和 execution_result 为空。
后续轮次：依据上一轮 Expert 总结和实际执行记录，更新已执行 T 节点相关的字段及任务状态；如需继续调查，使用已获得的真实结果作为明确查询条件，在对应 E 下生成本轮待执行的单步 T；如发现新的可疑实体，提取其已知信息并在对应 G 下创建 E 和调查任务。保留已有节点的 ID、objective、父子关系与历史路径，已结束任务不重开；实际执行代码和结果原样保留，不编造或改写。按执行优先级排列待执行任务；证据尚不足以回答目标时继续安排调查任务。
# 输出格式模板
严格按照以下模板输出完整 ITT，仅输出合法 YAML 列表，不添加解释或模板以外的字段。模板中的目标和实体仅为示例，须替换为本次输入的实际信息。id 全树唯一；仅 T 有 status 字段，实际输出从 todo、done、error 中选择一个值，新建任务为 todo；G 和 E 不输出 status。每个 G 至少包含一个 E；首轮每个 E 恰好包含一个 T，后续可追加 T 并保留历史任务，不增加其他层级。
- id: G1
  objective: "定位告警中恶意进程 evil.exe 的文件位置"
  children:
    - id: E1
      objective: "可疑进程 evil.exe"
      children:
        - id: T1
          objective: "以已知主机名 host-01 和进程名 evil.exe 为条件，一次查询匹配的进程记录，返回可执行文件完整路径、进程 ID 和时间"
          status: todo｜done｜error
          execution_code: ""   # 由其他角色补充实际执行代码；Planner 创建任务时只需置空
          execution_result: "" # 由其他角色补充实际执行结果；Planner 创建任务时只需置空
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
