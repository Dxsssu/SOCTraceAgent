from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.longterm_memory import (
    FactualMemoryDocument,
    FactualMemoryLibrary,
    ProceduralMemoryDocument,
    ProceduralMemoryLibrary,
)
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, RoundReview, TTTNode, TTTNodeStatus, TracebackTaskTree
from src.schema.ttt import coerce_ttt_node_status
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.tools import list_registered_tools


logger = logging.getLogger(__name__)

IP_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOSTLIKE_PATTERN = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9._-]{2,}\b")
ACCOUNTLIKE_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9._@-]{1,}\b")
MAX_L2_REPLAN_ATTEMPTS = 3


PLANNER_SYSTEM_PROMPT = """
你是多智能体驱动的 SOC 智能溯源系统中的 Planner。
你是整个溯源流程的总规划者，负责接收告警、进行初始研判，并维护共享黑板 TTT（Traceback Task Tree）。

你的职责只有两类：
1. 在收到新告警时，初始化 TTT。
2. 在每一轮结束后，根据 Reviewer 返回的总结更新 TTT。

你不直接执行工具，不伪造日志，不编造企业中不存在的能力。

你的输出必须严格使用 YAML，且只能输出以下三种 response_type：
- ROGER
- TTT_PLAN
- TTT_UPDATE

TTT 约束：
- TTT 必须是完整快照，而不是增量片段。
- TTT 必须严格三层：L1（战略层） -> L2（战术层） -> L3（执行层）。
- 一个事件通常包含多个调查大方向；只要存在多个明显方向，就必须拆成多个 L1 根节点。
- 每个 L1 只描述一个独立的大方向，不允许把真实性确认、IP 风险、目标影响、横向扩散等不同方向都塞进同一个总 L1。
- L2 必须描述围绕 L1 需要回答的问题，尽量写成问题句或明确的问题语义。
- L3 必须描述为了回答该问题需要执行的查询、取证或验证动作。
- L3 必须尽量携带完整实体信息，避免使用“该 IP / 该主机 / 该账号 / 相关日志”这类模糊指代。
- 如果输入中已经出现明确实体，例如 IP、主机名、账号、目标系统，L3 标题必须直接写出这些实体。
- 如果实体尚未完全明确，也要写成“查询告警中的源 IP 基础情报”这类不伪造值但不含模糊代词的表达。
- 仅允许三层，禁止出现第 4 层及以上层级。
- 一个 L2 可以对应一个或多个 L3。
- L3 必须是叶子节点，children 必须为空数组。
- node_id 必须使用纯数字分层编号，如 `1`、`1-2`、`1-2-3`。
- 更新 TTT 时必须优先做最小改动，避免无必要重写整棵树。
- 已经完成的节点应视为冻结节点，除非有强证据，否则不要改写其语义。
- 如果本轮执行成功且 review 没有明确提出新的关键证据、新缺口或新调查方向，默认只更新状态和最小必要调整，不要大幅改写 TTT 结构。
- 如果当前步骤的工具执行成功，并且已经返回了该步骤需要的结果，应优先沿着现有 TTT 继续向下执行，而不是为了形式上的更新去重写 TTT。

输出示例：
```yaml
type: llm_response
from: _planner
event_id: "{ 来自输入 }"
round_id: "{ 来自输入 }"
response_type: TTT_PLAN
response_text: 对告警的初始分析。
ttt:
  event_id: "{ 来自输入 }"
  round_id: "{ 来自输入 }"
  root_nodes:
    - node_id: "1"
      title: "方向一：确认告警真实性"
      status: todo
      children:
        - node_id: "1-1"
          title: "问题1.1：邮件网关是否在告警时间窗内出现来自 11.22.33.44 的真实登录尝试？"
          status: todo
          children:
            - node_id: "1-1-1"
              title: "查询告警时间窗内邮件网关与 11.22.33.44 相关的认证日志"
              status: todo
              children: []
    - node_id: "2"
      title: "方向二：评估源 IP 风险"
      status: todo
      children:
        - node_id: "2-1"
          title: "问题2.1：11.22.33.44 是否具备明确恶意情报或异常信誉？"
          status: todo
          children:
            - node_id: "2-1-1"
              title: "查询 11.22.33.44 的威胁情报标签与信誉信息"
              status: todo
              children: []
```
""".strip()


