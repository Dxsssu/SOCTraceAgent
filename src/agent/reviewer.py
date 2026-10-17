from __future__ import annotations

from src.schema.investigation import task_input
from src.schema.itt import review_payload
from src.schema.reviewer_output import parse_review
from src.schema import TracebackTaskTree

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
# 角色定位
你是 Reviewer，负责总结当前轮次任务的实际执行结果，不生成后续建议。
# 输出格式
仅输出 summary 一个 YAML 字符串字段，不加解释、建议、围栏、列表或其他字段。
summary 最多 80 字符；汉字、字母、数字、标点和空格均计数。
summary: "当前执行结果总结"
""".strip()


@dataclass(frozen=True, slots=True)
class ReviewerAgent:
    """Role definition for the Reviewer agent."""

    role_name: str = "_reviewer"
    display_name: str = "Reviewer"
    description: str = "负责总结当前轮次任务的实际执行结果。"
    responsibilities: tuple[str, ...] = (
        "总结当前轮单个 L3 的实际执行结果，不生成后续建议。",
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
        user_prompt = json.dumps({
            **task_input(event),
            **review_payload(TracebackTaskTree.from_dict(ttt_payload), executions[-1]),
        }, ensure_ascii=False)
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
        )
        review = parse_review(parse_yaml_response(response_text))
        return RoundReview(
            event_id=event.event_id,
            round_id=event.current_round,
            findings=(review["summary"],),
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
