from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.memory.longterm_memory import FactualMemoryLibrary, ProceduralMemoryLibrary


class ProceduralMemoryLibraryTests(unittest.TestCase):
    def test_list_documents_parses_front_matter_and_body(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "valid.md").write_text(
                """---
title: Test Workflow
event_types:
  - suspicious_login
tags:
  - auth
summary: Test summary
---

# Heading

Workflow body
""",
                encoding="utf-8",
            )

            library = ProceduralMemoryLibrary(base_dir)
            documents = library.list_documents()

            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].document_id, "valid")
            self.assertEqual(documents[0].title, "Test Workflow")
            self.assertEqual(documents[0].event_types, ("suspicious_login",))
            self.assertIn("Workflow body", documents[0].content)

    def test_list_documents_skips_invalid_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "invalid.md").write_text("# Missing front matter\n", encoding="utf-8")
            (base_dir / "missing_fields.md").write_text(
                """---
title: Missing Summary
event_types:
  - suspicious_login
---
Body
""",
                encoding="utf-8",
            )
            (base_dir / "notes.txt").write_text("ignored", encoding="utf-8")

            library = ProceduralMemoryLibrary(base_dir)

            self.assertEqual(library.list_documents(), [])


class FactualMemoryLibraryTests(unittest.TestCase):
    def test_list_documents_parses_front_matter_and_body(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "factual.md").write_text(
                """---
title: Splunk Background
categories:
  - enterprise_background
tags:
  - splunk
summary: Test factual summary
---

# Context

Splunk BOTS dataset context
""",
                encoding="utf-8",
            )

            library = FactualMemoryLibrary(base_dir)
            documents = library.list_documents()

            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].document_id, "factual")
            self.assertEqual(documents[0].categories, ("enterprise_background",))
            self.assertIn("Splunk BOTS dataset context", documents[0].content)

    def test_list_documents_skips_invalid_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "invalid.md").write_text("# Missing front matter\n", encoding="utf-8")
            (base_dir / "missing_fields.md").write_text(
                """---
title:
summary:
---
Body
""",
                encoding="utf-8",
            )

            library = FactualMemoryLibrary(base_dir)
            self.assertEqual(library.list_documents(), [])


if __name__ == "__main__":
    unittest.main()
