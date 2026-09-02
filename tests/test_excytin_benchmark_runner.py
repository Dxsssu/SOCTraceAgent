from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.benchmarks.excytin_bench.runner import (
    BenchmarkCase,
    ExcytinBenchmarkRunner,
    build_parser,
    calculate_metrics,
    load_test_cases,
    sample_test_cases,
)
from src.benchmarks.excytin_bench.workflow import (
    BenchmarkAction,
    BenchmarkActionType,
)


class FakeWorkflow:
    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.actions = 0
        self.executions: list[dict[str, Any]] = []
        self.answer = "answer-1"

    def start(self, *_: object, **__: object) -> None:
        return None

    def propose_next_action(self) -> BenchmarkAction:
        self.actions += 1
        if self.actions == 1:
            return BenchmarkAction(
                action_type=BenchmarkActionType.QUERY,
                content="SELECT 1;",
                step_no=1,
                node_id="1-1-1",
            )
        return BenchmarkAction(
            action_type=BenchmarkActionType.SUBMIT,
            content=self.answer,
            step_no=2,
        )

    def accept_observation(self, observation: str, *, query_success: bool) -> None:
        self.executions.append(
            {
                "sql": "SELECT 1;",
                "observation": observation,
                "query_success": query_success,
                "result_status": "rows_returned",
            }
        )

    def get_logging(self) -> dict[str, Any]:
        return {
            "actions_issued": self.actions,
            "executions": self.executions,
            "memory_retrievals": [
                {"role": "planner", "procedure_ids": ["procedure:base"]},
                {"role": "executor", "episode_ids": ["episode:1"]},
            ],
        }


class FakeExecutor:
    def execute(self, sql: str) -> tuple[str, bool, int]:
        assert sql == "SELECT 1;"
        return "[(1,)]", True, 1

    def close(self) -> None:
        return None


def test_default_benchmark_action_budget_is_twenty_five() -> None:
    assert build_parser().parse_args([]).max_steps == 25


def test_random_sample_is_reproducible_and_test_only() -> None:
    cases = load_test_cases()
    assert len(cases) == 589
    first = sample_test_cases(cases, sample_size=10, seed=42)
    second = sample_test_cases(cases, sample_size=10, seed=42)
    assert [item.case_id for item in first] == [item.case_id for item in second]
    assert len({item.case_id for item in first}) == 10
    assert all(item.question.get("question") for item in first)


def test_runner_records_process_and_incremental_metrics() -> None:
    cases = [
        BenchmarkCase(
            incident="incident_38",
            qid=index,
            question={
                "context": "Alert context",
                "question": f"Question {index}",
                "answer": "answer-1",
                "solution": ["find evidence", "answer-1"],
            },
        )
        for index in range(2)
    ]
    with TemporaryDirectory() as directory:
        output_dir = Path(directory)
        runner = ExcytinBenchmarkRunner(
            max_steps=4,
            output_dir=output_dir,
            llm_eval=False,
            workflow_factory=FakeWorkflow,
            executor_factory=lambda _: FakeExecutor(),
        )
        run_dir, metrics = runner.run(cases, seed=7, run_id="test-run")

        assert metrics["processed_count"] == 2
        assert metrics["exact_match_rate"] == 1.0
        assert metrics["sql_success_rate"] == 1.0
        assert metrics["planner_pm_hit_rate"] == 1.0
        assert metrics["executor_em_hit_rate"] == 1.0
        assert (run_dir / "manifest.json").exists()
        assert (run_dir / "summary.json").exists()
        assert (run_dir / "summary.md").exists()
        assert len((run_dir / "results.jsonl").read_text().splitlines()) == 2
        case_log = json.loads((run_dir / "cases/incident_38-q0000.json").read_text())
        assert [item["action_type"] for item in case_log["process"]] == [
            "query",
            "submit",
        ]
        assert case_log["process"][0]["observation"] == "[(1,)]"


def test_metrics_count_schema_errors_and_immediate_repairs() -> None:
    results = [
        {
            "incident": "incident_5",
            "status": "completed",
            "actions_issued": 3,
            "query_count": 2,
            "latency_seconds": 1.0,
            "evaluation": {"exact_match": False, "containment_match": True},
            "workflow": {
                "executions": [
                    {
                        "query_success": False,
                        "observation": "ProgrammingError: Unknown column 'bad'",
                        "result_status": "error",
                    },
                    {
                        "query_success": True,
                        "observation": "[(1,)]",
                        "result_status": "rows_returned",
                    },
                ],
                "memory_retrievals": [],
            },
        }
    ]
    metrics = calculate_metrics(results)
    assert metrics["schema_error_count"] == 1
    assert metrics["immediate_repair_rate"] == 1.0
    assert metrics["containment_match_rate"] == 1.0
