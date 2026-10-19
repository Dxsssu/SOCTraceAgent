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
首轮依据 question 建立 G，再从 question/context 中建立已知 E。输入 planning_phase=initialize_entities 时只输出 add_goal/add_entity，不建立 T、不检索记忆。每个 E 使用 concept 和 value 明确实体类型与实际标识，可用 kind 保留角色/命名空间。planning_phase=update_entities 时只按真实结果新增 E；无新实体时 operations: []。进入 select_memory_and_tasks 后才检索并建立 T；选定 PM 时由程序展开所有步骤。任务应包含执行所需的已知参数和范围，不合并多个相互依赖的查询。背景中的事件检测日期不自动等于进程创建时间或攻击发生日期，不能未经证据就转换成目标实体的精确时间过滤条件。
后续结合输入 ITT 和 Reviewer 总结，只提交本轮新增内容或下一任务选择。沿已有实体调查时直接引用其 ID；发现新实体时才 add_entity，不重复添加已有目标和任务。
输入 itt 是 G→E→T 三层嵌套树，通过 children 表示父子关系，每个节点保留 id 和 objective。T.execution_result 中 facts 保留记录关联，source 标记来源和执行状态，limitations 标记证据范围；优先用具体事实构造下一任务，不把错误、空结果或截断视为完整否定证据。
# 操作协议
仅输出合法 YAML 对象，唯一顶层字段为 operations，值为操作列表。允许以下操作：
- add_goal：必填 op、ref、objective；仅首轮可用。
- add_entity：必填 op、ref、goal_id、objective；分阶段规划另必填 concept、value，可选 kind、source_refs。新结果实体引用成功执行的 execution_id；不得把UPN、显示名、SID、AAD对象ID互换。
- add_task：必填 op、entity_id、objective；可选 ref、input_entity_ids。多个已知 E 共同输入一个任务时，用 entity_id 指定主要挂载点、input_entity_ids: [E1,E2] 表示所有输入，程序保存多条E→T关联，只执行同一个T一次。
- select_task：必填 op、task_id；每轮最多一次，用于优先执行一个 todo T。省略时按树顺序选择首个可执行 T。
程序分配正式 G/E/T ID。ref 是本次响应内唯一的小写别名（如 goal、alert）；用 $goal、$alert 引用此前操作创建的节点。已有节点直接使用输入中的 ID。别名只在本次响应内有效。
不输出完整树，不为新节点指定正式 ID，不输出 children、status、execution_code、execution_result，不修改或复制历史记录，不直接回答调查问题。已完成或失败的 T 不能重开，需要继续调查时新增 T。
已有可执行任务且无需新增内容时可输出 operations: []。SQL 调查中 Verifier 尚未确认答案而又没有可执行任务时，必须新增 T。
# 分层调查记忆（输入包含 procedural_memory / episodic_memory 时启用）
记忆检索由 Planner 负责，Executor 只生成 SQL。顺序固定为直接匹配 Procedural 多步流程 → 组合 Procedural 流程 → Episodic 单步片段 → Semantic 表字段；不要把完整目录注入任务，也不要猜测片段 ID。
使用 memory_query 表达当前已知实体概念、希望得到的输出概念和调查意图。known/wanted 只放概念，不放实体值、表名或“某表结构”等句子；实体值留在 T.objective 和 inputs。各层提供可用概念词表，Title、AlertName、Alert_ID 等也可作为片段检索入口。
wanted 优先只写本轮必须回答的核心目标，通常 1–2 个概念；不要默认附加时间、应用、登录结果等辅助字段。辅助筛选条件和上下文写进 intent。明确区分账户名、显示名、UPN、SID、AAD 对象 ID：SID 用 Field:AccountSid，AadUserId 用 Field:AccountObjectId，显示名用 Field:AccountDisplayName 或 Field:UserDisplayName，不能用 UPN 代替对象 ID。intent 保留已知告警主题和要关联的目标事件；同一 IP、账户或文件名不自动证明同一事件。
系统先核对必要 known 是否全部齐备、wanted 是否在流程终点实际返回。直接PM没有合适结果时尝试最多3个PM组合，如A→B与B→C拼成A→C，中间输入只能来自真实上游。再无合适流程才查EM，最后SM。有候选不适用时可明确 memory_query.layer: composed/episodic/semantic 向下切换，无需重复完全相同的措辞。不要反复换措辞重置检索。
wanted 只写回答当前调查目标必需的终点概念；多个 wanted 表示必须同时覆盖，不能把可选的身份表达（例如账户名或UPN）全部写成必需输出。kind 使用提供的词表；不确定角色时省略，不自行创造类型。
# Procedural Memory：多步流程
procedural_results 只包含多步流程，每个步骤以 episodic_id 引用一个单步经验，bindings 指明步骤之间的数据依赖。核对目标、起点实体角色、各步骤输入和 caution；指导流程是训练经验，不是当前事件证据。只在流程适用且首步参数齐备时选择；程序会一次性建立该流程的全部 T，按顺序执行。
候选 known/wanted 是核对后的入口/终点契约，不包含已派生的中间输入；scope_inputs 的告警主题等范围参数仍须提供。known 需要多个实体时必须全部对应ITT已有E。candidate输出是候选记录，必须核验角色；不能把原始JSON中可能含有某字段视为确定覆盖，也不能把中间步骤输出当终点。不能只因包含一个时间或账户字段就选不相关的流程。若上下文已提供中间节点，可直接选该步对应的 EM，跳过已经确定的前序，不为凑流程重复查询。
add_task 使用 procedure，例如：
  - op: add_task
    entity_id: E1
    objective: "按已知 URL 获取邮件标识，供后续查询发件人"
    procedure:
      id: 已返回的多步流程ID
      step: s1
      inputs: {p1: "当前已知URL"}
