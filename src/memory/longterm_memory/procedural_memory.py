from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._markdown_memory import coerce_str_tuple, parse_markdown_memory


@dataclass(frozen=True, slots=True)
class ProceduralMemoryDocument:
    """Long-term procedural memory markdown document."""

    path: Path
    title: str
    event_types: tuple[str, ...]
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
            "event_types": list(self.event_types),
            "tags": list(self.tags),
            "summary": self.summary,
            "path": str(self.path),
        }


class ProceduralMemoryLibrary:
    """Read markdown procedural memory docs from long-term memory."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent / "procedural_memory"
        self.base_dir = Path(base_dir)

    def list_documents(self) -> list[ProceduralMemoryDocument]:
        if not self.base_dir.exists():
            return []

        documents: list[ProceduralMemoryDocument] = []
        for path in sorted(self.base_dir.glob("*.md")):
            document = self._parse_document(path)
            if document is not None:
                documents.append(document)
        return documents

    def get_document(self, document_id: str) -> ProceduralMemoryDocument | None:
        normalized = document_id.strip()
        if not normalized:
            return None
        for document in self.list_documents():
            if document.document_id == normalized:
                return document
        return None

    @staticmethod
    def _parse_document(path: Path) -> ProceduralMemoryDocument | None:
        parsed_document = parse_markdown_memory(path)
        if parsed_document is None:
            return None
        parsed, content = parsed_document

        title = str(parsed.get("title") or "").strip()
        summary = str(parsed.get("summary") or "").strip()
        event_types = coerce_str_tuple(parsed.get("event_types"))
        tags = coerce_str_tuple(parsed.get("tags"))
        if not title or not summary or not event_types:
            return None

        return ProceduralMemoryDocument(
            path=path,
            title=title,
            event_types=event_types,
            tags=tags,
            summary=summary,
            content=content,
        )


__all__ = ["ProceduralMemoryDocument", "ProceduralMemoryLibrary"]
