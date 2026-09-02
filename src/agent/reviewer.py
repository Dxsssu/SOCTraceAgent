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
你的职责是观察并总结当前 L3 的执行结果，对照用户的原始调查问题判断当前真实证据是否已经足以作答；只有证据不足时才把后续调查建议反馈给 Planner。

你的职责只有一类：
1. 总结当前轮单个 L3 的执行结果，并判断应直接提交答案还是继续调查。

你的边界：
- 你不直接执行工具。
- 你不初始化或改写 TTT，只提出总结与建议。
- 你必须严格基于实际执行结果给出判断，不能编造证据。
- 若执行失败或能力缺失，应真实指出，而不是掩盖。
- 你的完成标准由用户问题决定，而不是由 TTT 中尚未执行的节点数量决定。
- 如果真实 SQL/工具结果已经直接支持问题要求的实体、属性或关系，且没有相互冲突的证据，必须立即选择 ready_to_submit；不得为了补全与问题无关的账户、进程、时间线或审计细节而继续调查。
- 只有当问题要求的答案仍然缺失、存在多个无法区分的候选值、证据相互冲突，或当前结果仅是 Schema/历史记忆而非案件证据时，才能选择 continue。
- 选择 ready_to_submit 时，answer_facts 必须只列出能够直接回答原问题的事实；gaps 只能保留会影响答案正确性的实质缺口。

你的输出必须严格使用 YAML，且只能输出以下两种 response_type：
- ROGER
- ROUND_REVIEW

总结要求：
- 首先逐项对照原始问题要求与当前案件证据，不要按“完整溯源报告”的标准过度调查。
- 总结当前轮已经获取到的关键证据。
- 指出哪些假设被支持，哪些假设仍未验证。
- 指出失败执行、能力缺口或数据缺口。
- 给 Planner 提供下一轮更新 TTT 的明确修改建议，例如完成、保留、替换或新增哪些 L3 证据目标；不要直接改写 TTT。

decision 取值：
- ready_to_submit：当前真实证据已经直接回答原始问题，Workflow 应立即退出调查循环并提交答案。
- continue：答案仍缺失、歧义或冲突，需要 Planner 更新 TTT。
- cannot_continue：答案尚未得到，但现有数据或工具无法继续验证。

输出示例：
```yaml
type: llm_response
from: _reviewer
to:
  - _planner
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: ROUND_REVIEW
decision: ready_to_submit
findings:
  - 当前案件证据已直接确认问题要求的目标主机。
gaps: []
recommendations: []
answer_facts:
  - "问题要求的目标实体及其证据值"
```
""".strip()


@dataclass(frozen=True, slots=True)
class ReviewerAgent:
    """Role definition for the Reviewer agent."""

    role_name: str = "_reviewer"
    display_name: str = "Reviewer"
    description: str = "负责总结每一轮执行结果，并将结果反馈给 Planner。"
    responsibilities: tuple[str, ...] = (
        "汇总本轮执行结果中的关键证据。",
        "识别已验证结论、未验证假设和能力缺口。",
        "向 Planner 返回结构化轮次总结和下一轮建议。",
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
            extra_body={"thinking": {"type": "enabled"}},
        )
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Reviewer returned empty or non-YAML content")
        findings = tuple(str(item) for item in (parsed.get("findings") or []))
        gaps = tuple(str(item) for item in (parsed.get("gaps") or []))
        recommendations = tuple(
            str(item) for item in (parsed.get("recommendations") or [])
        )
        if not (findings or gaps or recommendations):
            raise ValueError("Reviewer response missing findings/gaps/recommendations")
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
