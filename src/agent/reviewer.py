from __future__ import annotations

from src.schema.itt import review_payload, compact_json
from src.schema.reviewer_output import parse_review, evidence_result
from src.schema.ttt_updates import change_node, walk
from src.schema import TracebackTaskTree

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
from src.workflow.providers import NullReviewerContextProvider, ReviewerContextProvider

logger = logging.getLogger(__name__)


REVIEWER_SYSTEM_PROMPT = """
# 角色定位
你是 Reviewer，负责把当前 task 的实际 executor_result 压缩为可继续查询、可核对的证据，不生成后续建议或最终答案。
# 证据保留规则
仅根据本次返回结果提取事实；task 和 SQL 是查询意图及范围，不能把 WHERE 条件当作已观察到的字段值。
facts 中每个对象保留同一条记录内相关字段的关联，使用结果中的字段名；不要把不同记录的主机、PID、时间等拼成一个事实。
优先保留任务直接结果及后续查询所需的实体标识、PID/GUID、主机、时间及其时区、路径、IP、哈希、关联键。标识符、时间和长数字用带引号字符串，保持大小写、前导零和原始精度，不猜测缺失值。
删除无关列和完全重复的事实，不用“已找到关联进程”替代具体值。不要为缩短 summary 删除 facts 中必要的信息。
存在多个账户、进程或其他候选实体时，summary 概括候选数量和关联范围，不单独突出其中一个作为答案；若当前结果没有区分题目所需角色，在 limitations 说明仍有歧义。
SQL 报错或空结果时 facts 为 []，在 limitations 中说明查询范围或错误；截断、缺失关联键、多候选或省略记录时明确说明限制，不把局部结果说成完整结果。
来源 execution_id、执行状态和截断标记由程序补齐，不自行编造来源。
如果查询返回表结构元数据，每个 facts 对象保留一条 TABLE_NAME、COLUMN_NAME 等实际记录，不合并成 url_columns/account_columns 等数组，也不把表结构说成已发生的事件。原始单元格若是 JSON 对象或数组，用带引号的 JSON 文本保留；不要创建嵌套 facts 值。
# 输出格式
仅输出一个 YAML 对象，必填 summary、facts、limitations，不加围栏或其他字段。
summary 最多 240 字符，只概括本次结果；facts 不受此字符限制，是记录对象列表，每个字段值为字符串、数字、布尔或 null；limitations 是字符串列表，无限制时为 []。
summary 建议控制在 100 字符以内，只写结果类型、记录数量及范围；具体实体值和关联放在 facts，局限放在 limitations，避免在摘要重复这些细节。英文同样按字符计数，不是按单词计数。
summary: "本次查询返回一个关联进程"
facts:
  - host_id: "host-01"
    process_guid: "p-42"
    pid: "0042"
    timestamp: "2026-10-17T08:30:00Z"
limitations: []
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
        tree = TracebackTaskTree.from_dict(ttt_payload)
        user_prompt = compact_json(review_payload(tree, executions[-1]))
        response_text = call_llm(
            self.agent.system_prompt,
            user_prompt,
        )
        review = parse_review(parse_yaml_response(response_text))
        result = evidence_result(review, executions[-1])
        record = executions[-1].to_dict() if hasattr(executions[-1], "to_dict") else executions[-1]
        node_id = record["node_id"]
        node = next(n for n, _ in walk(tree) if n.node_id == node_id)
        updated = change_node(tree, node_id, metadata=dict(node.metadata,
            execution_result=result, execution_result_source="reviewer"))
        self.ttt_store.save_snapshot(updated, updated_by=self.agent.role_name, expected_version=tree.version)
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
