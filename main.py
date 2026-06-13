from __future__ import annotations

import argparse
import os
import threading

from dotenv import load_dotenv

from src.agent.executor import ExecutorRuntime
from src.agent.planner import PlannerRuntime
from src.agent.reviewer import ReviewerRuntime
from src.agent import run_executor, run_planner, run_reviewer
from src.memory.working_memory import TTTStore
from src.messaging import SQLiteMessageBus
from src.storage import SQLiteStorage
from src.webapp import run_web_server


load_dotenv()


def print_banner() -> None:
    print(
        r"""
  ____   ___   ____    _                    _
 / ___| / _ \ / ___|  / \   __ _  ___ _ __ | |_
 \___ \| | | | |     / _ \ / _` |/ _ \ '_ \| __|
  ___) | |_| | |___ / ___ \ (_| |  __/ | | | |_
 |____/ \___/ \____/_/   \_\__, |\___|_| |_|\__|
                           |___/
"""
    )


def initialize_local_state() -> None:
    SQLiteStorage()
    TTTStore()
    SQLiteMessageBus()


def start_role(role_name: str, poll_interval: float) -> None:
    normalized = role_name.strip().lower()
    if normalized in {"_planner", "planner"}:
        run_planner(poll_interval=poll_interval)
        return
    if normalized in {"_executor", "executor"}:
        run_executor(poll_interval=poll_interval)
        return
    if normalized in {"_reviewer", "reviewer"}:
        run_reviewer(poll_interval=poll_interval)
        return
    raise ValueError(f"Unknown role: {role_name}")


def start_all_services(poll_interval: float) -> None:
    initialize_local_state()
    runtimes = (
        ("planner", PlannerRuntime(poll_interval=poll_interval)),
        ("executor", ExecutorRuntime(poll_interval=poll_interval)),
        ("reviewer", ReviewerRuntime(poll_interval=poll_interval)),
    )
    for name, runtime in runtimes:
        thread = threading.Thread(
            target=runtime.run_forever,
            name=f"socagent-{name}",
            daemon=True,
        )
        thread.start()
    run_web_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="SOCAgent bootstrap")
    parser.add_argument("-role", type=str, help="Role: _planner, _executor, _reviewer")
    parser.add_argument("-init-db", action="store_true", help="Initialize the local SQLite database")
    parser.add_argument("-web", action="store_true", help="Start the web UI")
    args = parser.parse_args()

    print_banner()
    if args.init_db:
        initialize_local_state()
        print("SQLite state initialized.")
        return

    if args.role:
        initialize_local_state()
        poll_interval = float(os.environ.get("SOCAGENT_POLL_INTERVAL", "5"))
        start_role(args.role, poll_interval=poll_interval)
        return

    poll_interval = float(os.environ.get("SOCAGENT_POLL_INTERVAL", "5"))
    if args.web:
        run_web_server()
        return

    start_all_services(poll_interval=poll_interval)
    return


if __name__ == "__main__":
    main()
