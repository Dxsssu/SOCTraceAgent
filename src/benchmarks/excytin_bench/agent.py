from __future__ import annotations

from typing import Any

from .workflow import ExcytinBenchWorkflow


class ExcytinBenchAgent:
    """Duck-typed SecGym agent backed by the dedicated SOCTrace workflow."""

    def __init__(
        self,
        config_list: list[dict[str, Any]] | None = None,
        *,
        max_steps: int = 25,
        workflow: ExcytinBenchWorkflow | None = None,
        **_: Any,
    ) -> None:
        # ``config_list`` is accepted for compatibility with SecGym's constructors;
        # SOCTraceAgent reads its model configuration from the project's .env.
        self.config_list = config_list or []
        self.max_steps = max_steps
        self.workflow = workflow or ExcytinBenchWorkflow(max_steps=max_steps)
        self.messages: list[dict[str, str]] = []
        self.step_count = 0
        self._runtime_info: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "SOCTraceLTM"

    def reset(
        self,
        question_dict: dict[str, Any] | None = None,
        **runtime_info: Any,
    ) -> None:
        self.workflow.reset()
        self.messages = []
        self.step_count = 0
        self._runtime_info = dict(runtime_info)
        if question_dict is not None:
            # This supports direct use while retaining the strict context/question
            # allowlist inside workflow.start(). The official loop supplies the
            # question as the first observation instead.
            self.workflow.start(question_dict, runtime_info=runtime_info)

    def act(self, observation: str) -> tuple[str, bool]:
        action = self.workflow.act(observation, runtime_info=self._runtime_info)
        self.step_count += 1
        self.messages.append(
            {
                "role": "assistant",
                "content": f"{action.action_type.value}:{action.content}",
            }
        )
        return action.as_secgym_action()

    def get_logging(self) -> dict[str, Any]:
        return {
            "messages": list(self.messages),
            "soc_trace_workflow": self.workflow.get_logging(),
        }


__all__ = ["ExcytinBenchAgent"]
