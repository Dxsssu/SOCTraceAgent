"""Budgets for standalone memory tools without loading LTM in role requests."""
import sys


def memory_output_limit(system_prompt):
    # Standalone selectors already imported their own prompt definitions.
    # Ordinary workflow requests must not import memory modules just to compare.
    for name, prompt_name in (
        ("src.memory.longterm_memory.semantic_memory.excytin_bench.fallback", "SYSTEM_PROMPT"),
        ("src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval", "SELECTION_PROMPT"),
    ):
        module = sys.modules.get(name)
        if module is not None and system_prompt == getattr(module, prompt_name, None):
            return module.MAX_OUTPUT_TOKENS
    return None
