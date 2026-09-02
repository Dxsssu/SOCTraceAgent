from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from src.agent.llm import call_llm, parse_yaml_response

from .workflow import BenchmarkActionType, ExcytinBenchWorkflow

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUESTION_DIR = PROJECT_ROOT / "data/excytin-bench/huggingface/questions/test"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "runtime/excytin_bench/runs"
INCIDENT_PORTS: Mapping[str, int] = {
    "incident_5": 3306,
    "incident_38": 3307,
    "incident_34": 3308,
    "incident_39": 3309,
    "incident_55": 3310,
    "incident_134": 3311,
    "incident_166": 3312,
    "incident_322": 3313,
}
_SCHEMA_ERROR_RE = re.compile(
    r"unknown column|unknown table|table .* doesn't exist|no such table|"
    r"ambiguous column|invalid identifier",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    incident: str
    qid: int
    question: dict[str, Any]

    @property
    def case_id(self) -> str:
        return f"{self.incident}-q{self.qid:04d}"

    def manifest_view(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "incident": self.incident,
            "qid": self.qid,
            "question": str(self.question.get("question") or ""),
        }


class QueryExecutor(Protocol):
    def execute(self, sql: str) -> tuple[str, bool, int]: ...

    def close(self) -> None: ...


class MySQLQueryExecutor:
    """Minimal ExCyTIn-compatible MySQL execution boundary."""

    def __init__(
        self,
        incident: str,
        *,
        password: str,
        database: str = "env_monitor_db",
        max_entry_return: int = 15,
        max_str_len: int = 100_000,
    ) -> None:
        try:
            import mysql.connector
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "mysql-connector-python is required; run `uv sync` first"
            ) from exc
        try:
            port = INCIDENT_PORTS[incident]
        except KeyError as exc:
            raise ValueError(f"Unknown ExCyTIn incident: {incident}") from exc
        self.max_entry_return = max_entry_return
        self.max_str_len = max_str_len
        self.connection = mysql.connector.connect(
            host="127.0.0.1",
            port=port,
            user="root",
            password=password,
            database=database,
        )
        self.cursor = self.connection.cursor()
        self.cursor.execute("SET SESSION MAX_EXECUTION_TIME=30000")

    def execute(self, sql: str) -> tuple[str, bool, int]:
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            row_count = len(rows)
            observation = str(rows)
            if (
                len(observation) > self.max_str_len
                and row_count > self.max_entry_return
            ):
                observation = (
                    f"Retrieved {row_count} entries. Displaying first "
                    f"{self.max_entry_return} entries.\n"
                    f"{rows[: self.max_entry_return]}"
                )
            return observation, True, row_count
        except Exception as exc:  # noqa: BLE001 - connector error hierarchy varies.
            return f"{type(exc).__name__}: {exc}", False, 0

    def close(self) -> None:
        self.cursor.close()
        self.connection.close()


WorkflowFactory = Callable[[int], ExcytinBenchWorkflow]
ExecutorFactory = Callable[[str], QueryExecutor]


def load_test_cases(question_dir: Path = DEFAULT_QUESTION_DIR) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for incident in INCIDENT_PORTS:
        path = question_dir / f"{incident}_qa_incident_o1-ga_c42.json"
        if not path.exists():
            raise FileNotFoundError(f"Missing test question file: {path}")
        questions = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(questions, list):
            raise TypeError(f"Question file must contain a list: {path}")
        cases.extend(
            BenchmarkCase(incident=incident, qid=qid, question=dict(question))
            for qid, question in enumerate(questions)
        )
    return cases


def sample_test_cases(
    cases: Sequence[BenchmarkCase], *, sample_size: int = 10, seed: int = 42
) -> list[BenchmarkCase]:
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    if sample_size > len(cases):
        raise ValueError(
            f"sample_size={sample_size} exceeds test population={len(cases)}"
        )
    return random.Random(seed).sample(list(cases), sample_size)


