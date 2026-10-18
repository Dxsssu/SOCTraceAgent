"""Role context contracts without long-term memory dependencies."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from src.schema import Event, RoundReview, TracebackTaskTree


class PlannerContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        review: RoundReview | None = None,
        ttt: TracebackTaskTree | None = None,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]: ...


class ExecutorContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = False,
        episodic_query: str | None = None,
        episodic_background: str | None = None,
    ) -> dict[str, Any]: ...


class ReviewerContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]: ...


class NullPlannerContextProvider:
    def build(
        self,
        event: Event,
        *,
        review: RoundReview | None = None,
        ttt: TracebackTaskTree | None = None,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


class NullExecutorContextProvider:
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = False,
        episodic_query: str | None = None,
        episodic_background: str | None = None,
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


class NullReviewerContextProvider:
    def build(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


