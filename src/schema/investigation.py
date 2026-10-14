"""Task content exposed to models; runtime routing stays on Event."""
from collections.abc import Mapping
from typing import Any

from .event import Event

_RUNTIME_KEYS = {"runtime_info", "memory_profile", "benchmark", "dataset", "source_dataset",
                 "attack", "noise_level", "qid", "layer", "split"}


def task_input(event: Event) -> dict[str, Any]:
    context = event.context
    core = context.get("investigation_input")
    if isinstance(core, Mapping):
        return {"context": core.get("context", ""), "question": core.get("question", "")}
    return {
        "context": {k: v for k, v in context.items() if k not in _RUNTIME_KEYS},
        "question": event.message,
    }
