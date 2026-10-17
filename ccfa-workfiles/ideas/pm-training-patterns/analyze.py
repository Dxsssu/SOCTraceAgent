"""Read-only, reproducible mining of ExpeL training query/flow candidates.

Uses raw trajectories and SQL ASTs; does not import the existing PM builder.
Candidate grouping is syntactic evidence, not proof of semantic equivalence.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/excytin-bench/github/secgym/agents/expel_train/corrects.jsonl"
EXECUTE = re.compile(r"Action:\s*execute\[(.*)\]\s*$", re.I | re.S)
ERROR = re.compile(r"(?:ProgrammingError|OperationalError|DatabaseError|InterfaceError):", re.I)


def scalars(value):
    if isinstance(value, (tuple, list)):
        for x in value:
            yield from scalars(x)
    elif isinstance(value, dict):
        for x in value.values():
            yield from scalars(x)
    elif isinstance(value, str):
        yield value
        if value.startswith(("{", "[")):
            try:
                yield from scalars(json.loads(value))
            except (ValueError, TypeError):
                pass


def observed_values(text):
    body = re.sub(r"^Retrieved \d+ entries\. Displaying first \d+ entries\.\s*", "", text)
    body = re.sub(r"\s*\.\.\.\s*\(Truncated\)\s*$", "", body)
    try:
        return set(scalars(ast.literal_eval(body))), True
    except (ValueError, SyntaxError):
        return set(), False


def normalize(tree, *, omit_projection=False):
    tree = tree.copy()
    tables = list(tree.find_all(exp.Table))
    # Resolve aliases only for a simple single-table statement.
    if len(tables) == 1 and len(list(tree.find_all(exp.Select))) == 1:
        for column in tree.find_all(exp.Column):
            column.set("table", None)
        tables[0].set("alias", None)
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.this.lower())
        ident.set("quoted", False)
    # Only direct predicate values become slots. JSON paths, projections,
    # limits, ordering, operators, and Boolean structure remain intact.
    slots = {}
    for lit in list(tree.find_all(exp.Literal)):
        parent = lit.parent
        if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
                               exp.Like, exp.ILike, exp.In, exp.Between)):
            key = (lit.is_string, lit.this)
            slot = slots.setdefault(key, len(slots) + 1)
            if isinstance(parent, (exp.Like, exp.ILike)) and lit.is_string:
                shape = "".join(re.findall(r"[%_]|[^%_]+", lit.this))
                shape = re.sub(r"[^%_]+", f"VALUE{slot}", shape)
                lit.replace(exp.Literal.string(shape))
            else:
                lit.replace(exp.Literal.string(f"VALUE{slot}") if lit.is_string else exp.Placeholder(this=f"number{slot}"))
    if omit_projection:
        tree.set("expressions", [exp.Star()])
    return tree.sql(dialect="mysql")


def simple_select(tree):
    return (isinstance(tree, exp.Select)
            and len(list(tree.find_all(exp.Select))) == 1
            and len(list(tree.find_all(exp.Table))) == 1
            and not any(tree.args.get(x) for x in ("joins", "group", "having", "distinct", "with_"))
            and all(isinstance(x, exp.Column) or isinstance(x, exp.Star) for x in tree.expressions)
            and tree.args.get("where") is not None)


def ref(q):
    return {k: q[k] for k in ("line", "message_index", "incident", "question_index", "sql", "status", "truncated")}


def groups_report(groups):
    result = []
    for signature, queries in groups.items():
        lines = sorted({q["line"] for q in queries})
        if len(lines) < 2:
            continue
        result.append({"signature": signature, "trajectory_count": len(lines),
                       "query_count": len(queries), "lines": lines,
                       "incidents": sorted({q["incident"] for q in queries}),
                       "examples": [ref(q) for q in queries]})
    return sorted(result, key=lambda x: (-x["trajectory_count"], x["signature"]))


def main():
    rows = [json.loads(s) for s in SOURCE.read_text(encoding="utf-8").splitlines()]
    questions = []
    for path in sorted((ROOT / "data/excytin-bench/huggingface/questions/train").glob("*.json")):
        incident = re.search(r"incident_\d+", path.name).group()
        for i, value in enumerate(json.loads(path.read_text(encoding="utf-8"))):
            questions.append((value["question"].strip(), incident, i))
    exact, families, pairs = defaultdict(list), defaultdict(list), defaultdict(list)
    stats, inventory, failures = Counter(), [], []
    for line, row in enumerate(rows, 1):
        messages = row["value"]["messages"]
        task = next(m["content"] for m in messages if m["role"] == "user")
        matches = [q for q in questions if q[0] in task]
        if len(matches) != 1:
            raise ValueError((line, "task alignment", len(matches)))
        _, incident, qi = matches[0]
        stats["trajectories"] += 1
        stats["outer_key_mismatch"] += row["key"]["question"].strip() not in task
        queries, business, seen = [], [], set()
        for mi, message in enumerate(messages):
            if message["role"] != "assistant":
                continue
            match = EXECUTE.search(message["content"])
            if not match:
                if "submit[" not in message["content"]:
                    failures.append({"line": line, "message_index": mi, "reason": "unparsed action"})
                continue
            sql = match.group(1).strip()
            assert mi+1 < len(messages) and messages[mi+1]["role"] == "user"
            observation = messages[mi+1]["content"]
            q = dict(line=line, message_index=mi, incident=incident, question_index=qi,
                     sql=sql, truncated="(Truncated)" in observation or "Displaying first" in observation)
            stats["sql_attempts"] += 1
            try:
                tree = sqlglot.parse_one(sql, read="mysql")
            except Exception as error:
                failures.append({**ref({**q, "status": "parse_error"}), "reason": str(error)})
                stats["parse_error"] += 1
                continue
            if ERROR.search(observation):
                status = "error"
            elif isinstance(tree, (exp.Show, exp.Describe)):
                status = "schema"
            elif re.match(r"^\s*\[\s*\]", observation):
                status = "empty"
            elif observation.lstrip().startswith("[") or re.match(r"^Retrieved [1-9]\d* entries\.", observation):
                status = "rows"
            else:
                status = "unknown"
            q["status"] = status
            stats[status] += 1
            vals, parsed_obs = observed_values(observation)
            if status == "rows":
                stats["rows_observation_parseable"] += parsed_obs
                stats["rows_with_truncation_marker"] += q["truncated"]
            q["observation_parseable"] = parsed_obs
            q["tables"] = sorted({x.name for x in tree.find_all(exp.Table)})
            q["projection"] = [x.sql(dialect="mysql") for x in tree.expressions] if isinstance(tree, exp.Select) else []
            q["signature"] = normalize(tree)
            queries.append(q)
            if status != "rows" or not isinstance(tree, exp.Select):
                continue
            stats["nonempty_selects"] += 1
            if tree.args.get("where"):
                exact[q["signature"]].append(q)
            if simple_select(tree):
                q["family"] = normalize(tree, omit_projection=True)
                families[q["family"]].append(q)
            # Evidence-linked consecutive nonempty business queries; observations
            # must contain exact values used as the next query's predicate values.
            # Initial-context / older values do not count as newly discovered.
            if business:
                prev, prev_values, prev_seen = business[-1]
                where = tree.args.get("where")
                literals = {x.this for x in where.find_all(exp.Literal) if x.is_string and len(x.this) >= 6} if where else set()
                links = sorted(v for v in literals & prev_values if v not in task and v not in prev_seen)
                if links:
                    sig = (prev.get("family", prev["signature"]), q.get("family", q["signature"]))
                    pairs[sig].append({"line": line, "incident": incident,
                                       "from": ref(prev), "to": ref(q),
                                       "observed_binding_values": links,
                                       "intervening_attempts": [ref(x) for x in queries if prev["message_index"] < x["message_index"] < mi]})
            business.append((q, vals, seen.copy()))
            seen.update(vals)
        inventory.append({"line": line, "incident": incident, "question_index": qi,
                          "actual_task": task, "reward": row["value"].get("reward"), "queries": queries})
    pair_report = []
    for signature, occurrences in pairs.items():
        lines = sorted({x["line"] for x in occurrences})
        if len(lines) >= 2:
            pair_report.append({"signature": list(signature), "trajectory_count": len(lines),
                                "lines": lines, "incidents": sorted({x["incident"] for x in occurrences}),
                                "examples": occurrences})
    pair_report.sort(key=lambda x: (-x["trajectory_count"], str(x["signature"])))
    strict, family = groups_report(exact), groups_report(families)
    reviewed = [
        {"name": "根据 URL 查询相关邮件的发送信息", "lines": [11, 52],
         "decision": "merge_candidate_with_output_variants", "scope": "two_step",
         "reason": "Both traverse EmailUrlInfo.Url -> NetworkMessageId -> EmailEvents; sender identity and sender IP require different explicit projections.",
         "exclude_from_same_goal": [197],
         "caveat": "Line 197 shares the route but asks for recipient/click attribution. It can share the lookup primitive, not the sender conclusion."},
        {"name": "根据文件名查询进程命令行", "lines": [9, 25, 96],
         "decision": "merge_candidate", "scope": "single_step",
         "reason": "Identical ProcessCommandLine projection and FileName equality predicate; file value is parameterized.",
         "caveat": "All three sources are incident_39. Returns candidate process commands, not proof of a particular attack or remote execution. Line 96 also contains a distinct initiating-process query."},
        {"name": "根据邮件标识查询邮件内 URL", "lines": [10, 20, 93, 127, 178, 194, 230, 231],
         "decision": "merge_candidate", "scope": "single_step",
         "reason": "Same SELECT Url FROM EmailUrlInfo WHERE NetworkMessageId equality structure.",
         "caveat": "Does not establish maliciousness or a click; source observations are marked truncated."},
        {"name": "处理记录到邮件 URL", "lines": [10, 20, 127],
         "decision": "merge_candidate_with_action_conditions", "scope": "two_step",
         "reason": "Returned NetworkMessageId is observed and then used in EmailUrlInfo query.",
         "caveat": "Lines 10 and 20 reuse the same incident and message, not independent cross-case evidence. Line 127 is incident_134. Action terms are not interchangeable and must remain explicit."},
        {"name": "告警检索到关联证据", "lines": [23, 43, 130, 234],
         "decision": "shared_route_requires_goal_specific_output", "scope": "two_step",
         "reason": "Same title/category search then observed AlertId lookup.",
         "caveat": "AlertEvidence contains different entity types and roles; route similarity alone does not justify one fixed answer-producing PM."},
        {"name": "邮件发送信息与收件人信息", "lines": [11, 52, 139, 197],
         "decision": "share_query_primitive_do_not_collapse_goal", "scope": "single_step",
         "reason": "All query EmailEvents by NetworkMessageId, but projections/answer relationships differ.",
         "caveat": "Receiving an email does not by itself establish who clicked its URL."},
    ]
    for item in reviewed:
        item["incidents"] = sorted({inventory[n-1]["incident"] for n in item["lines"]})
        item["validation"] = "source_inspection_only_no_live_replay"
    assert stats["sql_attempts"] == sum(stats[x] for x in ("schema", "empty", "rows", "error", "unknown", "parse_error"))
    assert len(inventory) == len(rows) and not failures
    report = {"source": str(SOURCE.relative_to(ROOT)), "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "counts": dict(stats), "strict_repeated_queries": strict,
              "projection_variant_families": family, "evidence_linked_pairs": pair_report,
              "reviewed_candidates": reviewed,
              "extraction_failures": failures, "trajectory_inventory": inventory,
              "limitations": ["Parameterized SQL equality does not prove semantic applicability.",
                              "Projection-variant families require compatible outputs and case scope before merging.",
                              "Pairs use exact newly observed string values, not a complete causal/dataflow proof.",
                              "Only parseable visible observations support exact value links; this is a conservative scan, not an exhaustive enumeration of semantic workflows.",
                              "Source truncation markers and unknown/empty/error results remain explicit.",
                              "No SQL replay or model calls; no existing PM design/content used."]}
    (OUT / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"counts": stats, "strict_groups": len(strict), "family_groups": len(family),
                      "pair_groups": len(pair_report), "failures": len(failures)}, ensure_ascii=False, indent=2))
    for label, entries in (("FAMILY", family[:22]), ("PAIR", pair_report[:15])):
        for entry in entries:
            print(label, entry["trajectory_count"], entry["lines"], entry["signature"])


if __name__ == "__main__":
    main()
