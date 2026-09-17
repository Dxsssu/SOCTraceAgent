from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .event import utc_now


class TTTNodeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    BLOCKED = "blocked"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    NOT_APPLICABLE = "n/a"


@dataclass(slots=True, frozen=True)
class TTTNode:
    """TTT 单个节点。"""

    node_id: str
    title: str
    status: TTTNodeStatus = TTTNodeStatus.TODO
    children: tuple[TTTNode, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    level: int = 0
    result_summary: str = ""
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "level": self.level,
            "result_summary": self.result_summary,
            "evidence_refs": list(self.evidence_refs),
            "node_id": self.node_id,
            "title": self.title,
            "status": self.status.value,
            "children": [child.to_dict() for child in self.children],
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], depth: int = 1) -> TTTNode:
        return cls(
            node_id=str(data["node_id"]),
            title=str(data.get("title") or ""),
            status=TTTNodeStatus(str(data.get("status") or TTTNodeStatus.TODO.value)),
            children=tuple(TTTNode.from_dict(child, depth + 1) for child in (data.get("children") or [])),
            metadata=dict(data.get("metadata") or {}),
            level=int(data.get("level") or depth),
            result_summary=str(data.get("result_summary") or ""),
            evidence_refs=tuple(data.get("evidence_refs") or ()),
        )


@dataclass(slots=True, frozen=True)
class TracebackTaskTree:
    """按版本保存的 TTT 快照。"""

    event_id: str
    round_id: int
    root_nodes: tuple[TTTNode, ...]
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 1
    next_task_id: str | None = None
    selection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "next_task_id": self.next_task_id,
            "selection_reason": self.selection_reason,
            "event_id": self.event_id,
            "round_id": self.round_id,
            "root_nodes": [node.to_dict() for node in self.root_nodes],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TracebackTaskTree:
        return cls(
            version=int(data.get("version") or 1),
            next_task_id=data.get("next_task_id"),
            selection_reason=str(data.get("selection_reason") or ""),
            event_id=str(data["event_id"]),
            round_id=int(data.get("round_id") or 1),
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
    "TTTNodeStatus",
    "TracebackTaskTree",
]
