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


def coerce_ttt_node_status(value: Any) -> TTTNodeStatus:
    text = str(value or TTTNodeStatus.TODO.value).strip().lower()
    aliases = {
        "todo": TTTNodeStatus.TODO,
        "pending": TTTNodeStatus.TODO,
        "in_progress": TTTNodeStatus.IN_PROGRESS,
        "in-progress": TTTNodeStatus.IN_PROGRESS,
        "doing": TTTNodeStatus.IN_PROGRESS,
        "done": TTTNodeStatus.DONE,
        "completed": TTTNodeStatus.DONE,
        "success": TTTNodeStatus.DONE,
        "n/a": TTTNodeStatus.NOT_APPLICABLE,
        "na": TTTNodeStatus.NOT_APPLICABLE,
        "not_applicable": TTTNodeStatus.NOT_APPLICABLE,
        "not-applicable": TTTNodeStatus.NOT_APPLICABLE,
        "failed": TTTNodeStatus.NOT_APPLICABLE,
        "failure": TTTNodeStatus.NOT_APPLICABLE,
    }
    return aliases.get(text, TTTNodeStatus.TODO)


@dataclass(slots=True, frozen=True)
class TTTNode:
    """Single node in the Traceback Task Tree."""

    node_id: str
    title: str
    status: TTTNodeStatus = TTTNodeStatus.TODO
    children: tuple["TTTNode", ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "status": self.status.value,
            "children": [child.to_dict() for child in self.children],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TTTNode":
        return cls(
            node_id=str(data["node_id"]),
            title=str(data.get("title") or ""),
            status=coerce_ttt_node_status(data.get("status")),
            children=tuple(TTTNode.from_dict(child) for child in (data.get("children") or [])),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True, frozen=True)
class TracebackTaskTree:
    """Versioned snapshot of the Traceback Task Tree."""

    event_id: str
    round_id: int
    root_nodes: tuple[TTTNode, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "round_id": self.round_id,
            "root_nodes": [node.to_dict() for node in self.root_nodes],
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TracebackTaskTree":
        return cls(
            event_id=str(data["event_id"]),
            round_id=int(data.get("round_id") or 1),
            root_nodes=tuple(TTTNode.from_dict(node) for node in (data.get("root_nodes") or [])),
            metadata=dict(data.get("metadata") or {}),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utc_now()


__all__ = [
    "TTTNode",
    "TTTNodeStatus",
    "TracebackTaskTree",
    "coerce_ttt_node_status",
]