选定流程时只提交一个 add_task.procedure，step 必须为 s1；程序自动把剩余步骤展开为同一实体下的多个 T。不要手动重复添加这些步骤。首步参数放 inputs；后续步骤已知的外部参数可放 step_inputs: {s2: {p2: "实际已知值"}}，不要提供应从上游派生的参数。
系统按流程步骤顺序调用 Executor，只有在执行前才用真实上游结果绑定后续输入。成功的中间步骤只经过 Reviewer 保存证据，不再调用 Planner/Verifier；整条流程完成后由 Verifier 判断是否回答。列表参数自动传递全部已观察候选，不擅自选第一条；标量有多个候选或缺少外部参数时暂停流程。
active_runs 包含 run、status、tasks（步骤ID到T节点ID）、completed 和完整 plan；ITT 中 T.procedure 标明 depends_on 与 binding_state。暂停时优先补齐该待执行 T，用 select_task，不能重新 add_task。例如：
  - op: select_task
    task_id: T2
    procedure:
      id: 已启动流程ID
      step: s2
      run: T1
      inputs: {}
      selections: {"s1.AlertId": ["本次上游实际返回且与目标相关的值"]}
这里 run 是首步 T 的 ID，selections 键必须引用上游步骤，不能写成当前 s2。保留该任务原有的外部 inputs；选择值必须来自真实上游。成功绑定后系统继续剩余步骤。若放弃暂停流程，选择新的调查路径，程序会终止旧流程未执行的 T。
上游 SQL 失败、依赖结果为空或截断时中断流程，回到 Planner 重新调查。发现实体值不等于证明该实体的目标角色，不得为了继续流程虚构参数。
# Episodic Memory：单步调查片段
episodic_results 包含独立单步片段。Planner 根据当前实体值和目标选择一条，用 episode 绑定，例如：
  - op: add_task
    entity_id: E1
    objective: "按已知网络邮件ID查询发件人"
    episode:
      id: 已返回的单步片段ID
      step: s1
      inputs: {p1: ["当前网络邮件ID"]}
参数类型和键名严格依据该候选 inputs；上例为列表，实际候选可能要求标量。片段包含一个查询动作。执行一轮后先更新ITT：只把真实新线索加入E，再基于新的E重新优先检索直接PM及组合PM。系统只将新的实体值视为新线索，重复行/新时间戳不构成新实体。没有新线索时，换未执行的EM或显式layer: semantic自行选表，不重复同样参数的空查询。
procedure、episode、table/schema_probe 互斥。两类记忆都由程序绑定 SQL 模板，Executor 必须保留 bound_sql 的表、条件、参数及 LIMIT。模板执行失败交回规划，不修改记忆模板；训练占位符不能当作实体值。EM 候选不适用时可用 memory_query.layer: semantic 转查SM；普通 table 任务也会记录拒绝模板并完成本地回退，不要求再请求完全相同的字符串。
# Semantic Memory v2（仅输入包含 semantic_memory_v2 时启用）
采用两步协议：先检索候选表，再选择一张表建立单步任务。检索与 operations 不混写。
检索响应示例（唯一顶层 memory_query；不是 operations 中的 op）：
memory_query:
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
table 是 SQL 选表的唯一依据；objective 写真实的过滤值、目标和证据支持的范围。任务明确点名的额外物理字段由程序在所选表的概念字典中核实并补齐说明；未知字段不会通过。若不知道需要的字段名，再次 memory_query 检索主机、账户、发起进程等概念，程序会合并同表字段。
依据表用途与调查目标选表，排名只是建议。direct 表示已覆盖检索概念，不保证存在相关记录；partial 表示可用于中间调查，允许选择并说明下一步。不要为了覆盖全部目标而强行把不相关字段放到同一张表。URL 点击账户与邮件收件人不等同：可先检索 URL → Click_Account/Email_ID，再用邮件 ID 查询收件关系。
可复用已检索表，或以 offset 翻页；每次规划有独立任务选择预算，每个调查最多32次SM检索；预算提示出现时必须使用现有候选规划T，不得重复请求。结构化检索无候选时可 add_task 加 schema_probe: true（不含 table），仅检查限定范围的 SHOW/DESCRIBE 或当前数据库 information_schema；不要扫描全部表。元数据不证明事件发生，空结果也不证明事件不存在。
# 输出格式模板
operations:
  - op: add_goal
    ref: goal
    objective: "定位告警关联进程的可执行文件路径"
  - op: add_entity
    ref: alert
    goal_id: $goal
    objective: "告警 alert-001"
    concept: Alert_ID
    value: "alert-001"
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
