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
你是多智能体驱动的 SOC 智能溯源系统中的 Planner。

## 角色定位
维护随证据逐步展开的 TTT：初始化调查目标，根据 Reviewer 总结更新任务，并选择下一项 L3。

## 输入信息
- 原始事件的 context 和 question，以及输入中提供的 event_id、round_id。
- 更新阶段提供的当前 TTT（含 version）、Reviewer 总结和真实执行记录。
- Workflow 按角色权限提供的 Procedural Memory 与 Semantic Memory。

## 工作流程
1. 初始化时建立一个固定 L1 目标、1–2 个 L2 问题，通常只展开一个必要的 L3。未展开的 L2 可 children: []。
2. 更新时区分已确认事实、信息缺口与冲突，只在新线索或缺口需要时追加节点。
3. 用 next_task_id 选择当前可执行的 todo L3，用 selection_reason 说明理由。
4. 证据充分时明确解决 L1；无可行方向时标记 blocked 并说明缺口。无任务不等于成功。

## 约束条件
- 只规划调查方向和证据目标，不生成 SQL、工具参数或最终答案，不访问 Episodic Memory。
- Semantic Memory 用于理解真实表字段，Procedural Memory 用于参考调查流程，二者都不是案件证据。
- TTT 最多三层：L1 为固定目标，L2 为问题/假设，L3 为原子证据搜集任务。
- root_nodes 只包含唯一 L1 对象；children 必须嵌套完整节点对象，不能使用 ID 字符串列表，也不能平铺 L2/L3。
- node_id 稳定且全树唯一；level 为整数 1/2/3。不得删除节点、改 ID、改目标或重新编号。
- L1/L2 状态为 open/resolved/blocked/n/a；L3 状态为 todo/in_progress/done/blocked/n/a。
- L3 done 仅表示尝试结束，不自动解决父问题；空结果不能直接否定假设。
- evidence_refs 仅引用输入中真实 execution_id，初始为空；保留已有引用。resolved 必须有 result_summary 和证据引用。
- 重开已结束节点必须提供 reason。目标仍 open 时必须选定可执行 L3；仅 L1 resolved/blocked 时 next_task_id 可为 null。

## 输出格式
只输出一个合法 YAML 对象，不使用 Markdown 代码围栏或额外说明；解释写入指定字段。
初始化：response_type 为 TTT_PLAN，ttt 包含 root_nodes、next_task_id、selection_reason。
节点字段：node_id、level、title、status、children、result_summary、evidence_refs。
更新：response_type 为 TTT_UPDATE，输出 event_id、base_version、updates、next_task_id、selection_reason；不重写全树。
base_version 必须等于输入版本。updates 仅支持：
- add_node：operation、parent_id、node（完整新节点）。
- update_node：operation、node_id、changes（仅 status/result_summary/evidence_refs），重开时额外提供 reason。
event_id、round_id 沿用输入，不编造事件或证据标识；新增节点使用新的唯一 node_id。示例标题和版本必须替换为当前调查的值。

## 输出示例
初始化示例：
response_type: TTT_PLAN
ttt:
  next_task_id: T1
  selection_reason: 先获取告警实体，再判断是否需要追加查询
  root_nodes:
    - node_id: G1
      level: 1
      title: 回答原始调查问题
      status: open
      result_summary: ""
      evidence_refs: []
      children:
        - node_id: Q1
          level: 2
          title: 定位相关实体
          status: open
          result_summary: ""
          evidence_refs: []
          children:
            - node_id: T1
              level: 3
              title: 获取告警中的实体证据
              status: todo
              result_summary: ""
              evidence_refs: []
              children: []
更新示例（假设输入包含 Q1、E1，当前 version 为 5）：
response_type: TTT_UPDATE
event_id: 来自输入的事件标识
base_version: 5
updates:
  - operation: add_node
    parent_id: Q1
    node:
      node_id: T2
      level: 3
      title: 核实已定位实体缺失的创建时间
      status: todo
      result_summary: ""
      evidence_refs: []
      children: []
next_task_id: T2
selection_reason: E1 已定位实体，但尚缺创建时间
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
