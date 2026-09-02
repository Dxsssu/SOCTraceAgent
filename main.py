from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from src.memory.working_memory import TTTStore
from src.messaging import SQLiteMessageBus
from src.storage import SQLiteStorage
from src.webapp import run_web_server
from src.workflow.orchestrator import SOCTraceWorkflow

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
    SOCTraceWorkflow(poll_interval=poll_interval).run_role_forever(role_name)


def start_all_services(poll_interval: float) -> None:
    workflow = SOCTraceWorkflow(poll_interval=poll_interval)
    workflow.start_agent_threads()
    run_web_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="SOCAgent bootstrap")
    parser.add_argument("-role", type=str, help="角色: _planner, _executor, _reviewer")
    parser.add_argument(
        "-init-db", action="store_true", help="初始化本地 SQLite 数据库"
    )
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

    poll_interval = float(os.environ.get("SOCAGENT_POLL_INTERVAL", "5"))
    if args.web:
        run_web_server()
        return

    start_all_services(poll_interval=poll_interval)
    return


if __name__ == "__main__":
    main()
