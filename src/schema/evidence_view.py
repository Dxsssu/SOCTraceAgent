"""Lossless prompt-only row encoding inside the existing G/E/T tree."""
from copy import deepcopy

import yaml


class _Row(list):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Row, lambda dumper, value: dumper.represent_sequence(
    "tag:yaml.org,2002:seq", value, flow_style=True))


def dump_evidence_prompt(payload, *, compact=False):
    original = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if not compact:
        return original
    view = deepcopy(payload)
    changed = False

    def visit(nodes):
        nonlocal changed
        for node in nodes:
            if "children" in node:
                visit(node["children"])
            result = node.get("execution_result")
            if not isinstance(result, dict):
                continue
            facts = result.get("facts")
            if not isinstance(facts, list) or len(facts) < 3 or not all(isinstance(row, dict) and row for row in facts):
                continue
            columns = list(facts[0])
            # Exact ordered key equality preserves missing vs null and avoids
            # mixing heterogeneous records or inventing a union schema.
            if any(list(row) != columns for row in facts):
                continue
            if any(value is not None and type(value) not in (str, int, float, bool)
                   for row in facts for value in row.values()):
                continue
            result["facts"] = {"columns": _Row(columns),
                               "rows": [_Row([row[key] for key in columns]) for row in facts]}
            changed = True

    visit(view.get("itt", []))
    if not changed:
        return original
    view["facts_encoding"] = "facts 的 columns 是字段顺序；rows 每行按该顺序对应一条原始事实。null 与空字符串不同。树结构、来源及限制保持原义。"
    candidate = yaml.dump(view, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=160)
    # Include the decoding explanation in the comparison; small tables can
    # cost more than the original. This is bytes, not a tokenizer guarantee.
    return candidate if len(candidate.encode()) < 0.9 * len(original.encode()) else original
