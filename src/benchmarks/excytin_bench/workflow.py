from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import sqlglot

from src.agent.llm import call_llm, parse_yaml_response
from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.agent.reviewer import REVIEWER_SYSTEM_PROMPT
from src.schema.ttt_updates import apply_updates, parse_initial, next_task, change_node, resolved
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

    @property
    def execution_id(self) -> str:
        return f"E{self.step_no}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
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
        # Inspect SQL tokens, not log values: command-line evidence may contain
        # words such as SET or DELETE inside a quoted string literal.
        tokens = sqlglot.Dialect.get_or_raise("mysql").tokenize(normalized)
        literal_types = {"STRING", "NATIONAL_STRING", "RAW_STRING", "UNICODE_STRING", "HEX_STRING", "BIT_STRING", "BYTE_STRING"}
        inspected = normalized
        for token in reversed(tokens):
            if token.token_type.name in literal_types:
                inspected = inspected[:token.start] + " " + inspected[token.end + 1:]
        if cls._FORBIDDEN.search(inspected):
            raise ValueError("SQL contains a forbidden operation")
        try:
            expressions = sqlglot.parse(normalized, read="mysql")
        except sqlglot.errors.ParseError as exc:
            raise ValueError(f"SQL cannot be parsed as MySQL: {exc}") from exc
        if len(expressions) != 1 or expressions[0] is None:
            raise ValueError("Exactly one SQL statement is required")
        return normalized.rstrip(";").strip() + ";"