PLANNER_ANALYSIS_SYSTEM_PROMPT = """
你是 SOC 多智能体系统中的 Planner 分析助手。
你的任务是在初始化 TTT 之前，先对安全告警进行系统分析，输出一段简洁、清晰、方便阅读的分析文本。

你必须基于输入中的事件内容进行合理分析，不能编造不存在的日志、资产、能力或调查结果。
如果合适，可以自然地使用简洁 Markdown（如小标题、列表、加粗）提升可读性，但不要为了格式牺牲内容本身。
""".strip()


PROCEDURAL_MEMORY_SELECTION_PROMPT = """
你是 SOC Procedural Memory 匹配器。
你的任务是根据当前事件和 Planner 的初步分析，从候选 procedural memory 文档摘要中选择最匹配的一篇。

你只能返回 YAML，且只能包含一个字段：
- document_id: 候选文档的 document_id；如果没有合适文档则返回空字符串
""".strip()


TTT_RETRY_CORRECTION_PROMPT = """
上一次回复没有返回可解析的 YAML。
请这一次严格只返回 YAML，不要输出任何解释、前言、后记、markdown 代码块或多余文本。

返回要求：
- 顶层必须包含 `ttt:` 或 `tree:`
- 只输出一个完整的三层 TTT 快照
- L3 必须是叶子节点，children 必须为空数组
""".strip()

OVERALL_ASSESSMENT_PROMPT = """
你是 SOC 多智能体系统中的 Planner 总结助手。
当整个 TTT 已经没有待执行叶子节点时，你需要基于事件、全部执行记录、全部 round review 和最终 TTT，输出一段事件整体研判结论，并补充企业后续建议。

要求：
- 总结整体攻击/异常是否成立，以及当前最可靠的结论。
- 点出已经拿到的核心证据和仍然存在的不确定性。
- 明确给出企业建议采取的举措，优先输出 2 到 4 条可执行建议，可覆盖短期处置、进一步排查、加固与监控改进。
- 建议必须与本事件证据相关，不能空泛，也不能编造当前没有的事实。
- 用一段简洁、可读的自然语言输出即可。
- 建议使用清晰分段，例如“整体研判”“建议措施”等小标题，方便阅读。
- 如果合适，可以自然使用简洁 Markdown 提升可读性。
- 不要输出 YAML 或 JSON。
""".strip()


