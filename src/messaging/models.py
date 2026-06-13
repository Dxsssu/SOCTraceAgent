from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .message_types import MessageType, RoleName


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class MessageEnvelope:
    """Lightweight message envelope for auditing, debugging, and future UI display."""

    event_id: str
    message_type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    round_id: int | None = None
    from_role: RoleName = RoleName.SYSTEM
    to_role: RoleName | None = None
    message_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "event_id": self.event_id,
            "round_id": self.round_id,
            "from_role": self.from_role.value,
            "to_role": self.to_role.value if self.to_role else None,
            "message_type": self.message_type.value,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageEnvelope":
        created_at_raw = data.get("created_at")
        created_at = (
            datetime.fromisoformat(created_at_raw)
            if isinstance(created_at_raw, str) and created_at_raw
            else utc_now()
        )
        return cls(
            message_id=str(data.get("message_id") or uuid4()),
            event_id=str(data["event_id"]),
            round_id=int(data["round_id"]) if data.get("round_id") is not None else None,
            from_role=RoleName(str(data.get("from_role") or RoleName.SYSTEM.value)),
            to_role=RoleName(str(data["to_role"])) if data.get("to_role") else None,
            message_type=MessageType(str(data["message_type"])),
            payload=dict(data.get("payload") or {}),
            created_at=created_at,
        )


@dataclass(slots=True, frozen=True)
class MessageQuery:
    """Message query filter."""

    event_id: str
    round_id: int | None = None
    from_role: RoleName | None = None
    to_role: RoleName | None = None
    message_type: MessageType | None = None
    limit: int | None = None


__all__ = ["MessageEnvelope", "MessageQuery", "utc_now"]
