from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time

from src.agent.llm import call_llm
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, RoundReview
from src.schema.event import utc_now
from src.storage import SQLiteStorage


logger = logging.getLogger(__name__)


REVIEWER_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Reviewer。
你的职责是观察并总结一轮执行结果，识别当前证据支持了什么、缺失了什么，并把这些结论反馈给 Planner。

你的职责只有一类：
1. 总结当前轮的执行结果，并形成供 Planner 下一轮更新 TTT 的反馈。

你的边界：
- 你不直接执行工具。
- 你不初始化或改写 TTT，只提出总结与建议。
- 你必须严格基于实际执行结果给出判断，不能编造证据。
- 若执行失败或能力缺失，应真实指出，而不是掩盖。

总结要求：
- 总结本轮工具执行结果。
- 总结目前已经收集到的结论。
- 只给 Planner 1 条下一轮 TTT 调整建议，不要给多条建议清单。
- 如果当前步骤已经成功执行并返回了所需结果，且没有出现新的关键证据或新的调查方向，应明确建议 Planner 尽量保持 TTT 不变，沿现有节点继续向下执行。
- 如果合适，可以自然地使用简洁 Markdown（如小标题、列表、加粗）来提高可读性，但不要为了格式牺牲判断质量。
""".strip()


@dataclass(frozen=True, slots=True)
class ReviewerAgent:
    """Role definition for the Reviewer agent."""

    role_name: str = "_reviewer"
    display_name: str = "Reviewer"
    description: str = "负责总结每一轮执行结果，并将结果反馈给 Planner。"
    responsibilities: tuple[str, ...] = (
        "总结本轮工具执行结果。",
        "总结已经收集到的结论。",
        "给出 1 条 TTT 调整建议。",
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
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = ReviewerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
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
        if self.storage.get_round_review(event.event_id, event.current_round) is not None:
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
            review = self._generate_round_review(event, latest_ttt.to_dict(), executions)
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
    ) -> RoundReview:
        execution_payload = [execution.to_dict() for execution in executions]
        user_prompt = "\n".join(
            [
                "请根据以下事件、TTT 和本轮执行记录，直接输出一段轮次总结内容。",
                "如果你觉得合适，可以自然使用简洁 Markdown 来提升可读性；不必强行套格式。",
                "不要输出 YAML 或 JSON。",
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
        summary_text = (response_text or "").strip()
        if not summary_text:
            raise ValueError("Reviewer returned empty content")
        return RoundReview(
            event_id=event.event_id,
            round_id=event.current_round,
            summary_text=summary_text,
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
