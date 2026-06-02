from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, ExecutionStatus, RoundReview
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

你的输出必须严格使用 YAML，且只能输出以下两种 response_type：
- ROGER
- ROUND_REVIEW

总结要求：
- 总结当前轮已经获取到的关键证据。
- 指出哪些假设被支持，哪些假设仍未验证。
- 指出失败执行、能力缺口或数据缺口。
- 给 Planner 提供下一轮更新 TTT 的建议重点。

输出示例：
```yaml
type: llm_response
from: _reviewer
to:
  - _planner
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: ROUND_REVIEW
findings:
  - 已确认源 IP 存在恶意扫描与暴力破解标签。
  - 尚未确认邮件网关是否存在成功登录记录。
gaps:
  - 缺少目标主机认证日志证据。
  - 当前工具集中没有直接查询某设备审计日志的能力。
recommendations:
  - 下一轮优先验证目标主机在告警时间窗内的成功登录行为。
  - 若仍无日志查询能力，应将相关节点标记为能力缺口并调整 TTT。
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
            EventStatus.EXECUTING.value,
            EventStatus.PLANNED.value,
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
        if not self.ttt_store.all_leaves_terminal(event.event_id, event.current_round):
            return False

        executions = self.storage.list_executions(event.event_id, event.current_round)
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        if latest_ttt is None:
            return False

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.ROUND_REVIEW_STARTED,
            payload={"execution_count": len(executions)},
        )
        review = self._generate_round_review(event, latest_ttt.to_dict(), executions)
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
                "请根据以下事件、TTT 和本轮执行记录，输出 YAML 形式的轮次总结。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(ttt_payload, ensure_ascii=False, indent=2),
                json.dumps(execution_payload, ensure_ascii=False, indent=2),
            ]
        )
        try:
            response_text = call_llm(
                self.agent.system_prompt,
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
            parsed = parse_yaml_response(response_text)
            if parsed:
                findings = tuple(str(item) for item in (parsed.get("findings") or []))
                gaps = tuple(str(item) for item in (parsed.get("gaps") or []))
                recommendations = tuple(str(item) for item in (parsed.get("recommendations") or []))
                if findings or gaps or recommendations:
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
        except Exception:
            logger.exception("Reviewer round review generation failed, using fallback")
        return self._build_fallback_review(event, executions)

    def _build_fallback_review(self, event: Event, executions: list[object]) -> RoundReview:
        findings: list[str] = []
        gaps: list[str] = []
        recommendations: list[str] = []

        if not executions:
            gaps.append("本轮没有生成任何执行记录。")
            recommendations.append("检查 Executor 是否成功领取 TTT 叶子节点。")
        else:
            for execution in executions:
                if execution.execution_status == ExecutionStatus.COMPLETED:
                    findings.append(
                        f"节点 {execution.node_id} 已执行完成，工具为 {execution.tool_name}。"
                    )
                else:
                    gaps.append(
                        f"节点 {execution.node_id} 执行失败，原因：{execution.error_message or 'unknown'}。"
                    )
            if gaps:
                recommendations.append("下一轮优先处理工具能力缺口或补充外部数据源。")
            if not recommendations:
                recommendations.append("根据当前已完成结果继续缩小调查范围。")

        return RoundReview(
            event_id=event.event_id,
            round_id=event.current_round,
            findings=tuple(findings or ["本轮已完成执行结果收集。"]),
            gaps=tuple(gaps),
            recommendations=tuple(recommendations),
            created_by=self.agent.role_name,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

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
