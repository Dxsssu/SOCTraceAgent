from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._markdown_memory import coerce_str_tuple, parse_markdown_memory


@dataclass(frozen=True, slots=True)
class FactualMemoryDocument:
    """Long-term factual memory markdown document."""

    path: Path
    title: str
    tags: tuple[str, ...]
    summary: str
    content: str
    categories: tuple[str, ...] = ()

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
            "categories": list(self.categories),
        }


class FactualMemoryLibrary:
    """Read markdown factual memory docs from long-term memory."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent / "factual_memory"
        self.base_dir = Path(base_dir)

    def list_documents(self) -> list[FactualMemoryDocument]:
        if not self.base_dir.exists():
            return []

        documents: list[FactualMemoryDocument] = []
        for path in sorted(self.base_dir.glob("*.md")):
            document = self._parse_document(path)
            if document is not None:
                documents.append(document)
        return documents

    @staticmethod
    def _parse_document(path: Path) -> FactualMemoryDocument | None:
        parsed_document = parse_markdown_memory(path)
        if parsed_document is None:
            return None
        parsed, content = parsed_document

        title = str(parsed.get("title") or "").strip()
        summary = str(parsed.get("summary") or "").strip()
        tags = coerce_str_tuple(parsed.get("tags"))
        categories = coerce_str_tuple(parsed.get("categories"))
        if not title or not summary:
            return None

        return FactualMemoryDocument(
            path=path,
            title=title,
            tags=tags,
            summary=summary,
            content=content,
            categories=categories,
        )


__all__ = ["FactualMemoryDocument", "FactualMemoryLibrary"]
