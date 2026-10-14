import pytest

from src.schema.reviewer_output import parse_review, review_decision


def test_character_boundaries_include_punctuation_spaces_and_ascii():
    value = {"summary": "中" * 77 + "a! ", "suggestion": "查" * 60}
    assert len(value["summary"]) == 80
    assert parse_review(value)["summary"] == value["summary"].strip()
    for field in value:
        with pytest.raises(ValueError, match="exceeds"):
            parse_review({**value, field: value[field] + "x"})


@pytest.mark.parametrize("value", [
    {"summary": "结果"},
    {"summary": "结果", "suggestion": "继续", "decision": "continue"},
    {"summary": [], "suggestion": "继续"},
    {"summary": "结果", "suggestion": " "},
    {"summary": 123, "suggestion": "继续"},
])
def test_only_two_nonempty_strings_are_accepted(value):
    with pytest.raises(ValueError):
        parse_review(value)


@pytest.mark.parametrize("suggestion, decision", [
    ("提交答案", "ready_to_submit"),
    ("停止调查", "cannot_continue"),
    ("核查设备关联后再提交答案", "continue"),
    ("查明进程创建时间", "continue"),
])
def test_only_exact_terminal_suggestions_end_investigation(suggestion, decision):
    assert review_decision(parse_review({"summary": "E1 返回记录", "suggestion": suggestion})) == decision
