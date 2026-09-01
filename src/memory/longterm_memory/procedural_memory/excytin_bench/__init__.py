"""ExCyTIn-Bench procedural-memory assets and import utilities."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_PATH = PACKAGE_DIR / "procedural_memory.json"

__all__ = ["DEFAULT_KNOWLEDGE_PATH", "PACKAGE_DIR"]
