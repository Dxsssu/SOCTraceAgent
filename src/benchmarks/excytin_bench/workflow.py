from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import sqlglot

from src.agent.llm import call_llm, parse_yaml_response
from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.agent.reviewer import REVIEWER_SYSTEM_PROMPT
from src.schema import Event, RoundReview, TracebackTaskTree, TTTNode, TTTNodeStatus
from src.workflow.context import MemoryContextService


class BenchmarkActionType(StrEnum):
    QUERY = "query"
    SUBMIT = "submit"


@dataclass(frozen=True, slots=True)
class BenchmarkAction:
    """One action issued to ExCyTInEnv."""

    action_type: BenchmarkActionType
    content: str
    step_no: int
    node_id: str = ""

    @property
    def submit(self) -> bool:
        return self.action_type == BenchmarkActionType.SUBMIT

    def as_secgym_action(self) -> tuple[str, bool]:
        return self.content, self.submit

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "content": self.content,
            "step_no": self.step_no,
            "node_id": self.node_id,
            "submit": self.submit,
        }


@dataclass(slots=True)
class BenchmarkExecution:
    step_no: int
    node_id: str
    node_title: str
    sql: str
    observation: str
    query_success: bool
    result_status: str
    observation_truncated: bool = False
    original_observation_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "node_id": self.node_id,
            "node_title": self.node_title,
            "sql": self.sql,
            "observation": self.observation,
            "query_success": self.query_success,
            "result_status": self.result_status,
            "observation_truncated": self.observation_truncated,
            "original_observation_chars": self.original_observation_chars,
        }


@dataclass(slots=True)
class WorkflowState:
    event: Event
    ttt: TracebackTaskTree
    initial_input: str
    actions_issued: int = 0
    pending_action: BenchmarkAction | None = None
    pending_node: TTTNode | None = None
    executions: list[BenchmarkExecution] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    retrieval_log: list[dict[str, Any]] = field(default_factory=list)
    ttt_history: list[dict[str, Any]] = field(default_factory=list)
    action_history: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    ready_to_submit: bool = False
    cannot_continue: bool = False
    done: bool = False
    final_answer: str = ""
    error_repair_counts: dict[str, int] = field(default_factory=dict)
    empty_repair_counts: dict[str, int] = field(default_factory=dict)
    executor_attempt_counts: dict[str, int] = field(default_factory=dict)


class ReadOnlySQLValidator:
    """Validate one read-only MySQL statement before it reaches the benchmark."""

    _ALLOWED_PREFIXES: ClassVar[set[str]] = {
        "SELECT",
        "WITH",
        "SHOW",
        "DESCRIBE",
        "DESC",
        "EXPLAIN",
    }
    _FORBIDDEN = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|REPLACE|MERGE|UPSERT|CREATE|ALTER|DROP|"
        r"TRUNCATE|RENAME|GRANT|REVOKE|CALL|DO|SET|USE|LOCK|UNLOCK|LOAD|"
        r"INTO\s+(?:OUTFILE|DUMPFILE)|FOR\s+UPDATE)\b|"
        r"\b(?:LOAD_FILE|SLEEP|BENCHMARK)\s*\(",
        re.IGNORECASE,
    )

    @classmethod
    def validate(cls, sql: str) -> str:
        normalized = str(sql or "").strip()
        if not normalized:
            raise ValueError("SQL is empty")
        if normalized.startswith(("--", "#", "/*")):
            raise ValueError("SQL must start with an explicit read-only statement")
        prefix_match = re.match(r"([A-Za-z]+)", normalized)
        prefix = prefix_match.group(1).upper() if prefix_match else ""
        if prefix not in cls._ALLOWED_PREFIXES:
            raise ValueError(
                f"SQL statement type is not allowed: {prefix or 'unknown'}"
            )
        if cls._FORBIDDEN.search(normalized):
            raise ValueError("SQL contains a forbidden operation")
        try:
            expressions = sqlglot.parse(normalized, read="mysql")
        except sqlglot.errors.ParseError as exc:
            raise ValueError(f"SQL cannot be parsed as MySQL: {exc}") from exc
        if len(expressions) != 1 or expressions[0] is None:
            raise ValueError("Exactly one SQL statement is required")
        return normalized.rstrip(";").strip() + ";"


