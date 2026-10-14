"""Bounded output budgets for memory selectors, including benchmark transport."""
def memory_output_limit(system_prompt):
    from src.memory.longterm_memory.semantic_memory.excytin_bench.fallback import SYSTEM_PROMPT, MAX_OUTPUT_TOKENS as SM_LIMIT
    from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import SELECTION_PROMPT, MAX_OUTPUT_TOKENS as PM_LIMIT
    return SM_LIMIT if system_prompt == SYSTEM_PROMPT else PM_LIMIT if system_prompt == SELECTION_PROMPT else None
