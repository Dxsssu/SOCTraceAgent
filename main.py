from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

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
    raise ValueError(f"未知角色: {role_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SOCAgent bootstrap")
    parser.add_argument("-role", type=str, help="角色: _planner, _executor, _reviewer")
    parser.add_argument("-init-db", action="store_true", help="初始化本地 SQLite 数据库")
    parser.add_argument("-web", action="store_true", help="启动 Web 界面")
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

    if args.web or not args.role:
        run_web_server()
        return


if __name__ == "__main__":
    main()