@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = (
        "负责告警的初始分析、TTT 初始化，以及每一轮结束后的 TTT 更新。"
    )
    responsibilities: tuple[str, ...] = (
        "分析初始告警上下文并建立溯源目标。",
        "输出完整 TTT 快照，作为系统共享黑板。",
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
        procedural_memory: ProceduralMemoryLibrary | None = None,
        factual_memory: FactualMemoryLibrary | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = PlannerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.procedural_memory = procedural_memory or ProceduralMemoryLibrary()
        self.factual_memory = factual_memory or FactualMemoryLibrary()
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
            payload={"text": "planner_start_initial_planning", "event_name": event.event_name},
        )

        try:
            analysis = self._analyze_event(event)
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.PLANNER_ANALYSIS_COMPLETED,
                payload={
                    "analysis": analysis,
                    "selected_procedural_memory": None,
                },
            )
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={"text": "planner_start_procedural_memory_lookup"},
            )
            selected_memory = self._select_procedural_memory(event)
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={
                    "text": "planner_procedural_memory_lookup_completed",
                    "selected_procedural_memory": selected_memory.summary_payload() if selected_memory else None,
                },
            )
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={"text": "planner_start_ttt_initialization"},
            )
            candidate_tree = self._generate_initial_ttt(
                event,
                selected_memory=selected_memory,
                factual_memories=self.factual_memory.list_documents(),
                available_tools=self._describe_available_tools(),
            )
        except Exception as exc:
            logger.exception("Planner initial TTT generation failed")
            return self._fail_event(event, f"Planner initial TTT generation failed: {exc}")
        snapshot = self.ttt_store.save_snapshot(candidate_tree)

        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
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
        self._publish_overall_assessment_if_completed(
            event=updated_event,
            latest_ttt=snapshot,
        )
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_INITIALIZED,
            payload={"ttt": snapshot.to_dict(), "event_status": updated_event.event_status.value},
        )
        return updated_event

    def process_replanning(self, event: Event) -> Event:
        logger.info("Planner processing replanning for event=%s round=%s", event.event_id, event.current_round)
        latest_review = self.storage.get_round_review(event.event_id, event.current_round)
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        executions = self.storage.list_executions(event.event_id, event.current_round)
        if latest_review is None or latest_ttt is None:
            logger.warning("Planner replanning skipped for event=%s due to missing review or ttt", event.event_id)
            return event

        next_round = event.current_round + 1
        try:
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={
                    "text": "planner_start_ttt_replanning",
                    "review_round": latest_review.round_id,
                },
            )
            if self._should_reuse_existing_ttt(
                review=latest_review,
                executions=executions,
                latest_ttt=latest_ttt,
            ):
                candidate_tree = self._reuse_existing_ttt_for_next_round(
                    latest_ttt=latest_ttt,
                    next_round=next_round,
                )
            else:
                candidate_tree = self._generate_updated_ttt(
                    event=event,
                    latest_review=latest_review,
                    latest_ttt=latest_ttt,
                    executions=executions,
                    next_round=next_round,
                )
            candidate_tree = self._apply_l2_replan_policy(
                candidate_tree,
                previous_tree=latest_ttt,
            )
        except Exception as exc:
            logger.exception("Planner TTT update failed")
            fallback_tree = self._fallback_replanning_tree(
                event=event,
                latest_ttt=latest_ttt,
                next_round=next_round,
                error=exc,
            )
            if fallback_tree is None:
                return self._fail_event(event, f"Planner TTT update failed: {exc}")
            candidate_tree = fallback_tree
        snapshot = self.ttt_store.save_snapshot(candidate_tree)
        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
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
        self._publish_overall_assessment_if_completed(
            event=updated_event,
            latest_ttt=snapshot,
        )
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_UPDATED,
            payload={"ttt": snapshot.to_dict(), "based_on_review": latest_review.to_dict()},
        )
        return updated_event

    def _analyze_event(self, event: Event) -> str:
        user_prompt = "\n".join(
            [
                "请根据以下安全告警完成初始系统分析，直接返回分析内容本身即可。",
                "如果你觉得合适，可以自然使用简洁 Markdown 来提升可读性；不必强行套格式。",
                "不要输出 YAML 或 JSON。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            PLANNER_ANALYSIS_SYSTEM_PROMPT,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        analysis = (response_text or "").strip()
        if not analysis:
            raise ValueError("Planner analysis returned empty content")
        return analysis

    def _select_procedural_memory(
        self,
        event: Event,
    ) -> ProceduralMemoryDocument | None:
        documents = self.procedural_memory.list_documents()
        if not documents:
            return None

        user_prompt = "\n".join(
            [
                "请从以下 procedural memory 摘要中选择最适合当前事件的一篇。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(
                    [document.summary_payload() for document in documents],
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
        parsed = parse_yaml_response(
            call_llm(
                PROCEDURAL_MEMORY_SELECTION_PROMPT,
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
        )
        document_id = str((parsed or {}).get("document_id") or "").strip()
        if not document_id:
            return None
        return self.procedural_memory.get_document(document_id)

    def _generate_initial_ttt(
        self,
        event: Event,
        *,
        selected_memory: ProceduralMemoryDocument | None,
        factual_memories: list[FactualMemoryDocument],
        available_tools: list[dict[str, Any]],
    ) -> TracebackTaskTree:
        prompt_parts = [
            "请根据以下安全告警初始化 TTT，并只返回 YAML。",
            "请先理解事件内容，再参考 procedural memory、factual memory 与当前 MCP 工具能力构建更贴切的初始 TTT。",
            "其中：procedural memory 是调查 workflow 参考；factual memory 是企业与数据环境背景；MCP tools 代表当前真实可执行能力，L3 应尽量贴合这些能力。",
            "如果事件存在多个明显调查方向，必须拆成多个 L1 根节点；不要把多个方向都塞进一个总 L1。",
            "L3 必须尽量写出具体实体，例如具体 IP、主机名、账号；避免使用“该 IP / 该主机 / 该账号”这类模糊指代。",
            json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
        ]
        if selected_memory is not None:
            prompt_parts.extend(
                [
                    "以下是匹配到的 procedural memory 文档：",
                    json.dumps(selected_memory.summary_payload(), ensure_ascii=False, indent=2),
                    selected_memory.content,
                ]
            )
        if factual_memories:
            prompt_parts.extend(
                [
                    "以下是当前固定注入的 factual memory 文档：",
                    json.dumps(
                        [document.summary_payload() for document in factual_memories],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "\n\n".join(
                        [
                            f"## {document.title}\n{document.content}"
                            for document in factual_memories
                        ]
                    ),
                ]
            )
        if available_tools:
            prompt_parts.extend(
                [
                    "以下是当前可用的 MCP server 与 tools：",
                    json.dumps(available_tools, ensure_ascii=False, indent=2),
                ]
            )
        user_prompt = "\n".join(prompt_parts)
        ttt_payload = self._request_ttt_payload(user_prompt)
        entity_context = self._extract_entity_context(event=event)
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=event.current_round,
            ttt_payload=ttt_payload,
            metadata={
                "selected_procedural_memory": selected_memory.summary_payload() if selected_memory else None,
                "factual_memories": [document.summary_payload() for document in factual_memories],
                "available_mcp_tools": available_tools,
            },
            entity_context=entity_context,
            apply_l2_policy=False,
        )

    def _generate_updated_ttt(
        self,
        *,
        event: Event,
        latest_review: RoundReview,
        latest_ttt: TracebackTaskTree,
        executions: list[Any],
        next_round: int,
    ) -> TracebackTaskTree:
        entity_context = self._extract_entity_context(event=event, analysis=latest_review.summary_text)
        replanning_policy = self._build_replanning_policy_hint(
            review=latest_review,
            executions=executions,
        )
        user_prompt = "\n".join(
            [
                "请根据以下事件、上一轮 TTT 和 Reviewer 总结，更新下一轮 TTT，并只返回 YAML。",
                replanning_policy,
                "如果事件存在多个明显调查方向，必须分别保持或拆分为多个 L1 根节点。",
                "已经是 done 的节点必须保持 done，不要在新一轮里把它改回 todo 或 in_progress。",
                "L3 必须尽量写出具体实体；若输入中已有明确 IP、主机名、账号，不要写成“该 IP / 该主机 / 该账号”。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps([execution.to_dict() for execution in executions], ensure_ascii=False, indent=2),
                json.dumps(latest_review.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        ttt_payload = self._request_ttt_payload(user_prompt)
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=next_round,
            ttt_payload=ttt_payload,
            entity_context=entity_context,
            previous_tree=latest_ttt,
            apply_l2_policy=False,
        )

    def _request_ttt_payload(self, user_prompt: str) -> dict[str, Any]:
        attempts = (
            user_prompt,
            "\n\n".join([user_prompt, TTT_RETRY_CORRECTION_PROMPT]),
        )
        last_error = "Planner returned empty or non-YAML content"
        for attempt_prompt in attempts:
            response_text = call_llm(
                self.agent.system_prompt,
                attempt_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
            parsed = parse_yaml_response(response_text)
            if not parsed:
                last_error = "Planner returned empty or non-YAML content"
                continue
            ttt_payload = parsed.get("ttt") or parsed.get("tree")
            if isinstance(ttt_payload, dict):
                return ttt_payload
            last_error = "Planner response missing `ttt`/`tree` object"
        raise ValueError(last_error)

    def _publish_overall_assessment_if_completed(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
    ) -> None:
        if event.event_status != EventStatus.COMPLETED:
            return
        try:
            summary_text = self._generate_overall_assessment(event=event, latest_ttt=latest_ttt)
        except Exception:
            logger.exception("Planner overall assessment generation failed")
            return
        self._publish(
            event_id=event.event_id,
            round_id=latest_ttt.round_id,
            message_type=MessageType.OVERALL_ASSESSMENT_CREATED,
            payload={"summary_text": summary_text},
        )

    def _generate_overall_assessment(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
    ) -> str:
        executions = self.storage.list_executions(event.event_id)
        round_reviews: list[RoundReview] = []
        for round_id in range(1, latest_ttt.round_id + 1):
            review = self.storage.get_round_review(event.event_id, round_id)
            if review is not None:
                round_reviews.append(review)
        user_prompt = "\n".join(
            [
                "请基于以下完整溯源过程输出事件整体研判结论，并补充面向企业的后续建议措施。",
                "建议措施应尽量具体、可执行，并与当前证据直接相关。",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps([execution.to_dict() for execution in executions], ensure_ascii=False, indent=2),
                json.dumps([review.to_dict() for review in round_reviews], ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            OVERALL_ASSESSMENT_PROMPT,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        summary_text = (response_text or "").strip()
        if not summary_text:
            raise ValueError("Planner overall assessment returned empty content")
        return summary_text

    def _describe_available_tools(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in list_registered_tools():
            grouped.setdefault(tool.server_name, []).append(
                {
                    "server_name": tool.server_name,
                    "name": tool.name,
                    "description": tool.description,
                    "when_to_use": tool.when_to_use,
                    "limitations": tool.limitations,
                }
            )
        return [
            {"server_name": server_name, "tools": tools}
            for server_name, tools in sorted(grouped.items())
        ]

    def _build_replanning_policy_hint(
        self,
        *,
        review: RoundReview,
        executions: list[Any],
    ) -> str:
        if not executions:
            return "本轮缺少执行记录，可根据 review 自主补充下一轮 TTT，但仍应保持三层结构与实体完整性。"

        successes = [
            execution
            for execution in executions
            if str(getattr(execution, "execution_status", "")).lower() == "completed"
        ]
        has_failures = len(successes) != len(executions)
        review_text = (review.summary_text or "").strip()
        if successes and not has_failures:
            return "\n".join(
                [
                    "重规划策略提示：本轮执行整体成功。",
                    "如果 review 没有明确提出新的关键证据、新缺口或新的调查方向，默认只更新相关节点状态和最小必要调整，不要新增、重排或大幅重写 TTT。",
                    "如果当前步骤的工具执行成功且已经返回所需结果，应尽量不要更新 TTT，直接沿着当前树继续向下执行剩余节点。",
                    f"当前 review 文本：{review_text}",
                ]
            )
        return "\n".join(
            [
                "重规划策略提示：本轮存在失败执行、阻塞或未完成项，可根据 review 补充新的问题、L3 或新的 L1 方向，但仍应保持最小必要改动。",
                f"当前 review 文本：{review_text}",
            ]
        )

    def _should_reuse_existing_ttt(
        self,
        *,
        review: RoundReview,
        executions: list[Any],
        latest_ttt: TracebackTaskTree,
    ) -> bool:
        if not executions:
            return False
        if not self.ttt_store.has_open_work(latest_ttt.event_id, latest_ttt.round_id):
            return False

        successful_with_results = [
            execution
            for execution in executions
            if str(getattr(execution, "execution_status", "")).lower() == "completed"
            and bool(getattr(execution, "result", None))
            and not bool(getattr(execution, "result", {}).get("no_data_found"))
        ]
        if len(successful_with_results) != len(executions):
            return False

        review_text = (review.summary_text or "").lower()
        change_indicators = (
            "新方向",
            "新增方向",
            "新的调查方向",
            "扩线",
            "转向",
            "新增问题",
            "新增l1",
            "新增 l1",
            "新增 l2",
            "新增 l3",
            "补充新问题",
            "新的关键证据",
        )
        return not any(indicator in review_text for indicator in change_indicators)

    def _reuse_existing_ttt_for_next_round(
        self,
        *,
        latest_ttt: TracebackTaskTree,
        next_round: int,
    ) -> TracebackTaskTree:
        metadata = dict(latest_ttt.metadata or {})
        metadata["planner_replanning_strategy"] = "reuse_existing_ttt"
        return TracebackTaskTree(
            event_id=latest_ttt.event_id,
            round_id=next_round,
            root_nodes=tuple(self._reset_reusable_node_statuses(node) for node in latest_ttt.root_nodes),
            metadata=metadata,
            created_at=latest_ttt.created_at,
            updated_at=utc_now(),
        )

    def _fallback_replanning_tree(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
        next_round: int,
        error: Exception,
    ) -> TracebackTaskTree | None:
        error_text = str(error or "").strip()
        if not error_text:
            return None
        lowered_error = error_text.lower()
        fallback_indicators = (
            "non-yaml",
            "missing `ttt`/`tree` object",
            "timeout",
            "timed out",
            "readtimeout",
            "connecttimeout",
            "connection timed out",
        )
        if not any(indicator in lowered_error for indicator in fallback_indicators):
            return None

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_WARNING,
            payload={
                "text": "planner_ttt_replanning_fallback_reuse_existing_tree",
                "reason": error_text,
            },
        )
        return self._apply_l2_replan_policy(
            self._reuse_existing_ttt_for_next_round(
                latest_ttt=latest_ttt,
                next_round=next_round,
            ),
            previous_tree=latest_ttt,
        )

    def _reset_reusable_node_statuses(self, node: TTTNode) -> TTTNode:
        # Cross-round reuse must not carry old in_progress markers forward,
        # otherwise the next round becomes planned-without-claimable-work.
        reset_status = TTTNodeStatus.TODO if node.status == TTTNodeStatus.IN_PROGRESS else node.status
        return TTTNode(
            node_id=node.node_id,
            title=node.title,
            status=reset_status,
            children=tuple(self._reset_reusable_node_statuses(child) for child in node.children),
            metadata=dict(node.metadata or {}),
        )

    def _extract_entity_context(self, *, event: Event, analysis: str | None = None) -> dict[str, str]:
        text_parts = [
            event.event_name,
            event.message,
            json.dumps(event.context or {}, ensure_ascii=False),
        ]
        if analysis:
            text_parts.append(analysis)
        combined = "\n".join(part for part in text_parts if part)

        ip_matches = []
        seen_ips: set[str] = set()
        for match in IP_PATTERN.findall(combined):
            if match not in seen_ips:
                seen_ips.add(match)
                ip_matches.append(match)

        host_candidates = self._collect_named_values(
            event.context,
            keys=("host", "hostname", "server", "dest", "dest_host", "target_host", "asset"),
        )
        if not host_candidates:
            host_candidates = [
                token
                for token in HOSTLIKE_PATTERN.findall(combined)
                if any(sep in token for sep in ("-", "_", ".")) and not IP_PATTERN.fullmatch(token)
            ]

        account_candidates = self._collect_named_values(
            event.context,
            keys=("user", "username", "account", "account_name"),
        )
        if not account_candidates:
            account_candidates = [
                token
                for token in ACCOUNTLIKE_PATTERN.findall(combined)
                if "@" in token or token.lower().startswith(("user", "svc_", "adm_", "admin"))
            ]

        context: dict[str, str] = {}
        if ip_matches:
            context["ip"] = ip_matches[0]
        if host_candidates:
            context["host"] = host_candidates[0]
        if account_candidates:
            context["account"] = account_candidates[0]
        return context

    def _collect_named_values(self, context: dict[str, Any], *, keys: tuple[str, ...]) -> list[str]:
        values: list[str] = []
        if not isinstance(context, dict):
            return values
        for key in keys:
            value = context.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and text not in values:
                values.append(text)
        return values

    def _normalize_leaf_title(self, title: str, entity_context: dict[str, str]) -> str:
        normalized = title.strip()
        ip_value = entity_context.get("ip")
        host_value = entity_context.get("host")
        account_value = entity_context.get("account")

        replacements = [
            ("该 IP", ip_value),
            ("这个 IP", ip_value),
            ("源 IP", ip_value),
            ("该主机", host_value),
            ("目标主机", host_value),
            ("该账号", account_value),
            ("该用户", account_value),
        ]
        for needle, replacement in replacements:
            if replacement and needle in normalized:
                normalized = normalized.replace(needle, replacement)

        if ip_value and "IP" in normalized and ip_value not in normalized and "告警中的" not in normalized:
            normalized = normalized.replace("IP", f"IP {ip_value}", 1)
        if host_value and "主机" in normalized and host_value not in normalized and "告警中的" not in normalized:
            normalized = normalized.replace("主机", f"主机 {host_value}", 1)
        if account_value and ("账号" in normalized or "用户" in normalized) and account_value not in normalized and "告警中的" not in normalized:
            if "账号" in normalized:
                normalized = normalized.replace("账号", f"账号 {account_value}", 1)
            else:
                normalized = normalized.replace("用户", f"用户 {account_value}", 1)

        fallback_replacements = {
            "该 IP": "告警中的源 IP",
            "这个 IP": "告警中的源 IP",
            "源 IP": "告警中的源 IP",
            "该主机": "告警中的相关主机",
            "目标主机": "告警中的目标主机",
            "该账号": "告警中的相关账号",
            "该用户": "告警中的相关用户",
        }
        for needle, replacement in fallback_replacements.items():
            if needle in normalized:
                normalized = normalized.replace(needle, replacement)
        return normalized

    def _normalize_ttt_payload(
        self,
        *,
        event_id: str,
        round_id: int,
        ttt_payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        entity_context: dict[str, str] | None = None,
        previous_tree: TracebackTaskTree | None = None,
        apply_l2_policy: bool = True,
    ) -> TracebackTaskTree:
        roots = ttt_payload.get("root_nodes") or ttt_payload.get("nodes") or []
        resolved_entity_context = entity_context or {}
        frozen_done_nodes = self._collect_frozen_done_nodes(previous_tree)
        normalized_roots = tuple(
            self._normalize_node(
                node,
                path=str(index),
                entity_context=resolved_entity_context,
                frozen_done_nodes=frozen_done_nodes,
            )
            for index, node in enumerate(roots, start=1)
        )
        tree = TracebackTaskTree(
            event_id=event_id,
            round_id=round_id,
            root_nodes=normalized_roots,
            metadata=dict(metadata or ttt_payload.get("metadata") or {}),
        )
        if previous_tree is not None:
            tree = self._enforce_linear_progression_statuses(tree, previous_tree=previous_tree)
        if apply_l2_policy:
            tree = self._apply_l2_replan_policy(tree, previous_tree=previous_tree)
        return tree

    def _normalize_node(
        self,
        node: dict[str, Any],
        *,
        path: str,
        entity_context: dict[str, str],
        frozen_done_nodes: set[tuple[str, str]],
    ) -> TTTNode:
        title = str(node.get("title") or path)
        children_raw = node.get("children") or []
        if not children_raw:
            title = self._normalize_leaf_title(title, entity_context)
        status = coerce_ttt_node_status(node.get("status"))
        if (path, title) in frozen_done_nodes or ("", title) in frozen_done_nodes:
            status = TTTNodeStatus.DONE
        children = tuple(
            self._normalize_node(
                child,
                path=f"{path}-{child_index}",
                entity_context=entity_context,
                frozen_done_nodes=frozen_done_nodes,
            )
            for child_index, child in enumerate(children_raw, start=1)
        )
        return TTTNode(
            node_id=path,
            title=title,
            status=status,
            children=children,
            metadata=dict(node.get("metadata") or {}),
        )

    def _collect_frozen_done_nodes(
        self,
        previous_tree: TracebackTaskTree | None,
    ) -> set[tuple[str, str]]:
        if previous_tree is None:
            return set()

        frozen: set[tuple[str, str]] = set()

        def walk(node: TTTNode) -> None:
            if node.status == TTTNodeStatus.DONE:
                frozen.add((node.node_id, node.title))
                frozen.add(("", node.title))
            for child in node.children:
                walk(child)

        for root in previous_tree.root_nodes:
            walk(root)
        return frozen

    def _enforce_linear_progression_statuses(
        self,
        tree: TracebackTaskTree,
        *,
        previous_tree: TracebackTaskTree,
    ) -> TracebackTaskTree:
        previous_nodes_by_id: dict[str, TTTNode] = {}
        previous_nodes_by_title: dict[str, TTTNode] = {}

        def collect(node: TTTNode) -> None:
            previous_nodes_by_id[node.node_id] = node
            previous_nodes_by_title[node.title] = node
            for child in node.children:
                collect(child)

        for root in previous_tree.root_nodes:
            collect(root)

        def transform(node: TTTNode) -> TTTNode:
            children = tuple(transform(child) for child in node.children)
            metadata = dict(node.metadata or {})
            previous_node = previous_nodes_by_id.get(node.node_id) or previous_nodes_by_title.get(node.title)
            explicit_skip = bool(metadata.get("skip_reason"))
            status = node.status

            if explicit_skip:
                status = TTTNodeStatus.DONE if status == TTTNodeStatus.DONE else TTTNodeStatus.NOT_APPLICABLE
            elif previous_node is not None and previous_node.status in {TTTNodeStatus.DONE, TTTNodeStatus.NOT_APPLICABLE}:
                status = previous_node.status
            else:
                status = TTTNodeStatus.TODO

            return TTTNode(
                node_id=node.node_id,
                title=node.title,
                status=status,
                children=children,
                metadata=metadata,
            )

        return TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tuple(transform(root) for root in tree.root_nodes),
            metadata=dict(tree.metadata or {}),
            created_at=tree.created_at,
            updated_at=tree.updated_at,
        )

    def _apply_l2_replan_policy(
        self,
        tree: TracebackTaskTree,
        *,
        previous_tree: TracebackTaskTree | None,
    ) -> TracebackTaskTree:
        previous_l2_by_node_id: dict[str, TTTNode] = {}
        previous_l2_by_title: dict[str, TTTNode] = {}

        if previous_tree is not None:
            for previous_l2 in self._iter_l2_nodes(previous_tree.root_nodes):
                previous_l2_by_node_id[previous_l2.node_id] = previous_l2
                previous_l2_by_title[previous_l2.title] = previous_l2

        def transform(node: TTTNode) -> TTTNode:
            children = tuple(transform(child) for child in node.children)
            metadata = dict(node.metadata or {})
            status = node.status

            if self._node_depth(node.node_id) == 2:
                previous_l2 = previous_l2_by_node_id.get(node.node_id) or previous_l2_by_title.get(node.title)
                previous_count = 0
                if previous_l2 is not None:
                    previous_count = int((previous_l2.metadata or {}).get("replan_attempt_count") or 0)
                attempt_count = previous_count
                if previous_tree is not None:
                    if previous_l2 is None:
                        attempt_count = 1
                    elif self._l2_subtree_signature(node) != self._l2_subtree_signature(previous_l2):
                        attempt_count = previous_count + 1
                metadata["replan_attempt_count"] = attempt_count
                if attempt_count > MAX_L2_REPLAN_ATTEMPTS:
                    metadata["skip_reason"] = "l2_replan_cap_reached"
                    status = TTTNodeStatus.NOT_APPLICABLE
                    children = tuple(self._skip_subtree(child) for child in children)

            return TTTNode(
                node_id=node.node_id,
                title=node.title,
                status=status,
                children=children,
                metadata=metadata,
            )

        return TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tuple(transform(root) for root in tree.root_nodes),
            metadata=dict(tree.metadata or {}),
            created_at=tree.created_at,
            updated_at=tree.updated_at,
        )

    def _l2_subtree_signature(self, node: TTTNode) -> tuple[Any, ...]:
        return (
            node.title,
            tuple(self._l2_subtree_signature(child) for child in node.children),
        )

    def _skip_subtree(self, node: TTTNode) -> TTTNode:
        children = tuple(self._skip_subtree(child) for child in node.children)
        metadata = dict(node.metadata or {})
        metadata["skip_reason"] = "l2_replan_cap_reached"
        status = node.status if node.status == TTTNodeStatus.DONE else TTTNodeStatus.NOT_APPLICABLE
        return TTTNode(
            node_id=node.node_id,
            title=node.title,
            status=status,
            children=children,
            metadata=metadata,
        )

    @staticmethod
    def _node_depth(node_id: str) -> int:
        return len([part for part in str(node_id).split("-") if part])

    def _iter_l2_nodes(self, nodes: tuple[TTTNode, ...]) -> list[TTTNode]:
        l2_nodes: list[TTTNode] = []

        def walk(node: TTTNode) -> None:
            if self._node_depth(node.node_id) == 2:
                l2_nodes.append(node)
            for child in node.children:
                walk(child)

        for root in nodes:
            walk(root)
        return l2_nodes

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
