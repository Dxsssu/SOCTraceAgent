"""Real Fancybag + MySQL integration evaluation; preserve every failed attempt."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from src.agent.llm import LLMClient, LLMConfig
from src.benchmarks.excytin_bench.runner import ExcytinBenchmarkRunner, load_test_cases
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow


def save(path, value):
    """Atomic checkpoints: interruption cannot leave a half-written JSON."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_case(case, case_dir, config, max_steps, stop_event):
    case_id = case.case_id
    calls = []
    client = LLMClient(config)
    create = client.client.chat.completions.create

    def tracked_create(**kwargs):
        response = create(**kwargs)
        if response.usage:
            calls[-1]["usage"] = response.usage.model_dump()
        return response

    client.client.chat.completions.create = tracked_create

    def invoke(system, user):
        role = next(
            (
                r
                for r, marker in [
                    ("planner", "系统中的 Planner"),
                    ("executor", "你是 Executor"),
                    ("reviewer", "你是 Reviewer"),
                    ("verifier", "你是 Verifier"),
                ]
                if marker in system
            ),
            "unknown",
        )
        call = {
            "index": len(calls) + 1,
            "role": role,
            "system": system,
            "input": user,
        }
        calls.append(call)
        save(case_dir / "calls.json", calls)
        if stop_event.is_set():
            raise InterruptedError("Batch interrupted before next model call")
        start = time.monotonic()
        try:
            call["output"] = client.chat(system_prompt=system, user_prompt=user)
            return call["output"]
        except Exception as exc:
            call["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            call["seconds"] = round(time.monotonic() - start, 3)
            save(case_dir / "calls.json", calls)


    try:
        runner = ExcytinBenchmarkRunner(
            max_steps=max_steps,
            workers=1,
            llm_eval=False,
            workflow_factory=lambda limit: ExcytinBenchWorkflow(
                max_steps=limit, llm=invoke,
                semantic_memory_enabled=True, procedural_memory_enabled=True
            ),
        )
        result = runner.run_case(case, case_dir=case_dir)
    finally:
        client.client.close()
    log = result.get("workflow", {})
    return {
        **{
            k: result[k]
            for k in (
                "case_id",
                "question",
                "status",
                "error",
                "latency_seconds",
                "query_count",
                "submitted_answer",
                "evaluation",
            )
        },
        "reference_answer": case.question.get("answer"),
        "llm_calls": len(calls),
        "usage": {
            key: sum(c.get("usage", {}).get(key, 0) or 0 for c in calls)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "retrieval_count": len(log.get("memory_retrievals", [])),
        "selections": log.get("task_semantic_contexts", {}),
        "procedural_retrievals": log.get("procedural_retrievals", []),
        "procedural_selections": log.get("task_procedural_contexts", {}),
        "procedural_fallbacks": log.get("procedural_fallbacks", []),
        "executions": log.get("executions", []),
        "retry_events": [
            e
            for e in log.get("trace", [])
            if "invalid" in e["event_type"] or "retry" in e["event_type"]
        ],

    }


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def completed(record, case_id):
    return (isinstance(record, dict) and record.get("case_id") == case_id
            and record.get("status") == "completed" and not record.get("error")
            and bool(str(record.get("submitted_answer") or "").strip())
            and isinstance(record.get("evaluation"), dict))


def previous_result(output, case):
    record = read_json(output / case.case_id / "summary.json")
    if completed(record, case.case_id):
        return record
    # Compatibility with older timestamped runs, which saved only a root summary.
    legacy = read_json(output / case.case_id / f"{case.case_id}.json")
    if isinstance(legacy, dict) and legacy.get("finished_at") and completed(legacy, case.case_id):
        return {k: v for k, v in legacy.items() if k not in {"workflow", "process"}}
    return None


def attempt_case(case, output, config, max_steps, stop_event, worker=run_case):
    case_dir = output / case.case_id
    attempt_id = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex[:6]
    attempt_dir = case_dir / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True)
    save(case_dir / "summary.json", {"case_id": case.case_id, "status": "running", "attempt_id": attempt_id})
    try:
        result = worker(case, attempt_dir, config, max_steps, stop_event)
    except Exception as exc:
        result = {"case_id": case.case_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                  "submitted_answer": "", "evaluation": {}}
        (attempt_dir / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
    result.update(attempt_id=attempt_id, attempt_dir=str(attempt_dir))
    save(attempt_dir / "summary.json", result)
    save(case_dir / "summary.json", result)
    return result


class Progress:
    """Dependency-free progress bar; redraw in terminals, append checkpoints in logs."""
    def __init__(self, total, skipped):
        self.total, self.skipped = total, skipped
        self.ok = self.failed = 0
        self.started = time.monotonic()
        self.last = 0

    def show(self, active, *, force=False):
        now = time.monotonic()
        if not force and now - self.last < (1 if sys.stderr.isatty() else 30):
            return
        self.last = now
        done = self.skipped + self.ok + self.failed
        width = 25
        filled = int(width * done / max(self.total, 1))
        bar = "#" * filled + "-" * (width - filled)
        message = (f"[{bar}] {done}/{self.total} | 跳过 {self.skipped} | 本次完成 {self.ok} "
                   f"| 失败 {self.failed} | 运行中 {active} | 耗时 {int(now-self.started)}s")
        print(("\r" if sys.stderr.isatty() else "") + message,
              end="" if sys.stderr.isatty() else "\n", file=sys.stderr, flush=True)


def run_batch(cases, output, config, *, workers=10, max_steps=25, worker=run_case):
    output.mkdir(parents=True, exist_ok=True)
    records, pending = {}, []
    for case in cases:
        previous = previous_result(output, case)
        if previous is not None:
            records[case.case_id] = previous
        else:
            pending.append(case)
    progress = Progress(len(cases), len(records))
    stop = threading.Event()
    interrupted = False
    queue = iter(pending)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fancybag")
    active = {}

    def submit_one():
        case = next(queue, None)
        if case is not None:
            future = pool.submit(attempt_case, case, output, config, max_steps, stop, worker)
            active[future] = case

    def checkpoint():
        save(output / "summary.json", [records[c.case_id] for c in cases if c.case_id in records])
        save(output / "progress.json", {"total": len(cases), "skipped": progress.skipped,
            "completed": progress.ok, "failed": progress.failed, "active": len(active),
            "pending": len(cases) - len(records) - len(active), "interrupted": interrupted})

    try:
        for _ in range(min(workers, len(pending))):
            submit_one()
        checkpoint()
        progress.show(len(active), force=True)
        while active:
            try:
                done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                interrupted = True
                stop.set()
                print("\n已停止派发新题，等待正在进行的请求结束并保存记录；再次运行同一命令可续跑。", file=sys.stderr)
                continue
            for future in done:
                case = active.pop(future)
                try:
                    record = future.result()
                except Exception as exc:
                    record = {"case_id": case.case_id, "status": "failed", "error": str(exc)}
                records[case.case_id] = record
                if completed(record, case.case_id):
                    progress.ok += 1
                else:
                    progress.failed += 1
                if not stop.is_set():
                    submit_one()
                checkpoint()
            progress.show(len(active), force=bool(done))
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        checkpoint()
        progress.show(0, force=True)
        if sys.stderr.isatty():
            print(file=sys.stderr)
    return 130 if interrupted else (1 if progress.failed else 0)


def source_hashes():
    paths = [Path(__file__), ROOT / "src/benchmarks/excytin_bench/workflow.py",
             ROOT / "src/benchmarks/excytin_bench/runner.py", *sorted((ROOT / "src/agent").glob("*.py"))]
    for directory in [ROOT / "src/memory/longterm_memory/semantic_memory/v2",
                      ROOT / "src/memory/longterm_memory/procedural_memory/trajectory_snippets"]:
        paths.extend(sorted(directory.glob("*.py")))
        paths.extend(sorted((directory / "excytin").glob("*.json")))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser = argparse.ArgumentParser(description="Fancybag + PM/SM 全量测试，默认跳过已完成题目")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="运行全部测试题（默认）")
    selection.add_argument("--cases", nargs="+", help="只运行指定题目")
    parser.add_argument("--workers", type=int, default=10, help="并发题目数，默认 10")
    parser.add_argument("--max-steps", type=int, default=25,
                        help="每题总动作预算，默认与 baseline 一致为 25（SQL 和提交各计一步）")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/procedural_workflow_fancybag/batch",
                        help="固定输出目录；再次使用同一目录自动续跑")
    args = parser.parse_args()
    if args.workers < 1 or args.max_steps < 2:
        parser.error("workers 必须大于 0，max-steps 至少为 2（预留查询和提交）")
    available = {c.case_id: c for c in load_test_cases()}
    unknown = set(args.cases or []) - set(available)
    if unknown:
        parser.error(f"未知题目：{sorted(unknown)}")
    cases = [available[k] for k in dict.fromkeys(args.cases)] if args.cases else list(available.values())
    load_dotenv(ROOT / "docker/excytin-mysql/.env")
    os.environ["SOCAGENT_LLM_PROVIDER"] = "fancybag"
    config = replace(LLMConfig.from_env(), max_retries=0)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Prevent two processes from racing the same checkpoints (macOS/Linux).
    import fcntl
    with (output / ".batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"已有进程正在使用输出目录：{output}")
        manifest = {"provider": "fancybag", "model": config.model, "base_url": config.base_url,
                    "max_steps": args.max_steps, "workers": args.workers,
                    "cases": [c.case_id for c in cases], "llm_eval": False,
                    "semantic_memory_enabled": True, "procedural_memory_enabled": True,
                    "sources_sha256": source_hashes()}
        previous = read_json(output / "manifest.json")
        if previous and any(previous.get(k) != manifest[k] for k in ("model", "base_url", "max_steps", "sources_sha256")):
            print("注意：配置或代码与上次不同，仍按完成状态跳过；如需独立评测，请使用新的 --output 目录。", file=sys.stderr)
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
        (output / "manifests").mkdir(exist_ok=True)
        save(output / "manifests" / f"{stamp}.json", manifest)
        save(output / "manifest.json", manifest)
        print(f"OUTPUT {output}\n测试题数 {len(cases)}；并发 {args.workers}；模型 {config.model}", flush=True)
        code = run_batch(cases, output, config, workers=args.workers, max_steps=args.max_steps)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
