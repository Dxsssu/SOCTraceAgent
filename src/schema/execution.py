from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .event import utc_now


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class Execution:
    """Execution record for a single TTT leaf node handled by the Executor."""

    event_id: str
    round_id: int
    node_id: str
    node_title: str
    execution_id: str = field(default_factory=lambda: str(uuid4()))
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    execution_status: ExecutionStatus = ExecutionStatus.PENDING
    error_message: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "event_id": self.event_id,
            "round_id": self.round_id,
            "node_id": self.node_id,
            "node_title": self.node_title,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "result": self.result,
            "execution_status": self.execution_status.value,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Execution":
        return cls(
            execution_id=str(data.get("execution_id") or uuid4()),
            event_id=str(data["event_id"]),
            round_id=int(data.get("round_id") or 1),
            node_id=str(data["node_id"]),
            node_title=str(data.get("node_title") or ""),
            tool_name=str(data.get("tool_name") or ""),
            tool_input=dict(data.get("tool_input") or {}),
            result=dict(data.get("result") or {}),
            execution_status=ExecutionStatus(
                str(data.get("execution_status") or ExecutionStatus.PENDING.value)
            ),
            error_message=str(data.get("error_message") or ""),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utc_now()


__all__ = ["Execution", "ExecutionStatus"]