_EXECUTOR_PROMPT = """
你是 ExCyTIn-Bench 调查流程中的 Executor。

## 角色定位
将 Workflow 指定的一个 TTT L3 任务转化为一条交给外部环境执行的 MySQL 查询。

## 输入信息
- 原始问题、当前 L3 节点和已有执行记录。
- Workflow 提供的 Semantic Memory。
- 仅在当前 L3 上一条 SQL 返回数据库 error 或空结果时提供的 Episodic Memory。

## 工作流程
1. 理解当前 L3 所需证据，根据 Semantic Memory 选择表字段与合法连接键。
2. 首次查询仅使用 Semantic Memory；修复时结合真实 error/空结果及已提供的 Episodic Memory。
3. 输出一条只读 SQL，等待外部环境返回真实结果。

## 约束条件
- 不使用 Procedural Memory，不改变调查方向、不修改 TTT、不生成最终答案。
- Semantic Memory 是表名、字段名和连接关系的硬约束；Episodic Memory 仅供参考查询结构和修复方式，不是案件证据。
- 字段存在不代表每条记录都有值；空结果后不能继续假设多个实体一定共处于同一行。
- 告警名称与类别优先参考 AlertInfo；受影响实体、人工分配、自动调查与处置元数据优先参考 SecurityAlert 的 CompromisedEntity 和 ExtendedProperties；AlertEvidence 通过 AlertId 提供结构化证据实体。
- 只生成一条 MySQL 只读语句，不执行写入、多语句或副作用操作；结果返回前不臆测结果。

## 输出格式
只输出一个合法 YAML 对象，不使用 Markdown 代码围栏或额外说明。
response_type 固定为 EXECUTE_SQL；sql 为包含一条 MySQL 只读语句的字符串。
不要输出普通 Executor 的 execution/parameters 嵌套结构。

## 输出示例
response_type: EXECUTE_SQL
sql: "SELECT AlertId FROM AlertEvidence LIMIT 5;"
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
        self._bootstrap_trace: list[dict[str, Any]] = []
        self._bootstrap_status = "idle"
        self._bootstrap_event_id = ""
        self._bootstrap_initial_input = ""

    @staticmethod
    def _default_llm(system_prompt: str, user_prompt: str) -> str:
        return call_llm(system_prompt, user_prompt)

    def reset(self) -> None:
        self.state = None
        self._bootstrap_trace.clear()
        self._bootstrap_status = "idle"
        self._bootstrap_event_id = ""
        self._bootstrap_initial_input = ""

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
        self._bootstrap_trace.clear()
        self._bootstrap_status = "initializing"
        self._bootstrap_event_id = event.event_id
        self._bootstrap_initial_input = question
        self._record_trace(
            "workflow_starting",
            {"event_id": event.event_id},
        )
        planner_context = self._planner_memory.build(event)
        try:
            ttt = self._plan_initial_ttt(event, planner_context)
        except Exception as exc:
            self._bootstrap_status = "failed"
            self._record_trace(
                "workflow_start_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        bootstrap_trace = list(self._bootstrap_trace)
        self.state = WorkflowState(event=event, ttt=ttt, initial_input=question)
        self.state.trace.extend(bootstrap_trace)
        self._bootstrap_trace.clear()
        self._bootstrap_status = "idle"
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
        current = self._find_node(state.ttt, node.node_id)
        state.ttt = change_node(state.ttt, node.node_id,
            evidence_refs=(*current.evidence_refs, execution.execution_id),
            result_summary=f"{result_status}; observation_truncated={observation_truncated}")
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
        state.ttt = change_node(state.ttt, node.node_id,
            result_summary="; ".join(review["findings"] + review["gaps"]) or result_status)
        state.reviews.append(review)
        self._record_trace("review_completed", {"review": review})
        state.ready_to_submit = review["decision"] == "ready_to_submit"
        state.cannot_continue = review["decision"] == "cannot_continue"

        if state.ready_to_submit or state.cannot_continue:
            root = state.ttt.root_nodes[0]
            state.ttt = change_node(state.ttt, root.node_id,
                status=TTTNodeStatus.RESOLVED if state.ready_to_submit else TTTNodeStatus.BLOCKED,
                result_summary="; ".join(review["answer_facts"] + review["findings"] + review["gaps"]) or review["decision"],
                evidence_refs=tuple(e.execution_id for e in state.executions))
            state.ttt = replace(state.ttt, next_task_id=None)
        state.ttt_history.append(state.ttt.to_dict())
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
        if should_submit:
            return self._issue_submit()
        if node is None:
            if state.ttt.root_nodes[0].status == TTTNodeStatus.BLOCKED:
                state.cannot_continue = True
                return self._issue_submit()
            if resolved(state.ttt):
                state.ready_to_submit = True
                return self._issue_submit()
            gap_review = RoundReview(event_id=state.event.event_id, round_id=state.ttt.round_id,
                gaps=("目标未解决且没有可执行 L3，请展开下一步或明确阻塞原因。",))
            context = self._planner_memory.build(state.event, review=gap_review, ttt=state.ttt)
            self._log_retrieval("planner", context)
            state.ttt = self._replan(state.ttt, gap_review, context)
            state.ttt_history.append(state.ttt.to_dict())
            self._record_trace("ttt_updated", {"ttt": state.ttt.to_dict()})
            return self.propose_next_action()

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
                "event_id": self._bootstrap_event_id,
                "status": self._bootstrap_status,
                "actions_issued": 0,
                "initial_input": self._bootstrap_initial_input,
                "trace": list(self._bootstrap_trace),
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
                "请为以下 ExCyTIn-Bench 调查问题初始化最小 TTT，仅展开当前必要任务，只返回 YAML。",
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
        parsed = self._call_yaml_role(
            "planner",
            PLANNER_SYSTEM_PROMPT,
            prompt,
            validator=lambda value: self._parse_ttt(
                value,
                event.event_id,
                round_id=1,
            ),
        )
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
                "根据真实 SQL 结果的 Review 输出带 base_version 的局部 updates 和 next_task_id，只返回 YAML。",
                "保持已完成节点；只规划方向和 L3 证据目标，不生成 SQL。",
                json.dumps(
                    {"workflow_memory_context": planner_context},
                    ensure_ascii=False,
                    indent=2,
                ),
                json.dumps(current_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(review.to_dict(), ensure_ascii=False, indent=2),
                json.dumps({"executions": [e.to_dict() for e in state.executions]}, ensure_ascii=False),
            ]
        )
        next_round = current_ttt.round_id + 1
        parsed = self._call_yaml_role(
            "planner",
            PLANNER_SYSTEM_PROMPT,
            prompt,
            validator=lambda value: apply_updates(current_ttt, value, round_id=next_round,
                known_evidence=[e.execution_id for e in state.executions]),
        )
        return apply_updates(current_ttt, parsed, round_id=next_round,
            known_evidence=[e.execution_id for e in state.executions])

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
                "请根据原始问题和当前 L3 的真实执行结果判断应提交答案还是继续调查，只返回 YAML。",
                "Semantic Memory 只用于解释表和字段，不能作为当前案件证据。任务 done 不等于问题解决，空结果不能否定假设。明确 findings/gaps 和原始问题是否已有充分证据。",
                json.dumps({"ttt": state.ttt.to_dict(), "executions": [e.to_dict() for e in state.executions]}, ensure_ascii=False),
                json.dumps(
                    {"question": state.initial_input}, ensure_ascii=False, indent=2
                ),
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
        reasonings = str(parsed.get("reasonings") or "").strip()
        findings = self._string_list(parsed.get("findings"))
        if reasonings and not findings:
            findings = [reasonings]
        return {
            "round_id": len(state.reviews) + 1,
            "step_no": execution.step_no,
            "decision": decision,
            "reasonings": reasonings,
            "findings": findings,
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
        item = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "payload": dict(payload),
        }
        if self.state is None:
            self._bootstrap_trace.append(item)
        else:
            self.state.trace.append(item)

    def _require_state(self) -> WorkflowState:
        if self.state is None:
            raise RuntimeError("Call start() before using the ExCyTIn workflow")
        return self.state

    def _call_yaml_role(
        self,
        role: str,
        system_prompt: str,
        user_prompt: str,
        *,
        validator: Callable[[Mapping[str, Any]], object] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        prompt = user_prompt
        for attempt in range(1, self.yaml_response_attempts + 1):
            response_text = self._llm(system_prompt, prompt)
            try:
                parsed = self._parse_response(response_text)
                if validator is not None:
                    validator(parsed)
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
        return parse_initial(payload, event_id, round_id)

    _next_todo_leaf = staticmethod(next_task)
    _all_leaves_terminal = staticmethod(resolved)

    @staticmethod
    def _find_node(tree: TracebackTaskTree, node_id: str) -> TTTNode | None:
        from src.schema.ttt_updates import walk
        return next((n for n, _ in walk(tree) if n.node_id == node_id), None)

    @staticmethod
    def _set_node_status(tree: TracebackTaskTree, node_id: str, status: TTTNodeStatus) -> TracebackTaskTree:
        return change_node(tree, node_id, status=status)


__all__ = [
    "BenchmarkAction",
    "BenchmarkActionType",
    "BenchmarkExecution",
    "ExcytinBenchWorkflow",
    "ReadOnlySQLValidator",
]
