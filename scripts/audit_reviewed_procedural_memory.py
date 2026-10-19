"""Assemble coverage and reference-only branch/recovery knowledge from every review."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sqlglot
from sqlglot import exp
from scripts.review_procedural_trajectories import inputs, OUTPUT, save, sha, validate
from scripts.build_reviewed_procedural_memory import apply_review_notes
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
    digest,
    safe_text,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.sanitize import (
    parameterize_sql,
)


def sql_shape(sql):
    """Parameterize even an observed failed query; never make it executable."""
    tree = sqlglot.parse_one(sql, read="mysql")
    parameterized, kinds = parameterize_sql(tree)
    return {
        "sql_shape": parameterized,
        "parameter_types": kinds,
        "tables": sorted({t.name for t in tree.find_all(exp.Table)}),
        "operation": tree.key,
    }


def recovery_shape(recovery, attempts, shapes):
    """Keep a terminal failure without inventing an unobserved recovery query."""
    start, end = recovery["from"], recovery["to"]
    if start not in attempts or (
        end is not None and (end not in attempts or start >= end)
    ):
        raise ValueError("Invalid recovery indices")
    return {
        "before": shapes[start],
        "after": shapes[end] if end is not None else None,
        "observed_guard": attempts[start]["status"],
        "after_status": attempts[end]["status"] if end is not None else "not_attempted",
        "unrecovered": end is None,
    }


def build_audit(output=OUTPUT):
    reviewer_checks = {}
    note_sources = []
    for path in sorted(output.glob("range_*_notes.json")):
        notes = json.loads(path.read_text())
        entries = notes.get(
            "entries", notes.get("trajectories", notes.get("notes", []))
        )
        checked = set(notes.get("structured_review_checked_indices", []))
        checked.update(e.get("index", e.get("trajectory_index")) for e in entries)
        checked.discard(None)
        note_sources.append(
            {
                "path": path.name,
                "sha256": sha(path.read_text()),
                "method": notes.get("method"),
                "checked_indices": sorted(checked),
            }
        )
        for index in checked:
            reviewer_checks.setdefault(index, []).append(path.name)
    branches, recoveries, rows, exclusions, unresolved, unsupported = (
        {},
        {},
        [],
        Counter(),
        [],
        [],
    )
    for payload in inputs():
        directory = output / f"{payload['index']:03d}"
        if not (directory / "review.json").exists():
            continue
        result = json.loads((directory / "review.json").read_text())
        review = validate(result["review"], payload)
        assert result["source_sha256"] == sha(json.dumps(payload, ensure_ascii=False))
        if not (directory / "audit.json").exists():
            continue  # A concurrent reader can finish after the latest build snapshot.
        audit = json.loads((directory / "audit.json").read_text())
        review, _ = apply_review_notes(review, payload["index"], output)
        attempts = {a["sequence"]: a for a in audit["attempts"]}
        shapes = {a["sequence"]: sql_shape(a["sql"]) for a in payload["attempt_index"]}
        protected = [
            v.this
            for a in payload["attempt_index"]
            for v in sqlglot.parse_one(a["sql"], read="mysql").find_all(exp.Literal)
        ]

        def source(seqs):
            return {
                "trajectory_index": payload["index"],
                "episode_id": payload["episode_id"],
                "incident": payload["incident"],
                "sequences": seqs,
            }

        for snippet in review["snippets"]:
            for link in snippet["links"]:
                if (
                    link["type"] == "dataflow"
                    and attempts[link["from"]]["status"] == "schema_result"
                ):
                    link["type"] = "control"
            controls = [l for l in snippet["links"] if l["type"] != "dataflow"]
            if not controls:
                continue
            seqs = snippet["sequences"]
            remap = {seq: "s" + str(i + 1) for i, seq in enumerate(seqs)}
            steps = [
                {
                    "id": remap[seq],
                    **shapes[seq],
                    "observed_status": attempts[seq]["status"],
                }
                for seq in seqs
            ]
            edges = [
                {
                    "from": remap[l["from"]],
                    "to": remap[l["to"]],
                    "type": l["type"],
                    "observed_guard": attempts[l["from"]]["status"],
                }
                for l in controls
            ]
            edges = list({json.dumps(e, sort_keys=True): e for e in edges}.values())
            key = {"steps": steps, "branches": edges}
            ident = "branch_" + digest(key)
            entry = branches.setdefault(
                ident,
                {
                    "id": ident,
                    **key,
                    "status": "reference_only",
                    "name": safe_text(snippet["name"], protected),
                    "planner_guides": [],
                    "caution": [],
                    "sources": [],
                },
            )
            for guide in snippet["step_guides"]:
                text = safe_text(guide["guide"], protected)
                item = {"step": remap[guide["sequence"]], "guide": text}
                if item not in entry["planner_guides"]:
                    entry["planner_guides"].append(item)
            entry["caution"] = list(
                dict.fromkeys(
                    [
                        *entry["caution"],
                        *[safe_text(c, protected) for c in snippet["caution"]],
                        "这是已观察路线的参考记录，不是执行计划；报错 SQL 仅作诊断证据。",
                        "放宽事件、时间或主机条件后的命中必须重新证明事件归属。",
                        "原轨迹的回退动作可能不恰当；需结合当前语义重新选表和校验字段。",
                    ]
                )
            )
            entry["sources"].append(source(seqs))

        for recovery in review["recovery"]:
            start, end = recovery["from"], recovery["to"]
            key = recovery_shape(recovery, attempts, shapes)
            ident = "recovery_" + digest(key)
            entry = recoveries.setdefault(
                ident,
                {
                    "id": ident,
                    **key,
                    "status": "reference_only",
                    "lessons": [],
                    "sources": [],
                },
            )
            lesson = {
                k: safe_text(recovery[k], protected)
                for k in ("condition", "change", "lesson")
            }
            if lesson not in entry["lessons"]:
                entry["lessons"].append(lesson)
            entry["sources"].append(source([start] if end is None else [start, end]))

        for attempt in attempts.values():
            exclusions[attempt.get("exclusion") or "compiled_successful_query"] += 1
            if attempt["status"] == "rows_returned" and attempt.get("exclusion"):
                seq = attempt["sequence"]
                unsupported.append(
                    {
                        "source": source([seq]),
                        "status": "reference_only",
                        "reason": attempt["exclusion"],
                        **shapes[seq],
                        "planner_guide": safe_text(attempt["purpose"], protected),
                        "finding": safe_text(attempt["finding"], protected),
                    }
                )
        for finding in audit["quality_findings"]:
            if "link" not in finding:
                continue
            link = finding["link"]
            unresolved.append(
                {
                    "trajectory_index": payload["index"],
                    "episode_id": payload["episode_id"],
                    "proposal": finding.get("proposal"),
                    "relation": link,
                    "status": "not_executable_without_further_validation",
                }
            )
        rows.append(
            {
                "index": payload["index"],
                "episode_id": payload["episode_id"],
                "incident": payload["incident"],
                "source_sha256": result["source_sha256"],
                "prompt_sha256": result["prompt_sha256"],
                "messages": len(payload["messages"]),
                "sql_attempts": len(attempts),
                "model_final_assessment": result["review"]["final_assessment"],
                "effective_final_assessment": review["final_assessment"],
                "quality_findings": audit["quality_findings"],
                "reviewer_overrides": audit.get("reviewer_overrides", []),
                "reviewer_check_sources": reviewer_checks.get(payload["index"], []),
                "audit_path": f"{payload['index']:03d}/audit.json",
            }
        )
    report = {
        "method": "Full original messages per model review; deterministic source/SQL coverage and field/value-flow checks; reviewer notes kept separately.",
        "expected_trajectories": 237,
        "reviewed_trajectories": len(rows),
        "expected_sql_attempts": sum(len(p["attempt_index"]) for p in inputs()),
        "reviewed_sql_attempts": sum(r["sql_attempts"] for r in rows),
        "reviewed_messages": sum(r["messages"] for r in rows),
        "reviewer_checked_trajectories": sum(
            bool(r["reviewer_check_sources"]) for r in rows
        ),
        "missing_reviewer_indices": sorted(set(range(1, 238)) - reviewer_checks.keys()),
        "reviewer_note_sources": note_sources,
        "missing_indices": sorted(set(range(1, 238)) - {r["index"] for r in rows}),
        "attempt_coverage": dict(exclusions),
        "assessment_is_model_judgment_not_ground_truth": True,
        "final_assessments": dict(
            Counter(r["model_final_assessment"]["supported"] for r in rows)
        ),
        "zero_sql_trajectories": [r["index"] for r in rows if not r["sql_attempts"]],
        "reference_branch_recipes": len(branches),
        "reference_recovery_rules": len(recoveries),
        "unresolved_value_relations": len(unresolved),
        "unsupported_successful_queries": len(unsupported),
        "trajectories": rows,
    }
    save(output / "reading_ledger.json", report)
    save(
        output / "branch_recipes.json",
        {"status": "reference_only", "records": list(branches.values())},
    )
    save(
        output / "recovery_rules.json",
        {"status": "reference_only", "records": list(recoveries.values())},
    )
    save(output / "unresolved_relations.json", {"records": unresolved})
    save(
        output / "unsupported_queries.json",
        {"status": "reference_only", "records": unsupported},
    )
    return {k: v for k, v in report.items() if k != "trajectories"}


if __name__ == "__main__":
    print(json.dumps(build_audit(), ensure_ascii=False))
