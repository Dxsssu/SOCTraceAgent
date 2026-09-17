from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, RoundReview
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.workflow.context import NullReviewerContextProvider, ReviewerContextProvider

logger = logging.getLogger(__name__)


REVIEWER_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer。

## 角色定位
审查当前 L3 的执行结果，对照原始问题判断证据是否足以作答，并向 Planner 提供事实、缺口和后续建议。

## 输入信息
- 原始事件的 context 和 question。
- 当前 TTT、真实执行记录及其 execution_id、查询状态和结果限制。
- Workflow 按角色权限提供的 Semantic Memory。

## 工作流程
1. 区分执行错误、空结果和有效记录，提取有真实结果支持的事实。
2. 核对原始问题要求的实体、属性及关联关系，说明缺失信息、冲突和截断限制。
3. 判断应继续调查、提交答案，还是因无可行调查方向而停止，并给出理由。

## 约束条件
- Semantic Memory 只用于解释表和字段，不是案件证据；不使用 Procedural Memory 或 Episodic Memory。
- 不编造记录、实体值或证据引用；只引用输入中真实 execution_id。
- L3 done 不等于问题解决；查询成功不等于结论正确；空结果不能直接否定假设。
- 不修改 TTT，不生成 SQL 或工具参数。后续建议描述需要查明的事实，由 Planner 安排任务。
- ready_to_submit 需要原始问题已有充分证据且无未解释的关键冲突；continue 表示仍有缺口且存在调查方向；cannot_continue 表示仍缺证据但已无可行方向。

## 输出格式
只输出一个合法 YAML 对象，不使用 Markdown 代码围栏或额外说明；解释写入指定字段。
response_type 固定为 ROUND_REVIEW。
decision 为 ready_to_submit、continue 或 cannot_continue。
reasonings 为简短字符串；findings、gaps、recommendations、answer_facts 均为顶层字符串列表，无内容时用 []。
不要把 findings/gaps/recommendations 的 YAML 文本嵌入 reasonings 字符串。
findings 记录已确认事实；gaps 记录缺失或冲突；recommendations 记录后续方向；answer_facts 记录可用于最终答案的已确认内容。

## 输出示例
response_type: ROUND_REVIEW
decision: continue
reasonings: 已定位相关进程，但尚缺原始问题要求的创建时间
findings:
  - E1 提供了相关进程的标识和命令行
gaps:
  - 尚无进程创建时间的证据
recommendations:
  - 查询该进程的创建记录并核对设备与时间范围
answer_facts: []
""".strip()


@dataclass(frozen=True, slots=True)
class ReviewerAgent:
    """Role definition for the Reviewer agent."""

    role_name: str = "_reviewer"
    display_name: str = "Reviewer"
    description: str = "负责总结当前 L3 的执行结果，并判断提交答案或继续调查。"
    responsibilities: tuple[str, ...] = (
        "总结当前轮单个 L3 的执行结果，并判断应直接提交答案还是继续调查。",
    )
    allowed_response_types: tuple[str, ...] = (
        "ROGER",
        "ROUND_REVIEW",
    )
    system_prompt: str = REVIEWER_SYSTEM_PROMPT


class ReviewerRuntime:
    """Reviewer 角色运行时实现。"""

    def __init__(
        self,
        *,
        storage: SQLiteStorage | None = None,
        ttt_store: TTTStore | None = None,
        bus: SQLiteMessageBus | None = None,
        context_provider: ReviewerContextProvider | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = ReviewerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.context_provider = context_provider or NullReviewerContextProvider()
        self.poll_interval = poll_interval
        self.running = False

    def run_once(self) -> bool:
        did_work = False
        events = self.storage.list_events_by_status(
            EventStatus.REVIEWING.value,
        )
        for event in events:
            if self.process_event(event):
                did_work = True
        return did_work

    def run_forever(self) -> None:
        self.running = True
        logger.info("Reviewer runtime started")
        while self.running:
            try:
                did_work = self.run_once()
                if not did_work:
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Reviewer runtime interrupted")
                self.running = False
            except Exception:
                logger.exception("Reviewer runtime loop failed")
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self.running = False

    def process_event(self, event: Event) -> bool:
        if (
            self.storage.get_round_review(event.event_id, event.current_round)
            is not None
        ):
            return False
        executions = self.storage.list_executions(event.event_id, event.current_round)
        if not executions:
            return False
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        if latest_ttt is None:
            return False
        workflow_context = self.context_provider.build(
            event,
            ttt=latest_ttt,
            executions=executions,
        )

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.ROUND_REVIEW_STARTED,
            payload={"execution_count": len(executions)},
        )
        try:
            review = self._generate_round_review(
                event,
                latest_ttt.to_dict(),
                executions,
                workflow_context=workflow_context,
            )
        except Exception as exc:
            logger.exception("Reviewer round review generation failed")
            self._fail_event(event, f"Reviewer round review generation failed: {exc}")
            return False
        self.storage.save_round_review(review)
        replanning_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=EventStatus.REPLANNING,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(replanning_event)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.ROUND_REVIEW_CREATED,
            payload=review.to_dict(),
            to_role=RoleName.PLANNER,
        )
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.HANDOFF_TO_PLANNER,
            payload={"text": "round_review_ready_for_replanning"},
            to_role=RoleName.PLANNER,
        )
        return True

    def _generate_round_review(
        self,
        event: Event,
        ttt_payload: dict[str, object],
        executions: list[object],
        *,
        workflow_context: dict[str, Any] | None = None,
    ) -> RoundReview:
        execution_payload = [execution.to_dict() for execution in executions]
        user_prompt = "\n".join(
            [
                "请根据以下事件、TTT 和本轮执行记录，输出 YAML 形式的轮次总结。",
                "Workflow 仅提供 Semantic Memory 用于解释表字段含义；不得将 Schema 描述当作当前事件证据。",
                json.dumps(
                    {"workflow_memory_context": workflow_context or {}},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(ttt_payload, ensure_ascii=False, indent=2),
                json.dumps(execution_payload, ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Reviewer returned empty or non-YAML content")
        findings = tuple(str(item) for item in (parsed.get("findings") or []))
        gaps = tuple(str(item) for item in (parsed.get("gaps") or []))
        recommendations = tuple(
            str(item) for item in (parsed.get("recommendations") or [])
        )
        reasonings = str(parsed.get("reasonings") or "").strip()
        if reasonings and not (findings or gaps or recommendations):
            findings = (reasonings,)
        if not (findings or gaps or recommendations):
            raise ValueError(
                "Reviewer response missing reasonings or legacy review fields"
            )
        return RoundReview(
            event_id=event.event_id,
            round_id=event.current_round,
            findings=findings,
            gaps=gaps,
            recommendations=recommendations,
            created_by=self.agent.role_name,
            created_at=utc_now(),
            updated_at=utc_now(),
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
        payload: dict[str, object],
        to_role: RoleName | None = None,
    ) -> None:
        self.bus.publish(
            MessageEnvelope(
                event_id=event_id,
                round_id=round_id,
                from_role=RoleName.REVIEWER,
                to_role=to_role,
                message_type=message_type,
                payload=payload,
            )
        )


def run_reviewer(poll_interval: float = 5.0) -> None:
    ReviewerRuntime(poll_interval=poll_interval).run_forever()


__all__ = [
    "REVIEWER_SYSTEM_PROMPT",
    "ReviewerAgent",
    "ReviewerRuntime",
    "run_reviewer",
]
