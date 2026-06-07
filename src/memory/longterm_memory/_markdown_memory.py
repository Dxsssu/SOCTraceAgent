from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class MarkdownMemoryDocument:
    path: Path
    title: str
    tags: tuple[str, ...]
    summary: str
    content: str

    @property
    def document_id(self) -> str:
        return self.path.stem

    def summary_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "tags": list(self.tags),
            "summary": self.summary,
            "path": str(self.path),
        }


def parse_markdown_memory(path: Path) -> tuple[dict[str, Any], str] | None:
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text.startswith("---"):
        return None

    parts = raw_text.split("---", 2)
    if len(parts) < 3:
        return None
    _, front_matter_raw, content_raw = parts

    parsed = yaml.safe_load(front_matter_raw.strip()) or {}
    if not isinstance(parsed, dict):
        return None
    return parsed, content_raw.strip()


def coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ()
    text = str(value).strip()
    return (text,) if text else ()

