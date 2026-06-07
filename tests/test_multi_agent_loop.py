#!/usr/bin/env python3
"""SOCAgent 多智能体链路烟雾测试脚本。

用途：
1. 可选启动 Planner / Executor / Reviewer 三个独立进程
2. 直接写入一条测试事件，不经过前端
3. 轮询 SQLite 中的事件、TTT、Execution、RoundReview、Message 状态
4. 验证多轮链路是否形成闭环

说明：
- 这是一个终端脚本，不依赖 pytest 运行
- 风格参考 deepsoc/tools/test_multi_agent_loop.py
- 当前系统的“完成一轮”定义为：
  - 该轮至少生成 1 条 Execution
  - 该轮生成了 RoundReview
  - TTT 已经推进到下一轮或事件已完成
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")


PIPELINE_MESSAGE_TYPES = {
    "planner_analysis_completed",
    "ttt_initialized",
    "ttt_updated",
    "leaf_claimed",
    "execution_started",
    "execution_completed",
    "execution_failed",
    "round_review_started",
    "round_review_created",
    "handoff_to_reviewer",
    "handoff_to_planner",
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SOCAgent multi-agent loop smoke test (terminal only)"
    )
    parser.add_argument(
        "--start-agents",
        action="store_true",
        default=True,
        help="启动 Planner / Executor / Reviewer 三个进程（默认开启）。",
    )
    parser.add_argument(
        "--no-start-agents",
        action="store_false",
        dest="start_agents",
        help="不自动启动角色进程。",
    )
    parser.add_argument(
        "--warmup-seconds",
        type=int,
        default=5,
        help="启动角色后的预热时间（秒）。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="最大等待时长（秒）。",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=1,
        help="轮询间隔（秒）。",
    )
    parser.add_argument(
        "--event-id",
        type=str,
        default="",
        help="指定 event_id；默认自动生成。",
    )
    parser.add_argument(
        "--target-rounds",
        type=int,
        default=2,
        help="目标验证轮次数，默认 2。",
    )
    parser.add_argument(
        "--show-process-logs",
        action="store_true",
        help="显示子进程输出。",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="tests/runtime/test_multi_agent_loop.db",
        help="测试专用 SQLite 路径。",
    )
    parser.add_argument(
        "--poll-role-interval",
        type=str,
        default="1",
        help="传给角色进程的 SOCAGENT_POLL_INTERVAL。",
    )
    return parser


def prepare_test_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    initialize_test_state(db_path)


def initialize_test_state(db_path: Path) -> None:
    from src.memory.working_memory import TTTStore
    from src.messaging import SQLiteMessageBus
    from src.storage import SQLiteStorage

    SQLiteStorage(db_path)
    TTTStore(db_path)
    SQLiteMessageBus(db_path)


def build_process_env(db_path: Path, poll_interval: str) -> dict[str, str]:
    env = os.environ.copy()
    env["SOCAGENT_DB_PATH"] = str(db_path)
    env["SOCAGENT_POLL_INTERVAL"] = str(poll_interval)
    return env


def start_all_processes(
    *,
    env: dict[str, str],
    show_process_logs: bool = False,
) -> list[subprocess.Popen]:
    commands = [
        [sys.executable, "main.py", "-role", "planner"],
        [sys.executable, "main.py", "-role", "executor"],
        [sys.executable, "main.py", "-role", "reviewer"],
    ]
    processes: list[subprocess.Popen] = []
    for command in commands:
        if show_process_logs:
            processes.append(subprocess.Popen(command, cwd=ROOT_DIR, env=env))
        else:
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=ROOT_DIR,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
    return processes


def stop_all_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.time() + 10
    for process in processes:
        if process.poll() is None:
            timeout = max(0, deadline - time.time())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()


def create_test_event(event_id: str, db_path: Path) -> str:
    from src.schema import Event, SeverityLevel
    from src.storage import SQLiteStorage

    storage = SQLiteStorage(db_path)
    existing = storage.get_event(event_id)
    if existing is not None:
        return event_id

    event = Event(
        event_id=event_id,
        event_name="SOCAgent Multi-Agent Loop Test",
        message="SIEM 告警：外部 IP 11.22.33.44 对邮件网关 192.168.22.251 出现异常登录尝试，请自动分析并推进溯源。",
        context={
            "test_case": "tests/test_multi_agent_loop.py",
            "goal": "验证 Planner / Executor / Reviewer 多轮闭环",
        },
        source="terminal_smoke_test",
        severity=SeverityLevel.MEDIUM,
    )
    storage.save_event(event)
    return event_id


def fetch_snapshot(event_id: str, db_path: Path) -> dict[str, object]:
    from src.storage import SQLiteStorage

    storage = SQLiteStorage(db_path)
    event = storage.get_event(event_id)
    if event is None:
        return {"exists": False}

    executions = storage.list_executions(event_id)
    latest_review = storage.get_latest_round_review(event_id)

    snapshot = {
        "exists": True,
        "event_status": event.event_status.value,
        "current_round": event.current_round,
        "execution_count": len(executions),
        "latest_review_round": latest_review.round_id if latest_review else None,
        "execution_statuses": {},
        "execution_rounds": [],
        "review_rounds": [],
        "ttt_versions": 0,
        "message_count": 0,
    }

    execution_statuses: dict[str, int] = {}
    execution_rounds: set[int] = set()
    for execution in executions:
        status = execution.execution_status.value
        execution_statuses[status] = execution_statuses.get(status, 0) + 1
        execution_rounds.add(execution.round_id)
    snapshot["execution_statuses"] = execution_statuses
    snapshot["execution_rounds"] = sorted(execution_rounds)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        review_rows = _safe_fetchall(
            conn,
            "SELECT round_id FROM round_reviews WHERE event_id = ? ORDER BY round_id ASC",
            (event_id,),
        )
        ttt_rows = _safe_fetchall(
            conn,
            "SELECT ttt_version, round_id FROM ttt_snapshots WHERE event_id = ? ORDER BY ttt_version ASC",
            (event_id,),
        )
        message_count_row = _safe_fetchone(
            conn,
            "SELECT COUNT(*) AS cnt FROM messages WHERE event_id = ?",
            (event_id,),
        )

    snapshot["review_rounds"] = sorted(int(row["round_id"]) for row in review_rows)
    snapshot["ttt_versions"] = len(ttt_rows)
    snapshot["ttt_rounds"] = [int(row["round_id"]) for row in ttt_rows]
    snapshot["message_count"] = int(message_count_row["cnt"]) if message_count_row else 0
    return snapshot


def _safe_fetchall(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _safe_fetchone(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None


def print_snapshot(elapsed: int, snapshot: dict[str, object]) -> None:
    if not snapshot.get("exists"):
        print(f"\n===== SNAPSHOT @{elapsed:>4}s =====")
        print("event missing")
        return

    print(f"\n===== SNAPSHOT @{elapsed:>4}s =====")
    print(
        f"status={snapshot['event_status']}, "
        f"round={snapshot['current_round']}, "
        f"executions={snapshot['execution_count']}, "
        f"messages={snapshot['message_count']}"
    )
    print(f"execution_statuses={snapshot['execution_statuses']}")
    print(f"execution_rounds={snapshot['execution_rounds']}")
    print(f"review_rounds={snapshot['review_rounds']}")
    print(f"ttt_versions={snapshot['ttt_versions']}")
    print(f"ttt_rounds={snapshot.get('ttt_rounds', [])}")


def fetch_new_pipeline_messages(
    event_id: str,
    db_path: Path,
    last_seen_rowid: int,
) -> tuple[list[dict[str, object]], int]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT rowid, message_id, event_id, round_id, from_role, to_role, message_type, payload_json, created_at
                FROM messages
                WHERE event_id = ? AND rowid > ?
                ORDER BY rowid ASC
                """,
                (event_id, last_seen_rowid),
            ).fetchall()
    except sqlite3.OperationalError:
        return [], last_seen_rowid

    messages: list[dict[str, object]] = []
    new_last_seen = last_seen_rowid
    for row in rows:
        new_last_seen = max(new_last_seen, int(row["rowid"]))
        message_type = str(row["message_type"] or "")
        if message_type not in PIPELINE_MESSAGE_TYPES:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        messages.append(
            {
                "rowid": int(row["rowid"]),
                "message_id": row["message_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "from_role": row["from_role"],
                "to_role": row["to_role"],
                "message_type": message_type,
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return messages, new_last_seen


def print_pipeline_messages(messages: list[dict[str, object]]) -> None:
    if not messages:
        return
    print("\n===== NEW PIPELINE MESSAGES =====")
    for index, message in enumerate(messages, start=1):
        envelope = {
            "message_type": message["message_type"],
            "from_role": message["from_role"],
            "to_role": message["to_role"],
            "round_id": message["round_id"],
            "payload": message["payload"],
        }
        print(f"----- MESSAGE {index} -----")
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    print("===== END OF NEW MESSAGES =====")


def get_fully_completed_rounds(snapshot: dict[str, object]) -> set[int]:
    execution_rounds = set(snapshot.get("execution_rounds", []))
    review_rounds = set(snapshot.get("review_rounds", []))
    return execution_rounds & review_rounds


def is_multi_round_done(snapshot: dict[str, object], target_rounds: int) -> bool:
    rounds = get_fully_completed_rounds(snapshot)
    return all(round_id in rounds for round_id in range(1, target_rounds + 1))


def has_message_type(event_id: str, db_path: Path, message_type: str) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE event_id = ? AND message_type = ?
                LIMIT 1
                """,
                (event_id, message_type),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def main() -> int:
    args = build_arg_parser().parse_args()
    db_path = (ROOT_DIR / args.db_path).resolve()
    prepare_test_db(db_path)

    env = build_process_env(db_path, args.poll_role_interval)
    processes: list[subprocess.Popen] = []
    shutting_down = False

    def handle_signal(*_: object) -> None:
        nonlocal shutting_down
        shutting_down = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if args.start_agents:
            print(">>> Starting Planner / Executor / Reviewer ...")
            processes = start_all_processes(env=env, show_process_logs=args.show_process_logs)
            print(f">>> Waiting {args.warmup_seconds}s for warm-up ...")
            time.sleep(args.warmup_seconds)

        event_id = args.event_id.strip() or f"loop_test_{uuid.uuid4().hex[:8]}"
        create_test_event(event_id, db_path)
        print(f">>> Test event ready: {event_id}")
        print(f">>> Test db: {db_path}")

        started_at = time.time()
        last_seen_rowid = 0
        snapshot: dict[str, object] = {"exists": False}

        while not shutting_down:
            elapsed = int(time.time() - started_at)
            if elapsed > args.timeout:
                break

            snapshot = fetch_snapshot(event_id, db_path)
            print_snapshot(elapsed, snapshot)
            messages, last_seen_rowid = fetch_new_pipeline_messages(event_id, db_path, last_seen_rowid)
            print_pipeline_messages(messages)

            if is_multi_round_done(snapshot, args.target_rounds):
                break

            time.sleep(args.poll_interval)

        print("\n=== Result ===")
        if is_multi_round_done(snapshot, args.target_rounds):
            if not has_message_type(event_id, db_path, "planner_analysis_completed"):
                print("FAIL: planner analysis message not found.")
                print(f"event_id={event_id}")
                return 1
            print(f"PASS: multi-round loop completed (target_rounds={args.target_rounds}).")
            print(
                "INFO: "
                f"execution_rounds={snapshot.get('execution_rounds', [])}, "
                f"review_rounds={snapshot.get('review_rounds', [])}, "
                f"ttt_rounds={snapshot.get('ttt_rounds', [])}"
            )
            print(f"event_id={event_id}")
            return 0

        print(f"FAIL: timeout before reaching target_rounds={args.target_rounds}.")
        print(
            "INFO: "
            f"execution_rounds={snapshot.get('execution_rounds', [])}, "
            f"review_rounds={snapshot.get('review_rounds', [])}, "
            f"ttt_rounds={snapshot.get('ttt_rounds', [])}"
        )
        print(f"event_id={event_id}")
        return 1

    finally:
        if processes:
            print(">>> Shutting down spawned processes ...")
            stop_all_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
