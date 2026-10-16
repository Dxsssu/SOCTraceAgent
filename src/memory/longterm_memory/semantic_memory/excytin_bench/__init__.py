"""Two-level ExCyTIn semantic memory: global catalog and per-table dictionaries."""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_PATH = PACKAGE_DIR / "l1.yaml"
DEFAULT_L2_DIR = PACKAGE_DIR / "l2"

__all__ = ["DEFAULT_KNOWLEDGE_PATH", "DEFAULT_L2_DIR", "PACKAGE_DIR"]
