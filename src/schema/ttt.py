from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .event import utc_now


class TTTNodeStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    NOT_APPLICABLE = "n/a"


class TTTNodeLevel(StrEnum):
    PHASE = "L1_phase"
    SUB_GOAL = "L2_sub_goal"
    ATOMIC_INTENT = "L3_atomic_intent"


@dataclass(slots=True, frozen=True)
class TTTNode:
    """TTT 单个节点。"""

    node_id: str
    title: str
    node_level: TTTNodeLevel
    status: TTTNodeStatus = TTTNodeStatus.TODO
    task_type: str | None = None
    assignee: str | None = None
    children: tuple["TTTNode", ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "node_level": self.node_level.value,
            "status": self.status.value,
            "task_type": self.task_type,
            "assignee": self.assignee,
            "children": [child.to_dict() for child in self.children],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TTTNode":
        return cls(
            node_id=str(data["node_id"]),
            title=str(data.get("title") or ""),
            node_level=TTTNodeLevel(str(data.get("node_level") or TTTNodeLevel.ATOMIC_INTENT.value)),
            status=TTTNodeStatus(str(data.get("status") or TTTNodeStatus.TODO.value)),
            task_type=str(data["task_type"]) if data.get("task_type") is not None else None,
            assignee=str(data["assignee"]) if data.get("assignee") is not None else None,
            children=tuple(TTTNode.from_dict(child) for child in (data.get("children") or [])),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True, frozen=True)
class TracebackTaskTree:
    """按版本保存的 TTT 快照。"""

    event_id: str
    round_id: int
    root_nodes: tuple[TTTNode, ...]
    ttt_version: int = 1
    schema_version: str = "1.0"
    updated_by: str = "_planner"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "round_id": self.round_id,
            "ttt_version": self.ttt_version,
            "schema_version": self.schema_version,
            "updated_by": self.updated_by,
            "root_nodes": [node.to_dict() for node in self.root_nodes],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TracebackTaskTree":
        return cls(
            event_id=str(data["event_id"]),
            round_id=int(data.get("round_id") or 1),
            ttt_version=int(data.get("ttt_version") or 1),
            schema_version=str(data.get("schema_version") or "1.0"),
            updated_by=str(data.get("updated_by") or "_planner"),
            root_nodes=tuple(TTTNode.from_dict(node) for node in (data.get("root_nodes") or [])),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utc_now()


__all__ = [
    "TTTNode",
    "TTTNodeLevel",
    "TTTNodeStatus",
    "TracebackTaskTree",
]
