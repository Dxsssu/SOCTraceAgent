from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from .event import utc_now


@dataclass(slots=True, frozen=True)
class RoundReview:
    """Reviewer 在每一轮结束后生成的总结对象。"""

    event_id: str
    round_id: int
    summary_text: str = ""
    findings: tuple[str, ...] = field(default_factory=tuple)
    gaps: tuple[str, ...] = field(default_factory=tuple)
    recommendations: tuple[str, ...] = field(default_factory=tuple)
    review_id: str = field(default_factory=lambda: str(uuid4()))
    created_by: str = "_reviewer"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "event_id": self.event_id,
            "round_id": self.round_id,
            "summary_text": self.summary_text,
            "findings": list(self.findings),
            "gaps": list(self.gaps),
            "recommendations": list(self.recommendations),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoundReview":
        return cls(
            review_id=str(data.get("review_id") or uuid4()),
            event_id=str(data["event_id"]),
            round_id=int(data.get("round_id") or 1),
            summary_text=str(data.get("summary_text") or ""),
            findings=tuple(str(item) for item in (data.get("findings") or [])),
            gaps=tuple(str(item) for item in (data.get("gaps") or [])),
            recommendations=tuple(str(item) for item in (data.get("recommendations") or [])),
            created_by=str(data.get("created_by") or "_reviewer"),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utc_now()


__all__ = ["RoundReview"]
