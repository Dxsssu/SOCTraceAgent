from __future__ import annotations

import json
import re
import secrets
import string
from collections.abc import Callable, Mapping
from dataclasses import replace, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import sqlglot
import yaml

from src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge import entry_id as recovery_entry_id
from src.agent.llm import call_llm, parse_yaml_response
from src.agent.executor import SQL_EXECUTOR_SYSTEM_PROMPT
from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.agent.reviewer import REVIEWER_SYSTEM_PROMPT
from src.agent.verifier import VERIFIER_SYSTEM_PROMPT, validate_verification
from src.schema.ttt_updates import next_task, change_node, resolved
from src.schema.investigation import task_input
from src.schema.reviewer_output import parse_review
from src.schema.itt import parse_itt, parse_itt_response, itt_payload, planner_prompt, execution_fields, task_payload, review_payload
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
    verifications: list[dict[str, Any]] = field(default_factory=list)
    error_repair_counts: dict[str, int] = field(default_factory=dict)
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
        max_executor_attempts_per_l3: int = 2,
        max_error_repairs: int = 1,
        yaml_response_attempts: int = 2,
        max_observation_chars: int = 20_000,
    ) -> None:
        if max_steps < 2:
            raise ValueError("max_steps must reserve at least one query and one submit")
        self.max_steps = max_steps
        self._llm = llm or self._default_llm
        self.memory_contexts = memory_contexts or MemoryContextService(procedure_selector=self._llm)
        self._planner_memory = self.memory_contexts.planner_view()
        self.max_executor_attempts_per_l3 = min(2, max(1, max_executor_attempts_per_l3))
        self.max_error_repairs = min(1, max(0, max_error_repairs))
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

    def _call_role_llm(self, system_prompt: str, user_prompt: str) -> str:
        nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
        return self._llm(f"{nonce}\n{system_prompt}", user_prompt)

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
        investigation_input = (
            {
                key: str(initial_input.get(key) or "").strip()
                for key in ("context", "question")
            }
            if isinstance(initial_input, Mapping)
            else {"context": "", "question": question}
        )
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
                "runtime_info": safe_runtime_info,
                "investigation_input": investigation_input,
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
            ttt = self._plan_initial_ttt(
                event, planner_context, investigation_input=investigation_input
            )
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
        code, result, _ = execution_fields(state.executions, node.node_id)
        state.ttt = change_node(state.ttt, node.node_id,
            metadata=dict(current.metadata, execution_code=code, execution_result="", execution_result_source="reviewer"),
            evidence_refs=(*current.evidence_refs, execution.execution_id),
            result_summary="")
        self._record_trace(
            "observation_received",
            {"execution": execution.to_dict()},
        )
        state.pending_action = None
        state.pending_node = None
        repair_trigger = "sql_error" if not success else ""
        executor_attempt_count = state.executor_attempt_counts.get(node.node_id, 0)
        executor_attempt_budget_exhausted = (
            executor_attempt_count >= self.max_executor_attempts_per_l3
        )
        state.ttt = self._set_node_status(
            state.ttt,
            node.node_id,
            TTTNodeStatus.TODO if repair_trigger else TTTNodeStatus.DONE,
        )

        state.ttt_history.append(state.ttt.to_dict())
        self._record_trace("ttt_execution_updated", {"ttt": state.ttt.to_dict()})

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
        state.ttt = self._set_node_status(state.ttt, node.node_id,
            TTTNodeStatus.BLOCKED if repair_trigger else TTTNodeStatus.DONE)

        review = self._review(execution)
        current = self._find_node(state.ttt, node.node_id)
        state.ttt = change_node(state.ttt, node.node_id,
            metadata=dict(current.metadata, execution_result=review["summary"], execution_result_source="reviewer"),
            result_summary="")
        state.reviews.append(review)
        self._record_trace("review_completed", {"review": review})
        verdict = self._verify_progress(review)
        state.ready_to_submit = verdict["can_answer"] == "yes"
        state.cannot_continue = False
        budget_exhausted = state.actions_issued >= self.max_steps - 1
        if state.ready_to_submit:
            state.final_answer = verdict["answer"]
            for root in state.ttt.root_nodes:
                state.ttt = change_node(state.ttt, root.node_id, status=TTTNodeStatus.RESOLVED)
            state.ttt = replace(state.ttt, next_task_id=None)
        elif budget_exhausted:
            state.cannot_continue = True
        state.ttt_history.append(state.ttt.to_dict())
        if state.ready_to_submit or budget_exhausted:
            self._record_trace("investigation_loop_exit_scheduled", {
                "reason": "verifier_confirmed" if state.ready_to_submit else "query_budget_exhausted",
                "step_no": execution.step_no,
            })
            return execution

        # Only the Verifier can finish the investigation; reopen planner goals
        # if their status was prematurely marked complete.
        def reopen(n):
            return replace(n, status=TTTNodeStatus.OPEN if n.level < 3 else n.status,
                           children=tuple(reopen(c) for c in n.children))
        state.ttt = replace(state.ttt, root_nodes=tuple(reopen(n) for n in state.ttt.root_nodes))
        round_review = RoundReview(
            event_id=state.event.event_id,
            round_id=len(state.reviews),
            findings=tuple(review["findings"]),
            gaps=("Verifier：当前尚不能回答问题，请继续调查。",),
            recommendations=tuple(review["recommendations"]),
        )
        planner_context = self._planner_memory.build(
            state.event,
            review=round_review,
            ttt=state.ttt,
            executions=state.executions,
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
            or state.actions_issued >= self.max_steps - 1
        )
        node = self._next_todo_leaf(state.ttt)
        if should_submit:
            return self._issue_submit()
        if node is None:
            gap_review = RoundReview(event_id=state.event.event_id, round_id=state.ttt.round_id,
                gaps=("Verifier 尚未确认答案且没有可执行任务，请新增一个 todo 的 T 继续调查。",))
            context = self._planner_memory.build(state.event, review=gap_review, ttt=state.ttt, executions=state.executions)
            self._log_retrieval("planner", context)
            state.ttt = self._replan(state.ttt, gap_review, context)
            state.ttt_history.append(state.ttt.to_dict())
            self._record_trace("ttt_updated", {"ttt": state.ttt.to_dict()})
            return self.propose_next_action()

        state.ttt = self._set_node_status(
            state.ttt, node.node_id, TTTNodeStatus.IN_PROGRESS
        )
        node = self._find_node(state.ttt, node.node_id) or node
        sql = self._generate_sql(node)
        executor_attempt_no = state.executor_attempt_counts.get(node.node_id, 0) + 1
        if executor_attempt_no > self.max_executor_attempts_per_l3:
            raise RuntimeError(
                f"Executor exceeded the per-L3 attempt limit for {node.node_id}"
            )
        state.executor_attempt_counts[node.node_id] = executor_attempt_no
        codes = [e.sql for e in state.executions if e.node_id == node.node_id] + [sql]
        code = codes[0] if len(codes) == 1 else json.dumps(codes, ensure_ascii=False)
        state.ttt = change_node(state.ttt, node.node_id,
            metadata=dict(node.metadata, execution_code=code))
        node = self._find_node(state.ttt, node.node_id)
        self._record_trace("executor_sql_written", {"node_id": node.node_id, "sql": sql})
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
                "ltm_enabled": self.memory_contexts.enabled,
                "actions_issued": 0,
                "initial_input": self._bootstrap_initial_input,
                "trace": list(self._bootstrap_trace),
            }
        state = self.state
        return {
            "workflow": "excytin_bench",
            "ltm_enabled": self.memory_contexts.enabled,
            "event_id": state.event.event_id,
            "status": "done" if state.done else "active",
            "actions_issued": state.actions_issued,
            "max_steps": self.max_steps,
            "max_executor_attempts_per_l3": self.max_executor_attempts_per_l3,
            "initial_input": state.initial_input,
            "ttt": state.ttt.to_dict(),
            "executions": [item.to_dict() for item in state.executions],
            "reviews": list(state.reviews),
            "verifications": list(state.verifications),
            "memory_retrievals": list(state.retrieval_log),
            "ttt_history": list(state.ttt_history),
            "action_history": list(state.action_history),
            "trace": list(state.trace),
            "final_answer": state.final_answer,
        }

    def _plan_initial_ttt(
        self,
        event: Event,
        planner_context: Mapping[str, Any],
        *,
        investigation_input: Mapping[str, str],
    ) -> TracebackTaskTree:
        prompt = "下面是此次事件的背景和调查问题，请你根据要求初始化并输出ITT。\n" + json.dumps(
            dict(investigation_input), ensure_ascii=False, indent=2
        )
        parsed = self._call_yaml_role(
            "planner",
            planner_prompt(PLANNER_SYSTEM_PROMPT, planner_context),
            prompt,
            validator=lambda value: self._parse_ttt(
                value,
                event.event_id,
                round_id=1,
                procedures=planner_context.get("procedural_memory", {}).get("procedures", []),
            ),
        )
        return self._parse_ttt(parsed, event.event_id, round_id=1,
            procedures=planner_context.get("procedural_memory", {}).get("procedures", []))

    def _replan(
        self,
        current_ttt: TracebackTaskTree,
        review: RoundReview,
        planner_context: Mapping[str, Any],
    ) -> TracebackTaskTree:
        state = self._require_state()
        prompt = yaml.safe_dump(dict(task_input(state.event),
            itt=itt_payload(current_ttt, state.executions, planner_view=True),
            review={"summary": "；".join((*review.findings, *review.gaps))}),
            allow_unicode=True, sort_keys=False)
        next_round = current_ttt.round_id + 1
        def validate(value):
            tree = parse_itt(value, state.event.event_id, next_round,
                             previous=current_ttt, executions=state.executions,
                             procedures=planner_context.get("procedural_memory", {}).get("procedures", []))
            if self._next_todo_leaf(tree) is None:
                raise ValueError("Verifier has not confirmed an answer; add a new todo T to continue investigation")
            return tree
        parsed = self._call_yaml_role(
            "planner", planner_prompt(PLANNER_SYSTEM_PROMPT, planner_context),
            prompt, validator=validate,
        )
        return validate(parsed)

    def _generate_sql(self, node: TTTNode) -> str:
        state = self._require_state()
        failure = self._latest_repairable_for_node(state, node.node_id)
        recovery = None
        if failure is not None and not failure.query_success:
            recoveries = self.memory_contexts.executor_recoveries(
                state.event, error=failure.observation, sql=failure.sql
            ) if self.memory_contexts.enabled else []
            recovery = {"failed_sql": failure.sql, "error": failure.observation}
            self._record_trace("executor_error_recovery", {
                "node_id": node.node_id, "execution_id": failure.execution_id,
                "matched_count": len(recoveries), "recoveries": recoveries,
                "recovery_ids": [recovery_entry_id(e) for e in recoveries]})
        payload = task_payload(node)
        if recovery is not None:
            payload["error_recovery"] = recovery
        # One generation per SQL attempt. Format/local validation errors fail
        # immediately; only actual database errors enter the repair path.
        response = self._call_role_llm(SQL_EXECUTOR_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        parsed = self._parse_response(response)
        self._record_trace("role_yaml_parsed", {
            "role": "executor", "attempt": 1, "response_type": parsed.get("response_type"),
        })
        if node.metadata.get("query_template") and recovery is None:
            from src.memory.longterm_memory.procedural_memory.excytin_bench.templates import render_sql
            template = node.metadata["query_template"]
            if set(parsed) != {"template_id", "bindings"} or parsed.get("template_id") != template["id"]:
                raise ValueError("Return only the supplied template_id and bindings")
            sql = render_sql(template, parsed["bindings"])
        else:
            sql = str(parsed.get("sql") or "").strip()
        return ReadOnlySQLValidator.validate(sql)

    def _review(self, execution: BenchmarkExecution) -> dict[str, Any]:
        state = self._require_state()
        prompt = json.dumps({
            **task_input(state.event),
            **review_payload(state.ttt, execution),
        }, ensure_ascii=False)
        parsed = self._call_yaml_role("reviewer", REVIEWER_SYSTEM_PROMPT, prompt,
                                      validator=parse_review)
        review = parse_review(parsed)
        # Legacy storage fields are populated by the runtime, never requested
        # from the model. The Verifier controls loop completion.
        return {
            "round_id": len(state.reviews) + 1,
            "step_no": execution.step_no,
            **review,
            "findings": [review["summary"]],
            "gaps": [],
            "recommendations": [],
            "answer_facts": [],
        }

    def _issue_submit(self) -> BenchmarkAction:
        """Submit the Verifier answer without another model call."""
        state = self._require_state()
        if state.ready_to_submit:
            answer = state.final_answer
            if not answer:
                raise ValueError("Verifier confirmed without an answer")
        else:
            answer = "无法确认答案：调查结束，尚无充分证据。"
            state.cannot_continue = True
            for root in state.ttt.root_nodes:
                state.ttt = change_node(state.ttt, root.node_id, status=TTTNodeStatus.BLOCKED)
            state.ttt = replace(state.ttt, next_task_id=None)
            state.ttt_history.append(state.ttt.to_dict())
        action = BenchmarkAction(action_type=BenchmarkActionType.SUBMIT,
                                 content=answer, step_no=state.actions_issued + 1)
        state.actions_issued += 1
        state.pending_action = action
        state.final_answer = answer
        state.done = True
        state.action_history.append(action.to_dict())
        self._record_trace("action_issued", {"action": action.to_dict()})
        self._record_trace("workflow_completed", {"final_answer": answer})
        return action

    def _verify_progress(self, review):
        state = self._require_state()
        prompt = json.dumps({
            **task_input(state.event),
            "itt": itt_payload(state.ttt, state.executions),
            "reviewer": {"summary": review["summary"]},
        }, ensure_ascii=False)
        verdict = self._call_yaml_role("verifier", VERIFIER_SYSTEM_PROMPT, prompt,
                                      validator=validate_verification)
        verdict = validate_verification(verdict)
        state.verifications.append(verdict)
        self._record_trace("answer_verified", verdict)
        return verdict

    def _log_retrieval(self, role: str, context: Mapping[str, Any]) -> None:
        if self.state is None or not self.memory_contexts.enabled:
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
            "procedure_names": [p["name"] for p in procedural.get("procedures", [])],
            "procedure_application": procedural.get("application"),
            "template_ids": [p.get("id") for p in procedural.get("procedures", [])],
            "procedure_retrieval": procedural.get("retrieval"),
            "procedure_reuse": procedural.get("reuse"),
            "semantic_status": semantic.get("status"),
            "semantic_reuse": semantic.get("reuse"),
            "recovery_ids": [
                recovery_entry_id(entry) for entry in episodic.get("recoveries", [])
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
        validator: Callable[[Any], object] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        last_error: Exception | None = None
        prompt = user_prompt
        for attempt in range(1, self.yaml_response_attempts + 1):
            response_text = self._call_role_llm(system_prompt, prompt)
            try:
                parsed = parse_itt_response(response_text) if role == "planner" else self._parse_response(response_text)
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
                        "上一次输出未通过校验。请根据错误修正字段、长度或 YAML 格式，不要使用额外说明。",
                        f"解析错误：{type(exc).__name__}: {exc}",
                    ]
                )
                continue
            self._record_trace(
                "role_yaml_parsed",
                {
                    "role": role,
                    "attempt": attempt,
                    "response_type": "ITT" if isinstance(parsed, list) else parsed.get("response_type"),
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
            if not execution.query_success:
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
        cls, parsed: list[dict[str, Any]], event_id: str, *, round_id: int, procedures=()
    ) -> TracebackTaskTree:
        return parse_itt(parsed, event_id, round_id, procedures=procedures)

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
