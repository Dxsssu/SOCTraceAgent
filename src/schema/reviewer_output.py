"""Compact Reviewer output contract; limits count all Unicode characters."""

SUMMARY_MAX_CHARS = 80
SUGGESTION_MAX_CHARS = 60


def parse_review(value):
    if not isinstance(value, dict) or set(value) != {"summary", "suggestion"}:
        raise ValueError("Reviewer must output only summary and suggestion")
    result = {}
    for key, limit in (("summary", SUMMARY_MAX_CHARS), ("suggestion", SUGGESTION_MAX_CHARS)):
        text = value[key]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{key} must be a nonempty string")
        if len(text) > limit:
            raise ValueError(f"{key} exceeds {limit} characters; shorten it")
        result[key] = text.strip()
    return result


def review_decision(review):
    return {"提交答案": "ready_to_submit", "停止调查": "cannot_continue"}.get(
        review["suggestion"], "continue")
