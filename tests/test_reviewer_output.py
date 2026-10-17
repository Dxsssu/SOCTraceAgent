import pytest

from src.schema.reviewer_output import parse_review


def test_character_boundaries_include_punctuation_spaces_and_ascii():
    value = {"summary": "中" * 77 + "a! "}
    assert len(value["summary"]) == 80
    assert parse_review(value) == {"summary": value["summary"].strip()}
    with pytest.raises(ValueError, match="exceeds"):
        parse_review({"summary": value["summary"] + "x"})


@pytest.mark.parametrize("value", [
    {},
    {"summary": "结果", "suggestion": "继续"},
    {"summary": "结果", "decision": "continue"},
    {"summary": []},
    {"summary": " "},
    {"summary": 123},
])
def test_only_summary_nonempty_string_is_accepted(value):
    with pytest.raises(ValueError):
        parse_review(value)
