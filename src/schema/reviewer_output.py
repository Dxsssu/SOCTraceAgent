"""Structured task evidence; provenance and execution limits belong to the runtime."""
from copy import deepcopy
import math

SUMMARY_MAX_CHARS = 240


def parse_review(value):
    if not isinstance(value, dict) or set(value) != {"summary", "facts", "limitations"}:
        raise ValueError("Reviewer requires only summary, facts, limitations")
    summary = value["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a nonempty string")
    if len(summary) > SUMMARY_MAX_CHARS:
        raise ValueError(f"summary exceeds {SUMMARY_MAX_CHARS} characters; move details into facts")
    facts = value["facts"]
    if not isinstance(facts, list):
        raise ValueError("facts must be a list of record objects")
    for fact in facts:
        if not isinstance(fact, dict) or not fact:
            raise ValueError("Each fact must be a nonempty record object")
        for key, item in fact.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Fact fields must be nonempty strings")
            if item is not None and type(item) not in (str, int, float, bool):
                raise ValueError("Fact values must be JSON scalars; quote identifiers and timestamps")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Fact numbers must be finite")
    limitations = value["limitations"]
    if not isinstance(limitations, list) or any(not isinstance(s, str) or not s.strip() for s in limitations):
        raise ValueError("limitations must be a list of nonempty strings")
    return {"summary": summary.strip(), "facts": deepcopy(facts), "limitations": list(limitations)}


def evidence_result(review, execution):
    """Attach trusted provenance and unavoidable gaps, never inferred by the model."""
    result = parse_review(review)
    record = execution.to_dict() if hasattr(execution, "to_dict") else execution
    source_fields = ("execution_id", "query_success", "result_status", "observation_truncated",
                     "original_observation_chars", "execution_status")
    result["source"] = {key: record[key] for key in source_fields if key in record}
    if record.get("observation_truncated"):
        result["limitations"].append("输入结果已截断；事实仅覆盖可见内容，不能据此断言完整性或不存在其他记录。")
    if record.get("query_success") is False or record.get("execution_status") == "failed":
        # A failed query may expose diagnostic identifiers, but not successful evidence.
        result["facts"] = []
        error = record.get("error_message") or record.get("observation")
        if error:
            result["source"]["error"] = error
        result["limitations"].append("本次查询失败，未获得可用于确认调查结论的查询结果。")
    elif record.get("result_status") == "empty_result":
        result["facts"] = []
        result["limitations"].append("本次查询未返回记录；仅限本次查询条件，不能推断相关行为不存在。")
    result["limitations"] = list(dict.fromkeys(result["limitations"]))
    return result
