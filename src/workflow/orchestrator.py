from __future__ import annotations

import threading
from typing import Any

from src.agent.executor import ExecutorRuntime
from src.agent.planner import PlannerRuntime
from src.agent.reviewer import ReviewerRuntime
from src.memory.working_memory import TTTStore
from src.messaging import RoleName, SQLiteMessageBus
from src.storage import SQLiteStorage

from .context import MemoryContextService


class SOCTraceWorkflow:
    """Compose role runtimes with shared state and role-scoped memory views."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        poll_interval: float = 5.0,
        memory_contexts: MemoryContextService | None = None,
    ) -> None:
        self.storage = SQLiteStorage(db_path)
        self.ttt_store = TTTStore(db_path)
        self.bus = SQLiteMessageBus(db_path)
        self.memory_contexts = memory_contexts or MemoryContextService()
        shared: dict[str, Any] = {
            "storage": self.storage,
            "ttt_store": self.ttt_store,
            "bus": self.bus,
            "poll_interval": poll_interval,
        }
        self.planner = PlannerRuntime(
            **shared,
            context_provider=self.memory_contexts.planner_view(),
        )
        self.executor = ExecutorRuntime(
            **shared,
            context_provider=self.memory_contexts.executor_view(),
        )
        self.reviewer = ReviewerRuntime(
            **shared,
            context_provider=self.memory_contexts.reviewer_view(),
        )
        self._runtimes = {
            RoleName.PLANNER: self.planner,
            RoleName.EXECUTOR: self.executor,
            RoleName.REVIEWER: self.reviewer,
        }

    def run_role_forever(self, role_name: str) -> None:
        role = self._normalize_role(role_name)
        self._runtimes[role].run_forever()

    def start_agent_threads(self) -> list[threading.Thread]:
        threads: list[threading.Thread] = []
        for role, runtime in self._runtimes.items():
            thread = threading.Thread(
                target=runtime.run_forever,
                name=f"socagent-{role.value.removeprefix('_')}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        return threads

    def stop(self) -> None:
        for runtime in self._runtimes.values():
            runtime.stop()

    @staticmethod
    def _normalize_role(role_name: str) -> RoleName:
        normalized = role_name.strip().lower().removeprefix("_")
        mapping = {
            "planner": RoleName.PLANNER,
            "executor": RoleName.EXECUTOR,
            "reviewer": RoleName.REVIEWER,
        }
        try:
            return mapping[normalized]
        except KeyError as exc:
            raise ValueError(f"未知角色: {role_name}") from exc


__all__ = ["SOCTraceWorkflow"]
