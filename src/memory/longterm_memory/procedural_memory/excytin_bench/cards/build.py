"""Compile reviewed five-field procedure cards with raw-training provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import sqlglot
from sqlglot import exp
import yaml

from .recovery import is_time_recovery, is_alert_device_recovery, is_url_form_recovery, is_parent_role_recovery

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[6]
SOURCE = ROOT / "data/excytin-bench/github/secgym/agents/expel_train/corrects.jsonl"
SCHEMA = HERE.parents[2] / "semantic_memory/excytin_bench/l2"
FIELDS = {"name", "inputs", "steps", "outputs", "constraints"}


def compile_catalog(*, exclude_incidents=()):
    definitions = yaml.safe_load((HERE / "definitions.yaml").read_text(encoding="utf-8"))
    raw = [json.loads(x) for x in SOURCE.read_text(encoding="utf-8").splitlines()]
    questions = []
    for path in sorted((ROOT / "data/excytin-bench/huggingface/questions/train").glob("*.json")):
        incident = re.search(r"incident_\d+", path.name).group()
        questions.extend((x["question"].strip(), incident, i) for i, x in enumerate(json.loads(path.read_text(encoding="utf-8"))))
    attempts = []
    for line, row in enumerate(raw, 1):
        if row["value"].get("reward") != 1:
            continue
        messages = row["value"]["messages"]
        task = next(m["content"] for m in messages if m["role"] == "user")
        matches = [(incident, i) for question, incident, i in questions if question in task]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous train alignment at source line {line}")
        incident, qi = matches[0]
        if incident in exclude_incidents:
            continue
        for mi, m in enumerate(messages):
            match = re.search(r"Action:\s*execute\[(.*)\]\s*$", m.get("content", ""), re.I | re.S) if m["role"] == "assistant" else None
            if not match:
                continue
            assert mi + 1 < len(messages) and messages[mi+1]["role"] == "user"
            obs = messages[mi+1]["content"]
            if re.search(r"(?:ProgrammingError|OperationalError|DatabaseError|InterfaceError):", obs) or re.match(r"^\s*\[\s*\]", obs):
                continue
            if not (obs.lstrip().startswith("[") or re.match(r"^Retrieved [1-9]\d* entries\.", obs)):
                continue
            tree = sqlglot.parse_one(match.group(1), read="mysql")
            if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Table))) != 1 or len(list(tree.find_all(exp.Select))) != 1:
                continue
            where = tree.args.get("where")
            if where is None:
                continue
            attempts.append({"line": line, "message_index": mi, "incident": incident, "question_index": qi,
                             "table": next(tree.find_all(exp.Table)).name,
                             "filters": {c.name for c in where.find_all(exp.Column)},
                             "projection": {c.name for c in tree.expressions if isinstance(c, exp.Column)},
                             "wildcard": any(isinstance(c, exp.Star) for c in tree.expressions),
                             "sql": match.group(1), "observation_sha256": hashlib.sha256(obs.encode()).hexdigest(),
                             "truncation_marker": "(Truncated)" in obs or "Displaying first" in obs})
    cards, audit = {}, {}
    for d in definitions:
        if set(d["card"]) != FIELDS or d["id"] in cards:
            raise ValueError(f"Invalid card: {d['id']}")
        for table, columns in d["dependencies"].items():
            schema = yaml.safe_load((SCHEMA / f"{table}.yaml").read_text(encoding="utf-8"))
            missing = set(columns) - {c["name"] for c in schema["core_columns"]}
            if missing:
                raise ValueError(f"Unknown fields {table}: {missing}")
        rule = d["source_rule"]
        support = [a for a in attempts if a["table"] == rule["table"] and set(rule["filters"]) <= a["filters"]
                   and (a["wildcard"] or set(rule["projection_any"]) & a["projection"])]
        recovery_check = {"empty_time_scope": is_time_recovery,
                          "empty_alert_device_scope": is_alert_device_recovery,
                          "empty_url_form": is_url_form_recovery,
                          "empty_parent_role": is_parent_role_recovery}.get(d.get("recovery_trigger"))
        if d.get("recovery_trigger") and recovery_check is None:
            raise ValueError(f"Unsupported recovery trigger: {d['recovery_trigger']}")
        if recovery_check:
            support = []
            for candidate in attempts:
                messages = raw[candidate["line"] - 1]["value"]["messages"]
                for previous_index in range(candidate["message_index"] - 1, -1, -1):
                    previous = messages[previous_index]
                    match = re.search(r"Action:\s*execute\[(.*)\]\s*$", previous.get("content", ""), re.I | re.S) if previous["role"] == "assistant" else None
                    if not match:
                        continue
                    observation = messages[previous_index + 1]["content"]
                    if re.match(r"^\s*\[\s*\]", observation) and recovery_check(match.group(1), candidate["sql"]):
                        support.append(dict(candidate, preceding_empty_sql=match.group(1), preceding_message_index=previous_index))
                    break
        if not support:
            if exclude_incidents:
                continue
            raise ValueError(f"No successful training support: {d['id']}")
        cards[d["id"]] = {k: d[k] for k in ("card", "requires", "produces", "intents", "dependencies")}
        if d.get("recovery_trigger"):
            cards[d["id"]]["recovery_trigger"] = d["recovery_trigger"]
        audit[d["id"]] = {
            "status": "candidate_source_and_schema_checked",
            "trajectory_count": len({a["line"] for a in support}),
            "incidents": sorted({a["incident"] for a in support}),
            "sources": [{k: a[k] for k in ("line", "message_index", "incident", "question_index", "sql", "observation_sha256", "truncation_marker")} for a in support],
            "scope": "Historical query support; added projections/constraints are reviewed design, not a replay result. Wildcard observations may be truncated.",
        }
        if d.get("recovery_trigger"):
            audit[d["id"]]["recovery_pairs"] = [{k: a[k] for k in ("line", "message_index", "preceding_message_index", "preceding_empty_sql", "sql")} for a in support]
    catalog = {"format": "evidence-pivot-cards-1", "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
               "definition_sha256": hashlib.sha256((HERE / "definitions.yaml").read_bytes()).hexdigest(),
               "excluded_incidents": sorted(exclude_incidents), "cards": cards}
    return catalog, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE)
    parser.add_argument("--exclude-incidents", nargs="*", default=[])
    args = parser.parse_args()
    catalog, audit = compile_catalog(exclude_incidents=args.exclude_incidents)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in (("catalog.json", catalog), ("build_audit.json", audit)):
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    ids = list(catalog["cards"])
    for n in range(10, len(ids)+1, 10):
        print(json.dumps({"completed": n, "batch": ids[n-10:n], "sources": {i: audit[i]["trajectory_count"] for i in ids[n-10:n]}}, ensure_ascii=False), flush=True)
    print(f"Compiled {len(ids)} cards; source/schema checks passed; not live validated.")


if __name__ == "__main__":
    main()
