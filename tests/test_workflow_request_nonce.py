from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.schema.reviewer_output import parse_review
from src.workflow.context import MemoryContextService


def test_role_retry_refreshes_system_prefix_and_preserves_user(monkeypatch):
    characters = iter("aB3xY9kL7mN2")
    monkeypatch.setattr(
        "src.benchmarks.excytin_bench.workflow.secrets.choice",
        lambda alphabet: next(characters),
    )
    calls = []

    def model(system, user):
        calls.append((system, user))
        return "invalid" if len(calls) == 1 else 'summary: reviewed\nfacts: []\nlimitations: []'

    workflow = ExcytinBenchWorkflow(
        llm=model, memory_contexts=MemoryContextService(enabled=False)
    )
    result = workflow._call_yaml_role(
        "reviewer", "system instructions", "original input", validator=parse_review
    )
    assert result == {"summary": "reviewed", "facts": [], "limitations": []}
    assert calls[0] == ("aB3xY9\nsystem instructions", "original input")
    assert calls[1][0] == "kL7mN2\nsystem instructions"
    assert calls[1][1].startswith("original input\n上一次输出未通过校验")