_EXECUTOR_PROMPT = """
你是 ExCyTIn-Bench 调查流程中的 Executor。你一次只处理一个 TTT L3 节点，并生成一条交给外部环境执行的 MySQL 查询。

边界：
- 首次构造当前 L3 的 SQL 时，只使用 Workflow 提供的 Semantic Memory，不参考 Episodic Memory。
- 当前 L3 的上一条 SQL 返回数据库 error 或空结果时，Workflow 才会提供按失败现象检索到的 Episodic Memory，用于修复 SQL。
- Semantic Memory 是表名、字段名和连接关系的硬约束。
- Schema 中存在字段不代表每条记录都会填充该字段；空结果后不得继续假设多个实体一定共处于同一行。
- 告警名称和类别优先参考 AlertInfo；告警受影响实体、人工分配、自动调查和处置元数据优先参考 SecurityAlert 的 CompromisedEntity 与 ExtendedProperties；AlertEvidence 通过 AlertId 提供结构化证据实体。
- Episodic Memory 只用于参考脱敏查询结构和错误修复，不是当前案件证据。
- 不得使用 Procedural Memory，不得改变调查方向，不得生成最终答案。
- 只能生成一条只读 SQL；查询结果尚未返回时不得臆测结果。

只输出 YAML：
response_type: EXECUTE_SQL
sql: "一条 MySQL 只读语句"
""".strip()


_ANSWER_PROMPT = """
你是 ExCyTIn-Bench 的最终答案整理器。仅根据问题和已执行 SQL 的真实 observation 作答。
不得使用长期记忆中的历史值，不得猜测，不得提及 TTT、Memory 或内部角色。
答案应直接、简洁，并包含问题要求的实体或关系。只输出 YAML：
response_type: FINAL_ANSWER
answer: "提交给评测器的最终答案"
""".strip()


LLMCallable = Callable[[str, str], str]


