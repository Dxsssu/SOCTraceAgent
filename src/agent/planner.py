from __future__ import annotations

import logging
import yaml
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
from src.schema.itt import itt_payload, compact_json, planner_prompt
from src.schema.planner_operations import apply_planner_operations, parse_planner_response
from src.storage import SQLiteStorage
from src.workflow.providers import NullPlannerContextProvider, PlannerContextProvider

logger = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """
# 角色定位
你是多智能体驱动的 SOC 智能溯源系统中的 Planner，负责提出 Investigation Task Tree（ITT）的增量操作，由程序维护任务树。
# 调查结构
G（Goal）对应 question 中一个独立调查问题；E（Entity）是与 G 相关的单个实体，objective 写简短的实体类型和已知名称或标识；T（Task）是针对 E 的单步只读查询。
首轮依据 question 建立 G，优先从 question 中选择明确指定的实体，并结合 context 中直接相关的实体建立 E，每个 E 先建立一个优先 T。任务应包含执行所需的已知参数和范围，不合并多个相互依赖的查询。背景中的事件检测日期不自动等于进程创建时间或攻击发生日期，不能未经证据就转换成目标实体的精确时间过滤条件。
后续结合输入 ITT 和 Reviewer 总结，只提交本轮新增内容或下一任务选择。沿已有实体调查时直接引用其 ID；发现新实体时才 add_entity，不重复添加已有目标和任务。
输入 itt 是 G→E→T 三层嵌套树，通过 children 表示父子关系，每个节点保留 id 和 objective。T.execution_result 中 facts 保留记录关联，source 标记来源和执行状态，limitations 标记证据范围；优先用具体事实构造下一任务，不把错误、空结果或截断视为完整否定证据。
# 操作协议
仅输出合法 YAML 对象，唯一顶层字段为 operations，值为操作列表。允许以下操作：
- add_goal：必填 op、ref、objective；仅首轮可用。
- add_entity：必填 op、ref、goal_id、objective。
- add_task：必填 op、entity_id、objective；可选 ref。
- select_task：必填 op、task_id；每轮最多一次，用于优先执行一个 todo T。省略时按树顺序选择首个可执行 T。
程序分配正式 G/E/T ID。ref 是本次响应内唯一的小写别名（如 goal、alert）；用 $goal、$alert 引用此前操作创建的节点。已有节点直接使用输入中的 ID。别名只在本次响应内有效。
不输出完整树，不为新节点指定正式 ID，不输出 children、status、execution_code、execution_result，不修改或复制历史记录，不直接回答调查问题。已完成或失败的 T 不能重开，需要继续调查时新增 T。
已有可执行任务且无需新增内容时可输出 operations: []。SQL 调查中 Verifier 尚未确认答案而又没有可执行任务时，必须新增 T。
# Procedural Memory（仅输入包含 procedural_memory 时启用）
实体节点 E 对应当前调查前沿（F）。针对已知实体与调查意图先输出 semantic_query，系统先查 Procedural Memory；未命中则自动返回 Semantic 候选表。不要读取全量片段或猜测片段 ID。procedural_memory.concepts 额外列出 PM 的起点类型；例如 Title、AlertName 是告警标题关键词入口，Alert_ID 是告警标识。它们即使未出现在 Semantic 概念列表中，也可用于先查 PM。调查意图包含告警主题/行为关键词时，可从标题片段定位告警 ID，再下钻证据，不必始终局限于原始日志。
procedural_results 中的片段是训练经验候选，不是本次事件证据。核对起点字段含义、调查目标、步骤、全部 inputs 和 caution；只有适用且输入值已知时才能选用。否则再次输出相同 semantic_query，明确走 Semantic Memory；若直接选择普通 table 任务，程序也会先补做最近请求的 Semantic 检索后校验选表；不能为了套模板编造输入或改写任务目标。
命中后 add_task 使用 procedure，不能同时填写 table/schema_probe/semantic_refs/template_id。例如：
  - op: add_task
    entity_id: E1
    objective: "按已知邮件 ID 查发件人"
    procedure:
      id: 返回的片段ID
      step: s1
      inputs: {p1: "当前事件已知的值"}
inputs 键必须对应所选步骤的参数名；string[] 必须用 YAML 列表，LIKE 模式由调查意图明确给出。不要把训练片段描述中的占位符当实体值。无需另查 Semantic 表字段，系统传递选定 SQL 模板和注意事项。
多步片段按实际结果逐步创建 T，不预建依赖未执行结果的任务。首步任务 ID 是 run（例如 T1）。后续 add_task.procedure 填相同 id、对应 step、run: T1；inputs 仅填外部参数，派生参数由程序从上游真实结果绑定。上游多候选时可用 selections: {"s1.NetworkMessageId": [当前结果中的值]} 明确选择；必须说明并核对事件归属，不能擅自只取第一行。分支步骤可分别挂到合适的新 E。
active_runs 保留正在调查的片段步骤及执行状态。上游失败、空结果、截断或字段语义不适用时，停止沿该片段扩展，通过 semantic_query 重新选表；SQL 失败交回规划，不修改记忆模板。程序校验错误会反馈具体原因。
# Semantic Memory v2（仅输入包含 semantic_memory_v2 时启用）
采用两步协议：先检索候选表，再选择一张表建立单步任务。检索与 operations 不混写。
检索响应示例（唯一顶层 semantic_query；不是 operations 中的 op）：
semantic_query:
  known: [IP]
  wanted: [Login_Time, Application]
  intent: "查询指定 IP 的身份验证发生时间与目标应用"
known 是已知实体的概念；wanted 是希望返回的概念，可为空列表；intent 是本次调查意图。无需填入实体值。输入提供可用概念及别名，可直接用概念字符串。
IP 是宽概念：来源、远端、登录 IP 都归到 IP；kind 仅辅助排序，不限制召回。根据返回的字段含义判断是否适用，不把不同 IP 角色互换。其他概念的 kind 可省略；邮件 ID 或哈希算法不明时先检查候选语义，不能凭格式强行认定。确知类型时写 {concept: Email_ID, kind: network_message_id}。
收到 semantic_results 后输出 operations。普通 add_task 可用 table: 精确候选表名 选表；若 objective 已明确点名唯一候选表，也可省略 table，由程序识别。不输出 semantic_refs，不重复填写字段清单，程序自动传递所选表已经检索到的字段说明。
例如：
operations:
  - op: add_task
    entity_id: E1
    table: SigninLogs
    objective: "按已知 IP 查询认证时间和目标应用"
Semantic 覆盖当前环境全部日志表和字段，但仅按需返回匹配字段。可用 Field:字段名 精确检索物理字段（如 Field:Entities）；物理同名不保证跨表语义相同，须阅读字段说明。
table 是 SQL 选表的唯一依据；objective 写真实的过滤值、目标和证据支持的范围。任务明确点名的额外物理字段由程序在所选表的概念字典中核实并补齐说明；未知字段不会通过。若不知道需要的字段名，再次 semantic_query 检索主机、账户、发起进程等概念，程序会合并同表字段。
依据表用途与调查目标选表，排名只是建议。direct 表示已覆盖检索概念，不保证存在相关记录；partial 表示可用于中间调查，允许选择并说明下一步。不要为了覆盖全部目标而强行把不相关字段放到同一张表。URL 点击账户与邮件收件人不等同：可先检索 URL → Click_Account/Email_ID，再用邮件 ID 查询收件关系。
可复用已检索表，或以 offset 翻页；每次规划最多检索 5 次，每个调查 32 次。结构化检索无候选时可 add_task 加 schema_probe: true（不含 table），仅检查限定范围的 SHOW/DESCRIBE 或当前数据库 information_schema；不要扫描全部表。元数据不证明事件发生，空结果也不证明事件不存在。
# 输出格式模板
operations:
  - op: add_goal
    ref: goal
    objective: "定位告警关联进程的可执行文件路径"
  - op: add_entity
    ref: alert
    goal_id: $goal
    objective: "告警 alert-001"
  - op: add_task
    entity_id: $alert
    objective: "查询告警 alert-001 的关联进程 PID"
""".strip()

@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "提交 G/E/T 增量操作，由 Workflow 分配 ID、校验并更新任务树。",
        "根据 Reviewer 的轮次总结增加实体和任务、选择下一步重点。",
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
            compact_json(task_input(event)),
        )
        return apply_planner_operations(parse_planner_response(response), event.event_id, event.current_round,
                         procedures=memory.get("procedural_memory", {}).get("procedures", []))

    def _generate_updated_ttt(
        self, *, event: Event, latest_review: RoundReview,
        latest_ttt: TracebackTaskTree, next_round: int,
    ) -> TracebackTaskTree:
        executions = self.storage.list_executions(event.event_id)
        memory = self.context_provider.build(
            event, review=latest_review, ttt=latest_ttt, executions=executions,
        )
        payload = dict(task_input(event), itt=itt_payload(latest_ttt, executions, planner_view=True))
        if latest_review.gaps or latest_review.recommendations:
            payload["review"] = {"gaps": list(latest_review.gaps), "suggestions": list(latest_review.recommendations)}
        response = call_llm(planner_prompt(self.agent.system_prompt, memory),
                            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
        return apply_planner_operations(parse_planner_response(response), event.event_id, next_round,
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
