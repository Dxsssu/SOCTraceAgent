"""Compact Reviewer output contract; limits count all Unicode characters."""

SUMMARY_MAX_CHARS = 80


def parse_review(value):
    if not isinstance(value, dict) or set(value) != {"summary"}:
        raise ValueError("Reviewer must output only summary")
    result = {}
    for key, limit in (("summary", SUMMARY_MAX_CHARS),):
        text = value[key]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{key} must be a nonempty string")
        if len(text) > limit:
            raise ValueError(f"{key} exceeds {limit} characters; shorten it")
        result[key] = text.strip()
    return result
