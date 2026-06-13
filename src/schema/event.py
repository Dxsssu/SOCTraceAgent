from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventStatus(StrEnum):
    """Event states for the three-agent workflow."""

    PENDING = "pending"
    PLANNED = "planned"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    FAILED = "failed"


class SeverityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class Event:
    """Primary security event object."""

    event_name: str
    message: str
    event_id: str = field(default_factory=lambda: str(uuid4()))
    context: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    severity: SeverityLevel = SeverityLevel.UNKNOWN
    event_status: EventStatus = EventStatus.PENDING
    current_round: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_name": self.event_name,
            "message": self.message,
            "context": self.context,
            "source": self.source,
            "severity": self.severity.value,
            "event_status": self.event_status.value,
            "current_round": self.current_round,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            event_id=str(data.get("event_id") or uuid4()),
            event_name=str(data.get("event_name") or ""),
            message=str(data.get("message") or ""),
            context=dict(data.get("context") or {}),
            source=str(data.get("source") or ""),
            severity=SeverityLevel(str(data.get("severity") or SeverityLevel.UNKNOWN.value)),
            event_status=EventStatus(str(data.get("event_status") or EventStatus.PENDING.value)),
            current_round=int(data.get("current_round") or 1),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utc_now()


__all__ = ["Event", "EventStatus", "SeverityLevel", "utc_now"]