class ExcytinBenchWorkflow:
    """Synchronous, environment-stepped workflow for ExCyTIn-Bench.

    The workflow never executes SQL itself. ``propose_next_action`` returns one
    SQL statement, and ``accept_observation`` consumes the value returned by
    ``ExcytinEnv.step`` before any next query is generated.
    """

    def __init__(
        self,
        *,
        max_steps: int = 25,
        memory_contexts: MemoryContextService | None = None,
        llm: LLMCallable | None = None,
        sql_generation_attempts: int = 2,
        max_executor_attempts_per_l3: int = 3,
        max_error_repairs: int = 2,
        max_empty_repairs: int = 2,
        yaml_response_attempts: int = 2,
        max_observation_chars: int = 20_000,
    ) -> None:
        if max_steps < 2:
            raise ValueError("max_steps must reserve at least one query and one submit")
        self.max_steps = max_steps
        self.memory_contexts = memory_contexts or MemoryContextService()
        self._planner_memory = self.memory_contexts.planner_view()
        self._executor_memory = self.memory_contexts.executor_view()
        self._reviewer_memory = self.memory_contexts.reviewer_view()
        self._llm = llm or self._default_llm
        self.sql_generation_attempts = max(1, sql_generation_attempts)
        self.max_executor_attempts_per_l3 = max(1, max_executor_attempts_per_l3)
        self.max_error_repairs = max(0, max_error_repairs)
        self.max_empty_repairs = max(0, max_empty_repairs)
        self.yaml_response_attempts = max(1, yaml_response_attempts)
        self.max_observation_chars = max(1_000, max_observation_chars)
        self.state: WorkflowState | None = None

    @staticmethod
    def _default_llm(system_prompt: str, user_prompt: str) -> str:
        return call_llm(
            system_prompt,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )

    def reset(self) -> None:
        self.state = None

    def start(
        self,
        initial_input: str | Mapping[str, Any],
        *,
        runtime_info: Mapping[str, Any] | None = None,
    ) -> TracebackTaskTree:
        if self.state is not None and not self.state.done:
            raise RuntimeError("An ExCyTIn workflow episode is already active")
        question = self._sanitize_initial_input(initial_input)
        safe_runtime_info = {
            key: runtime_info[key]
            for key in ("attack", "noise_level", "qid", "layer", "split")
            if runtime_info and key in runtime_info
        }
        event = Event(
            event_id=f"excytin-{uuid4()}",
            event_name="ExCyTIn-Bench investigation",
            message=question,
            source="excytin_bench",
            context={
                "memory_profile": "excytin_bench",
                "benchmark": "excytin_bench",
                **safe_runtime_info,
            },
        )
        planner_context = self._planner_memory.build(event)
        ttt = self._plan_initial_ttt(event, planner_context)
        self.state = WorkflowState(event=event, ttt=ttt, initial_input=question)
        self.state.ttt_history.append(ttt.to_dict())
        self._record_trace(
            "workflow_started",
            {"event_id": event.event_id, "round_id": ttt.round_id},
        )
        self._log_retrieval("planner", planner_context)
        self._record_trace("ttt_planned", {"ttt": ttt.to_dict()})
        return ttt

    def act(
        self,
        observation: str | Mapping[str, Any],
        *,
        runtime_info: Mapping[str, Any] | None = None,
    ) -> BenchmarkAction:
        """SecGym-style convenience method: consume observation, emit one action."""

        if self.state is None:
            self.start(observation, runtime_info=runtime_info)
        elif (
            self.state.pending_action is None
            and self.state.actions_issued == 0
            and not self.state.executions
        ):
            # ``reset(question_dict=...)`` may preload the sanitized question.
            # In that mode SecGym can still pass the same first observation.
            pass
        else:
            self.accept_observation(str(observation))
        return self.propose_next_action()

    def accept_observation(
        self,
        observation: str,
        *,
        query_success: bool | None = None,
    ) -> BenchmarkExecution:
        state = self._require_state()
        pending = state.pending_action
        node = state.pending_node
        if pending is None or pending.action_type != BenchmarkActionType.QUERY:
            raise RuntimeError("No pending SQL action is waiting for an observation")
        if node is None:
            raise RuntimeError("Pending SQL action has no associated TTT node")

        raw_observation = str(observation)
        success = (
            self._infer_query_success(raw_observation)
            if query_success is None
            else bool(query_success)
        )
        observation_text, observation_truncated = self._compact_observation(
            raw_observation
        )
        result_status = self._result_status(raw_observation, success)
        execution = BenchmarkExecution(
            step_no=pending.step_no,
            node_id=node.node_id,
            node_title=node.title,
            sql=pending.content,
            observation=observation_text,
            query_success=success,
            result_status=result_status,
            observation_truncated=observation_truncated,
            original_observation_chars=len(raw_observation),
        )
        state.executions.append(execution)
        self._record_trace(
            "observation_received",
            {"execution": execution.to_dict()},
        )
        state.pending_action = None
        state.pending_node = None
        repair_trigger = (
            "sql_error"
            if not success
            else ("empty_result" if result_status == "empty_result" else "")
        )
        executor_attempt_count = state.executor_attempt_counts.get(node.node_id, 0)
        executor_attempt_budget_exhausted = (
            executor_attempt_count >= self.max_executor_attempts_per_l3
        )
        state.ttt = self._set_node_status(
            state.ttt,
            node.node_id,
            TTTNodeStatus.TODO if repair_trigger else TTTNodeStatus.DONE,
        )

        if repair_trigger == "sql_error" and not executor_attempt_budget_exhausted:
            repair_count = state.error_repair_counts.get(node.node_id, 0)
            if repair_count < self.max_error_repairs:
                state.error_repair_counts[node.node_id] = repair_count + 1
                self._record_trace(
                    "executor_error_repair_scheduled",
                    {
                        "node_id": node.node_id,
                        "repair_attempt": repair_count + 1,
                        "max_error_repairs": self.max_error_repairs,
                        "error": observation_text,
                    },
                )
                return execution
        elif repair_trigger == "empty_result" and not executor_attempt_budget_exhausted:
            repair_count = state.empty_repair_counts.get(node.node_id, 0)
            if repair_count < self.max_empty_repairs:
                state.empty_repair_counts[node.node_id] = repair_count + 1
                self._record_trace(
                    "executor_empty_repair_scheduled",
                    {
                        "node_id": node.node_id,
                        "repair_attempt": repair_count + 1,
                        "max_empty_repairs": self.max_empty_repairs,
                        "previous_sql": execution.sql,
                    },
                )
                return execution
        else:
            state.error_repair_counts.pop(node.node_id, None)
            state.empty_repair_counts.pop(node.node_id, None)

        if repair_trigger and executor_attempt_budget_exhausted:
            self._record_trace(
                "executor_attempt_budget_exhausted",
                {
                    "node_id": node.node_id,
                    "attempt_count": executor_attempt_count,
                    "max_executor_attempts_per_l3": (self.max_executor_attempts_per_l3),
                    "last_result_status": result_status,
                },
            )

        # The local repair budget is exhausted. Hand the evidence gap to the
        # Reviewer/Planner instead of silently executing the same leaf forever.
        state.executor_attempt_counts.pop(node.node_id, None)
        state.error_repair_counts.pop(node.node_id, None)
        state.empty_repair_counts.pop(node.node_id, None)
        state.ttt = self._set_node_status(state.ttt, node.node_id, TTTNodeStatus.DONE)

        reviewer_context = self._reviewer_memory.build(
            state.event,
            ttt=state.ttt,
            executions=state.executions,
        )
        self._log_retrieval("reviewer", reviewer_context)
        review = self._review(execution, reviewer_context)
        state.reviews.append(review)
        self._record_trace("review_completed", {"review": review})
        state.ready_to_submit = review["decision"] == "ready_to_submit"
        state.cannot_continue = review["decision"] == "cannot_continue"

        budget_exhausted = state.actions_issued >= self.max_steps - 1
        if state.ready_to_submit or state.cannot_continue or budget_exhausted:
            reason = (
                review["decision"]
                if state.ready_to_submit or state.cannot_continue
                else "query_budget_exhausted"
            )
            self._record_trace(
                "investigation_loop_exit_scheduled",
                {
                    "reason": reason,
                    "step_no": execution.step_no,
                    "answer_facts": review["answer_facts"],
                },
            )
            return execution

        round_review = RoundReview(
            event_id=state.event.event_id,
            round_id=len(state.reviews),
            findings=tuple(review["findings"]),
            gaps=tuple(review["gaps"]),
            recommendations=tuple(review["recommendations"]),
        )
        planner_context = self._planner_memory.build(
            state.event,
            review=round_review,
            ttt=state.ttt,
        )
        self._log_retrieval("planner", planner_context)
        state.ttt = self._replan(state.ttt, round_review, planner_context)
        state.ttt_history.append(state.ttt.to_dict())
        self._record_trace("ttt_updated", {"ttt": state.ttt.to_dict()})
        return execution

    def propose_next_action(self) -> BenchmarkAction:
        state = self._require_state()
        if state.done:
            raise RuntimeError("The ExCyTIn workflow episode is already complete")
        if state.pending_action is not None:
            raise RuntimeError(
                "Consume the pending action observation before continuing"
            )

        # The official environment counts submission as a step, so always reserve it.
        should_submit = (
            state.ready_to_submit
            or state.cannot_continue
            or state.actions_issued >= self.max_steps - 1
        )
        node = self._next_todo_leaf(state.ttt)
        if should_submit or node is None:
            return self._issue_submit()

        state.ttt = self._set_node_status(
            state.ttt, node.node_id, TTTNodeStatus.IN_PROGRESS
        )
        node = self._find_node(state.ttt, node.node_id) or node
        latest_failure = self._latest_repairable_for_node(state, node.node_id)
        episodic_query = ""
        if latest_failure is not None:
            episodic_query = (
                f"{node.title}\nresult_status={latest_failure.result_status}\n"
                f"{latest_failure.sql}\n{latest_failure.observation}"
            )
        executor_context = self._executor_memory.build(
            state.event,
            node=node,
            executions=state.executions,
            include_episodic=latest_failure is not None,
            episodic_query=episodic_query or None,
        )
        self._log_retrieval("executor", executor_context)
        sql = self._generate_sql(node, executor_context)
        executor_attempt_no = state.executor_attempt_counts.get(node.node_id, 0) + 1
        if executor_attempt_no > self.max_executor_attempts_per_l3:
            raise RuntimeError(
                f"Executor exceeded the per-L3 attempt limit for {node.node_id}"
            )
        state.executor_attempt_counts[node.node_id] = executor_attempt_no
        action = BenchmarkAction(
            action_type=BenchmarkActionType.QUERY,
            content=sql,
            step_no=state.actions_issued + 1,
            node_id=node.node_id,
        )
        state.actions_issued += 1
        state.pending_action = action
        state.pending_node = node
        state.action_history.append(action.to_dict())
        self._record_trace(
            "action_issued",
            {
                "action": action.to_dict(),
                "executor_attempt_no": executor_attempt_no,
                "max_executor_attempts_per_l3": (self.max_executor_attempts_per_l3),
            },
        )
        return action

    def get_logging(self) -> dict[str, Any]:
        if self.state is None:
            return {
                "workflow": "excytin_bench",
                "status": "idle",
                "actions_issued": 0,
            }
        state = self.state
        return {
            "workflow": "excytin_bench",
            "event_id": state.event.event_id,
            "status": "done" if state.done else "active",
            "actions_issued": state.actions_issued,
            "max_steps": self.max_steps,
            "max_executor_attempts_per_l3": self.max_executor_attempts_per_l3,
            "initial_input": state.initial_input,
            "ttt": state.ttt.to_dict(),
            "executions": [item.to_dict() for item in state.executions],
            "reviews": list(state.reviews),
            "memory_retrievals": list(state.retrieval_log),
            "ttt_history": list(state.ttt_history),
            "action_history": list(state.action_history),
            "trace": list(state.trace),
            "final_answer": state.final_answer,
        }

    def _plan_initial_ttt(
        self, event: Event, planner_context: Mapping[str, Any]
    ) -> TracebackTaskTree:
        prompt = "\n".join(
            [
                "请为以下 ExCyTIn-Bench 调查问题生成完整三层 TTT，只返回 YAML。",
                "Planner 只能使用已过滤的 Procedural Memory 和 Semantic Memory；不要生成 SQL。",
                json.dumps(
                    {"workflow_memory_context": planner_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(
                    {"event_id": event.event_id, "question": event.message},
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
        parsed = self._call_yaml_role("planner", PLANNER_SYSTEM_PROMPT, prompt)
        return self._parse_ttt(parsed, event.event_id, round_id=1)

    def _replan(
        self,
        current_ttt: TracebackTaskTree,
        review: RoundReview,
        planner_context: Mapping[str, Any],
    ) -> TracebackTaskTree:
        state = self._require_state()
        prompt = "\n".join(
            [
                "根据真实 SQL 结果的 Review 更新完整三层 TTT，只返回 YAML。",
                "保持已完成节点；只规划方向和 L3 证据目标，不生成 SQL。",
                json.dumps(
                    {"workflow_memory_context": planner_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(current_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(review.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        parsed = self._call_yaml_role("planner", PLANNER_SYSTEM_PROMPT, prompt)
        return self._parse_ttt(
            parsed,
            state.event.event_id,
            round_id=current_ttt.round_id + 1,
        )

    def _generate_sql(self, node: TTTNode, executor_context: Mapping[str, Any]) -> str:
        state = self._require_state()
        validation_error = ""
        for attempt_no in range(1, self.sql_generation_attempts + 1):
            prompt_parts = [
                "为当前 L3 生成一条 MySQL 查询。查询由外部 ExCyTInEnv 执行；本轮不要假设结果。",
                json.dumps(
                    {"question": state.initial_input}, ensure_ascii=False, indent=2
                ),
                json.dumps({"l3": node.to_dict()}, ensure_ascii=False, indent=2),
                json.dumps(
                    {"workflow_memory_context": executor_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(
                    {
                        "prior_executions": [
                            execution.to_dict() for execution in state.executions[-5:]
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
            if "episodic_memory" in executor_context:
                latest_failure = self._latest_repairable_for_node(state, node.node_id)
                if latest_failure and latest_failure.result_status == "empty_result":
                    prompt_parts.append(
                        "当前是空结果后的修复尝试。空结果表示上一条 SQL 可以执行，但其表、关联路径或过滤条件没有命中证据。"
                        "请结合 Semantic Memory 与 Episodic Memory 改换候选表、结构化字段或合法连接键；"
                        "不要只删除次要过滤条件却保留已经证实无匹配行的核心谓词，也不要原样重复上一条 SQL。"
                    )
                else:
                    prompt_parts.append(
                        "当前是数据库 error 后的修复尝试。请结合上一条 SQL、error 和按 error 检索的 Episodic Memory 修复查询。"
                    )
            else:
                prompt_parts.append(
                    "当前是该 L3 的首次 SQL 构造，Workflow 未提供 Episodic Memory；只依据 Semantic Memory 构造查询。"
                )
            if validation_error:
                prompt_parts.append(
                    f"上一次候选 SQL 未通过本地安全校验：{validation_error}。请修复后重新输出。"
                )
            parsed = self._call_yaml_role(
                "executor",
                _EXECUTOR_PROMPT,
                "\n".join(prompt_parts),
            )
            sql = str(parsed.get("sql") or "").strip()
            try:
                return ReadOnlySQLValidator.validate(sql)
            except ValueError as exc:
                validation_error = str(exc)
                if attempt_no == self.sql_generation_attempts:
                    raise
        raise RuntimeError("SQL generation failed")

    def _review(
        self,
        execution: BenchmarkExecution,
        reviewer_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self._require_state()
        prompt = "\n".join(
            [
                "请沿用原 Reviewer 输出结构。recommendations 必须是交给 Planner 的 TTT 修改建议，不要直接输出修改后的 TTT。",
                "先对照 question 与全部真实 SQL observation 判断是否已能直接作答。若问题要求的实体、属性或关系已有明确且无冲突的案件证据，必须输出 decision: ready_to_submit，并在 answer_facts 中写出直接答案事实；不要为了完成其余 TTT 节点或补充与答案无关的取证细节而继续调查。",
                "只有答案缺失、存在多个无法区分的候选值或证据冲突时才能输出 decision: continue。",
                "Workflow 仅提供 Semantic Memory 解释字段；不得将 Schema 当作当前案件证据。",
                json.dumps(
                    {"question": state.initial_input}, ensure_ascii=False, indent=2
                ),
                json.dumps(state.ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(
                    {"current_execution": execution.to_dict()},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(
                    {"workflow_memory_context": reviewer_context},
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
        parsed = self._call_yaml_role("reviewer", REVIEWER_SYSTEM_PROMPT, prompt)
        decision = str(parsed.get("decision") or "continue").strip().lower()
        if decision not in {"continue", "ready_to_submit", "cannot_continue"}:
            decision = "continue"
        return {
            "round_id": len(state.reviews) + 1,
            "step_no": execution.step_no,
            "decision": decision,
            "findings": self._string_list(parsed.get("findings")),
            "gaps": self._string_list(parsed.get("gaps")),
            "recommendations": self._string_list(parsed.get("recommendations")),
            "answer_facts": self._string_list(parsed.get("answer_facts")),
        }

    def _issue_submit(self) -> BenchmarkAction:
        state = self._require_state()
        evidence = [execution.to_dict() for execution in state.executions]
        prompt = "\n".join(
            [
                json.dumps(
                    {"question": state.initial_input}, ensure_ascii=False, indent=2
                ),
                json.dumps({"executions": evidence}, ensure_ascii=False, indent=2),
                json.dumps(
                    {
                        "review_answer_facts": [
                            fact
                            for review in state.reviews
                            for fact in review.get("answer_facts", [])
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
        parsed = self._call_yaml_role("answer_synthesizer", _ANSWER_PROMPT, prompt)
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            raise ValueError("Final answer synthesizer returned an empty answer")
        action = BenchmarkAction(
            action_type=BenchmarkActionType.SUBMIT,
            content=answer,
            step_no=state.actions_issued + 1,
        )
        state.actions_issued += 1
        state.pending_action = action
        state.final_answer = answer
        state.done = True
        state.action_history.append(action.to_dict())
        self._record_trace("action_issued", {"action": action.to_dict()})
        self._record_trace("workflow_completed", {"final_answer": answer})
        return action

    def _log_retrieval(self, role: str, context: Mapping[str, Any]) -> None:
        if self.state is None:
            return
        semantic = context.get("semantic_memory") or {}
        procedural = context.get("procedural_memory") or {}
        episodic = context.get("episodic_memory") or {}
        item = {
            "role": role,
            "allowed_memory_types": list(context.get("allowed_memory_types") or []),
            "table_ids": [
                table.get("table_id") for table in semantic.get("tables", [])
            ],
            "procedure_ids": [
                procedure.get("procedure_id")
                for procedure in procedural.get("procedures", [])
            ],
            "episode_ids": [
                episode.get("episode_id") for episode in episodic.get("episodes", [])
            ],
        }
        self.state.retrieval_log.append(item)
        self._record_trace("memory_retrieved", item)

    def _record_trace(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.state is None:
            return
        self.state.trace.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": event_type,
                "payload": dict(payload),
            }
        )

    def _require_state(self) -> WorkflowState:
        if self.state is None:
            raise RuntimeError("Call start() before using the ExCyTIn workflow")
        return self.state

    def _call_yaml_role(
        self,
        role: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        prompt = user_prompt
        for attempt in range(1, self.yaml_response_attempts + 1):
            response_text = self._llm(system_prompt, prompt)
            try:
                parsed = self._parse_response(response_text)
            except Exception as exc:  # noqa: BLE001 - retry malformed model output.
                last_error = exc
                self._record_trace(
                    "role_yaml_invalid",
                    {
                        "role": role,
                        "attempt": attempt,
                        "error": f"{type(exc).__name__}: {exc}",
                        "raw_response": response_text[:10_000],
                    },
                )
                prompt = "\n".join(
                    [
                        user_prompt,
                        "上一次输出不是合法 YAML。请修复格式，只输出合法 YAML，不要使用额外说明。",
                        f"解析错误：{type(exc).__name__}: {exc}",
                    ]
                )
                continue
            self._record_trace(
                "role_yaml_parsed",
                {
                    "role": role,
                    "attempt": attempt,
                    "response_type": parsed.get("response_type"),
                },
            )
            return parsed
        if last_error is not None:
            raise last_error
        raise ValueError(f"{role} returned no parseable YAML")

    def _compact_observation(self, observation: str) -> tuple[str, bool]:
        if len(observation) <= self.max_observation_chars:
            return observation, False
        omitted = len(observation) - self.max_observation_chars
        return (
            observation[: self.max_observation_chars]
            + f"\n...[truncated {omitted} characters by workflow]",
            True,
        )

    @staticmethod
    def _latest_repairable_for_node(
        state: WorkflowState, node_id: str
    ) -> BenchmarkExecution | None:
        for execution in reversed(state.executions):
            if execution.node_id != node_id:
                continue
            if not execution.query_success or execution.result_status == "empty_result":
                return execution
            return None
        return None

    @staticmethod
    def _sanitize_initial_input(value: str | Mapping[str, Any]) -> str:
        if isinstance(value, Mapping):
            # Explicit allowlist prevents answer/solution/ground-truth leakage.
            parts = [
                str(value.get(key) or "").strip() for key in ("context", "question")
            ]
            normalized = " ".join(part for part in parts if part)
        else:
            normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("ExCyTIn initial input is empty")
        return normalized

    @staticmethod
    def _parse_response(response_text: str) -> dict[str, Any]:
        parsed = parse_yaml_response(response_text)
        if not parsed:
            raise ValueError("Role returned empty or non-YAML content")
        return parsed

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list | tuple):
            return [str(item) for item in value]
        return [str(value)]

    @staticmethod
    def _infer_query_success(observation: str) -> bool:
        text = observation.strip()
        return re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):", text) is None

    @staticmethod
    def _result_status(observation: str, success: bool) -> str:
        if not success:
            return "error"
        if observation.strip() in {"", "[]", "()"}:
            return "empty_result"
        return "rows_returned"

    @classmethod
    def _parse_ttt(
        cls, parsed: Mapping[str, Any], event_id: str, *, round_id: int
    ) -> TracebackTaskTree:
        payload = parsed.get("ttt") or parsed.get("tree")
        if not isinstance(payload, Mapping):
            raise TypeError("Planner response is missing ttt/tree")
        roots = payload.get("root_nodes") or payload.get("nodes") or []
        if not isinstance(roots, list) or not roots:
            raise ValueError("Planner returned an empty TTT")
        root_nodes = tuple(
            cls._parse_node(item, path=str(index), depth=1)
            for index, item in enumerate(roots, start=1)
        )
        tree = TracebackTaskTree(
            event_id=event_id,
            round_id=round_id,
            root_nodes=root_nodes,
        )
        if cls._next_todo_leaf(tree) is None and not cls._all_leaves_terminal(tree):
            raise ValueError("Planner TTT has no executable L3 node")
        return tree

    @classmethod
    def _parse_node(cls, value: Any, *, path: str, depth: int) -> TTTNode:
        if not isinstance(value, Mapping):
            raise TypeError(f"TTT node {path} is not an object")
        children_raw = value.get("children") or []
        if not isinstance(children_raw, list):
            raise TypeError(f"TTT node {path} children must be a list")
        if depth >= 3 and children_raw:
            raise ValueError("TTT must contain exactly three levels")
        if depth < 3 and not children_raw:
            raise ValueError(f"TTT node {path} ends before L3")
        children = tuple(
            cls._parse_node(
                child,
                path=f"{path}-{index}",
                depth=depth + 1,
            )
            for index, child in enumerate(children_raw, start=1)
        )
        try:
            status = TTTNodeStatus(str(value.get("status") or TTTNodeStatus.TODO.value))
        except ValueError:
            status = TTTNodeStatus.TODO
        return TTTNode(
            node_id=path,
            title=str(value.get("title") or path),
            status=status,
            children=children,
            metadata={},
        )

    @staticmethod
    def _next_todo_leaf(tree: TracebackTaskTree) -> TTTNode | None:
        def walk(node: TTTNode) -> TTTNode | None:
            if not node.children:
                return node if node.status == TTTNodeStatus.TODO else None
            for child in node.children:
                found = walk(child)
                if found is not None:
                    return found
            return None

        for root in tree.root_nodes:
            found = walk(root)
            if found is not None:
                return found
        return None

    @staticmethod
    def _all_leaves_terminal(tree: TracebackTaskTree) -> bool:
        def terminal(node: TTTNode) -> bool:
            if not node.children:
                return node.status in {TTTNodeStatus.DONE, TTTNodeStatus.NOT_APPLICABLE}
            return all(terminal(child) for child in node.children)

        return all(terminal(root) for root in tree.root_nodes)

    @staticmethod
    def _find_node(tree: TracebackTaskTree, node_id: str) -> TTTNode | None:
        def walk(node: TTTNode) -> TTTNode | None:
            if node.node_id == node_id:
                return node
            for child in node.children:
                found = walk(child)
                if found is not None:
                    return found
            return None

        for root in tree.root_nodes:
            found = walk(root)
            if found is not None:
                return found
        return None

    @staticmethod
    def _set_node_status(
        tree: TracebackTaskTree, node_id: str, status: TTTNodeStatus
    ) -> TracebackTaskTree:
        def update(node: TTTNode) -> TTTNode:
            node_status = status if node.node_id == node_id else node.status
            children = tuple(update(child) for child in node.children)
            if children and all(
                child.status in {TTTNodeStatus.DONE, TTTNodeStatus.NOT_APPLICABLE}
                for child in children
            ):
                node_status = TTTNodeStatus.DONE
            return TTTNode(
                node_id=node.node_id,
                title=node.title,
                status=node_status,
                children=children,
                metadata=dict(node.metadata),
            )

        return TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tuple(update(root) for root in tree.root_nodes),
            created_at=tree.created_at,
        )


__all__ = [
    "BenchmarkAction",
    "BenchmarkActionType",
    "BenchmarkExecution",
    "ExcytinBenchWorkflow",
    "ReadOnlySQLValidator",
]