def normalize_answer(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(text.split())


def deterministic_evaluation(expected: Any, submitted: str) -> dict[str, Any]:
    expected_text = normalize_answer(expected)
    submitted_text = normalize_answer(submitted)
    exact = bool(expected_text) and expected_text == submitted_text
    contains = bool(expected_text) and (
        expected_text in submitted_text or submitted_text in expected_text
    )
    return {
        "exact_match": exact,
        "containment_match": contains,
        "ground_truth": expected,
        "submitted_answer": submitted,
    }


_JUDGE_PROMPT = """
You evaluate an ExCyTIn-Bench answer after the investigation has ended. Compare
the submitted answer with the ground-truth answer and each ordered solution
step. Do not help the investigation and do not invent matches. Semantic
equivalence and harmless formatting differences are acceptable. Large,
irrelevant enumerations are incorrect.

Return YAML only:
answer_correct: true | false
answer_analysis: "brief reason"
solution_steps:
  - step_index: 0
    correct: true | false
    analysis: "brief reason"
""".strip()


def llm_judge_evaluation(question: Mapping[str, Any], submitted: str) -> dict[str, Any]:
    solutions = question.get("solution") or []
    if not isinstance(solutions, list):
        solutions = [solutions]
    prompt = json.dumps(
        {
            "context": question.get("context", ""),
            "question": question.get("question", ""),
            "ground_truth_answer": question.get("answer", ""),
            "ordered_ground_truth_solution": solutions,
            "submitted_answer": submitted,
        },
        ensure_ascii=False,
        indent=2,
    )
    parsed = parse_yaml_response(call_llm(_JUDGE_PROMPT, prompt)) or {}
    answer_correct = _as_bool(parsed.get("answer_correct"))
    judged_steps = parsed.get("solution_steps") or []
    step_correct = [False] * len(solutions)
    for item in judged_steps if isinstance(judged_steps, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            index = int(item.get("step_index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(step_correct):
            step_correct[index] = _as_bool(item.get("correct"))
    reward = 1.0 if answer_correct else _discounted_partial_reward(step_correct)
    return {
        "llm_judge_answer_correct": answer_correct,
        "llm_judge_reward": reward,
        "llm_judge_answer_analysis": str(parsed.get("answer_analysis") or ""),
        "llm_judge_solution_steps": judged_steps,
        "reward_note": (
            "Locally reproduced ExCyTIn reward shape with the configured model; "
            "not an official-paper score unless the official evaluator model/config is used."
        ),
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def _discounted_partial_reward(step_correct: Sequence[bool]) -> float:
    reward = 0.0
    current = 0.4
    for correct in reversed(step_correct[:-1]):
        if correct:
            reward += current
        if reward >= 1.0:
            return 1.0
        current *= 0.4
    return reward


class ExcytinBenchmarkRunner:
    def __init__(
        self,
        *,
        max_steps: int = 25,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        llm_eval: bool = False,
        workflow_factory: WorkflowFactory | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self.max_steps = max_steps
        self.output_dir = output_dir
        self.llm_eval = llm_eval
        self.workflow_factory = workflow_factory or (
            lambda limit: ExcytinBenchWorkflow(max_steps=limit)
        )
        if executor_factory is None:
            password = os.environ.get("MYSQL_ROOT_PASSWORD", "").strip()
            if not password:
                raise ValueError(
                    "MYSQL_ROOT_PASSWORD is missing; source docker/excytin-mysql/.env"
                )
            executor_factory = lambda incident: MySQLQueryExecutor(
                incident, password=password
            )
        self.executor_factory = executor_factory

    def run(
        self,
        cases: Sequence[BenchmarkCase],
        *,
        seed: int,
        run_id: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        run_id = run_id or datetime.now(UTC).strftime(
            f"%Y%m%dT%H%M%SZ_seed{seed}_n{len(cases)}"
        )
        run_dir = self.output_dir / run_id
        case_dir = run_dir / "cases"
        case_dir.mkdir(parents=True, exist_ok=False)
        manifest = {
            "run_id": run_id,
            "created_at": _utc_now(),
            "split": "test",
            "seed": seed,
            "sample_size": len(cases),
            "max_steps": self.max_steps,
            "llm_eval": self.llm_eval,
            "selected_cases": [case.manifest_view() for case in cases],
        }
        _write_json(run_dir / "manifest.json", manifest)

        results: list[dict[str, Any]] = []
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case.case_id}: running", flush=True)
            result = self.run_case(case, case_dir=case_dir)
            results.append(result)
            _append_jsonl(run_dir / "results.jsonl", result)
            metrics = calculate_metrics(results, expected_total=len(cases))
            _write_json(run_dir / "summary.json", metrics)
            (run_dir / "summary.md").write_text(
                render_summary_markdown(metrics, results), encoding="utf-8"
            )
            print(
                f"[{index}/{len(cases)}] {case.case_id}: "
                f"{result['status']}, exact={result['evaluation']['exact_match']}, "
                f"steps={result['actions_issued']}",
                flush=True,
            )
        return run_dir, calculate_metrics(results, expected_total=len(cases))

    def run_case(self, case: BenchmarkCase, *, case_dir: Path) -> dict[str, Any]:
        started_at = _utc_now()
        started = time.perf_counter()
        workflow = self.workflow_factory(self.max_steps)
        process: list[dict[str, Any]] = []
        submitted_answer = ""
        error = ""
        status = "failed"
        executor: QueryExecutor | None = None
        case_path = case_dir / f"{case.case_id}.json"
        base_record: dict[str, Any] = {
            **case.manifest_view(),
            "split": "test",
            "started_at": started_at,
            "status": "running",
            "process": process,
        }
        _write_json(case_path, base_record)

        try:
            workflow.start(
                {
                    "context": case.question.get("context", ""),
                    "question": case.question.get("question", ""),
                },
                runtime_info={
                    "attack": case.incident,
                    "qid": case.qid,
                    "split": "test",
                },
            )
            executor = self.executor_factory(case.incident)
            for _ in range(self.max_steps):
                action_started = time.perf_counter()
                action = workflow.propose_next_action()
                action_record: dict[str, Any] = {
                    "timestamp": _utc_now(),
                    "step_no": action.step_no,
                    "node_id": action.node_id,
                    "action_type": action.action_type.value,
                    "action": action.content,
                }
                if action.action_type == BenchmarkActionType.SUBMIT:
                    submitted_answer = action.content
                    action_record["latency_seconds"] = round(
                        time.perf_counter() - action_started, 6
                    )
                    process.append(action_record)
                    status = "completed"
                    _write_json(
                        case_path,
                        {
                            **base_record,
                            "status": status,
                            "process": process,
                            "workflow": workflow.get_logging(),
                        },
                    )
                    break
                observation, query_success, row_count = executor.execute(action.content)
                action_record.update(
                    {
                        "observation": observation,
                        "query_success": query_success,
                        "row_count": row_count,
                        "latency_seconds": round(
                            time.perf_counter() - action_started, 6
                        ),
                    }
                )
                process.append(action_record)
                _write_json(
                    case_path,
                    {
                        **base_record,
                        "process": process,
                        "workflow": workflow.get_logging(),
                    },
                )
                workflow.accept_observation(
                    observation,
                    query_success=query_success,
                )
        except Exception as exc:  # noqa: BLE001 - record a failed case and continue.
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if executor is not None:
                executor.close()

        evaluation = deterministic_evaluation(
            case.question.get("answer", ""), submitted_answer
        )
        if self.llm_eval and submitted_answer:
            try:
                evaluation.update(llm_judge_evaluation(case.question, submitted_answer))
            except Exception as exc:  # noqa: BLE001 - judging must not lose a run.
                evaluation["llm_judge_error"] = f"{type(exc).__name__}: {exc}"
        workflow_log = workflow.get_logging()
        finished_at = _utc_now()
        result = {
            **case.manifest_view(),
            "split": "test",
            "status": status,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "latency_seconds": round(time.perf_counter() - started, 6),
            "actions_issued": int(workflow_log.get("actions_issued") or 0),
            "query_count": len(workflow_log.get("executions") or []),
            "submitted_answer": submitted_answer,
            "evaluation": evaluation,
            "process": process,
            "workflow": workflow_log,
        }
        _write_json(case_path, result)
        return result


def calculate_metrics(
    results: Sequence[Mapping[str, Any]], *, expected_total: int | None = None
) -> dict[str, Any]:
    total = expected_total if expected_total is not None else len(results)
    completed = [item for item in results if item.get("status") == "completed"]
    executions = [
        execution
        for result in results
        for execution in (result.get("workflow", {}).get("executions", []) or [])
    ]
    query_count = len(executions)
    query_successes = sum(bool(item.get("query_success")) for item in executions)
    sql_errors = [item for item in executions if not item.get("query_success")]
    schema_errors = [
        item
        for item in sql_errors
        if _SCHEMA_ERROR_RE.search(str(item.get("observation") or ""))
    ]
    repaired = 0
    repair_opportunities = 0
    for result in results:
        episode_executions = result.get("workflow", {}).get("executions", []) or []
        for index, execution in enumerate(episode_executions[:-1]):
            if execution.get("query_success"):
                continue
            repair_opportunities += 1
            if episode_executions[index + 1].get("query_success"):
                repaired += 1

    retrievals = [
        retrieval
        for result in results
        for retrieval in (result.get("workflow", {}).get("memory_retrievals", []) or [])
    ]
    executor_retrievals = [
        item for item in retrievals if item.get("role") == "executor"
    ]
    planner_retrievals = [item for item in retrievals if item.get("role") == "planner"]
    judged_rewards = [
        float(item["evaluation"]["llm_judge_reward"])
        for item in results
        if "llm_judge_reward" in item.get("evaluation", {})
    ]
    incident_counts = Counter(str(item.get("incident")) for item in results)
    return {
        "updated_at": _utc_now(),
        "expected_sample_size": total,
        "processed_count": len(results),
        "completed_count": len(completed),
        "failed_count": len(results) - len(completed),
        "submission_rate": _ratio(len(completed), total),
        "exact_match_count": sum(
            bool(item.get("evaluation", {}).get("exact_match")) for item in results
        ),
        "exact_match_rate": _ratio(
            sum(
                bool(item.get("evaluation", {}).get("exact_match")) for item in results
            ),
            total,
        ),
        "containment_match_rate": _ratio(
            sum(
                bool(item.get("evaluation", {}).get("containment_match"))
                for item in results
            ),
            total,
        ),
        "mean_llm_judge_reward": (
            round(statistics.fmean(judged_rewards), 6) if judged_rewards else None
        ),
        "average_actions": _mean(
            int(item.get("actions_issued") or 0) for item in results
        ),
        "average_queries": _mean(int(item.get("query_count") or 0) for item in results),
        "sql_query_count": query_count,
        "sql_success_count": query_successes,
        "sql_success_rate": _ratio(query_successes, query_count),
        "sql_error_count": len(sql_errors),
        "schema_error_count": len(schema_errors),
        "schema_error_rate": _ratio(len(schema_errors), query_count),
        "empty_result_rate": _ratio(
            sum(item.get("result_status") == "empty_result" for item in executions),
            query_count,
        ),
        "repair_opportunities": repair_opportunities,
        "immediate_repair_count": repaired,
        "immediate_repair_rate": _ratio(repaired, repair_opportunities),
        "planner_pm_hit_rate": _ratio(
            sum(bool(item.get("procedure_ids")) for item in planner_retrievals),
            len(planner_retrievals),
        ),
        "executor_em_hit_rate": _ratio(
            sum(bool(item.get("episode_ids")) for item in executor_retrievals),
            len(executor_retrievals),
        ),
        "average_case_latency_seconds": _mean(
            float(item.get("latency_seconds") or 0) for item in results
        ),
        "incident_processed_counts": dict(sorted(incident_counts.items())),
    }


def render_summary_markdown(
    metrics: Mapping[str, Any], results: Sequence[Mapping[str, Any]]
) -> str:
    lines = [
        "# ExCyTIn-Bench Sample Run",
        "",
        f"- Processed: {metrics['processed_count']}/{metrics['expected_sample_size']}",
        f"- Completed: {metrics['completed_count']}",
        f"- Exact match rate: {metrics['exact_match_rate']}",
        f"- Containment match rate: {metrics['containment_match_rate']}",
        f"- Mean LLM judge reward: {metrics['mean_llm_judge_reward']}",
        f"- SQL success rate: {metrics['sql_success_rate']}",
        f"- Schema error rate: {metrics['schema_error_rate']}",
        f"- Immediate repair rate: {metrics['immediate_repair_rate']}",
        f"- Average actions: {metrics['average_actions']}",
        f"- Average latency (s): {metrics['average_case_latency_seconds']}",
        "",
        "| Case | Status | Exact | Reward | Actions | Queries |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        evaluation = result.get("evaluation", {})
        lines.append(
            "| {case_id} | {status} | {exact} | {reward} | {actions} | {queries} |".format(
                case_id=result.get("case_id"),
                status=result.get("status"),
                exact=evaluation.get("exact_match"),
                reward=evaluation.get("llm_judge_reward", "n/a"),
                actions=result.get("actions_issued"),
                queries=result.get("query_count"),
            )
        )
    lines.append("")
    lines.append(
        "> LLM judge reward is a local reproduction and is not an official-paper score "
        "unless the same official evaluator configuration is used."
    )
    return "\n".join(lines) + "\n"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _mean(values: Iterable[float | int]) -> float | None:
    items = list(values)
    return round(statistics.fmean(items), 6) if items else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Random-sample ExCyTIn test questions and run SOCTrace LTM workflow"
    )
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--question-dir", type=Path, default=DEFAULT_QUESTION_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--llm-eval",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use the configured LLM for fuzzy answer and partial-step judging",
    )
    parser.add_argument(
        "--incident",
        action="append",
        choices=list(INCIDENT_PORTS),
        help="Restrict sampling to one or more incidents",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = load_test_cases(args.question_dir)
    if args.incident:
        allowed = set(args.incident)
        cases = [case for case in cases if case.incident in allowed]
    selected = sample_test_cases(cases, sample_size=args.sample_size, seed=args.seed)
    runner = ExcytinBenchmarkRunner(
        max_steps=args.max_steps,
        output_dir=args.output_dir,
        llm_eval=args.llm_eval,
    )
    run_dir, metrics = runner.run(selected, seed=args.seed)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"Run artifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INCIDENT_PORTS",
    "BenchmarkCase",
    "ExcytinBenchmarkRunner",
    "MySQLQueryExecutor",
    "calculate_metrics",
    "deterministic_evaluation",
    "load_test_cases",
    "main",
    "sample_test_cases",
]
