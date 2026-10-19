"""Compile full-trajectory reviews, verify value flow, and publish bounded progress batches."""

from __future__ import annotations
import argparse
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sqlglot
from sqlglot import exp
from dotenv import dotenv_values
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor, INCIDENT_PORTS
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
    digest,
    normalize_sql,
    verify_link,
    safe_text,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.sql_parser import (
    classify_result,
)
from src.memory.longterm_memory.procedural_memory.snippets.definitions import (
    output_contract,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import (
    TrajectorySnippetStore,
)
from scripts.review_procedural_trajectories import (
    inputs,
    validate,
    save,
    sha,
    OUTPUT,
)


def schema_snapshot(output):
    path = output / "target_schemas.json"
    if path.exists():
        return json.loads(path.read_text())
    config = dotenv_values(ROOT / "docker/excytin-mysql/.env")
    schemas = {}
    for incident in INCIDENT_PORTS:
        db = MySQLQueryExecutor(incident, password=config["MYSQL_ROOT_PASSWORD"])
        try:
            db.cursor.execute(
                "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION"
            )
            tables = {}
            for table, col, dtype in db.cursor.fetchall():
                tables.setdefault(table, {})[col] = dtype
            schemas[incident] = tables
        finally:
            db.close()
    save(path, schemas)
    return schemas


def apply_review_notes(review, index, output):
    review = deepcopy(review)
    applied = []
    for path in sorted(output.glob("range_*_notes.json")):
        data = json.loads(path.read_text())
        entries = (
            data
            if isinstance(data, list)
            else data.get("entries", data.get("trajectories", data.get("notes", [])))
        )
        for note in entries:
            if note.get("index", note.get("trajectory_index")) != index:
                continue
            if note.get("final_assessment_override"):
                review["final_assessment"] = deepcopy(note["final_assessment_override"])
                applied.append(
                    {
                        "source": path.name,
                        "final_assessment_override": note["final_assessment_override"],
                    }
                )
            for link in note.get("link_additions", []):
                seqs = [link["from"], link["to"]]
                assert seqs[0] < seqs[1]
                attempts = {a["sequence"]: a for a in review["attempt_reviews"]}
                assert set(seqs) <= attempts.keys()
                review["snippets"].append(
                    {
                        "name": "复核补全的非相邻证据关联",
                        "intent": attempts[seqs[-1]]["purpose"],
                        "trigger_entity": {},
                        "sequences": seqs,
                        "step_guides": [
                            {
                                "sequence": seq,
                                "guide": attempts[seq]["purpose"],
                                "outputs": [],
                            }
                            for seq in seqs
                        ],
                        "links": [deepcopy(link)],
                        "caution": [
                            "中间失败或空结果没有提供新实体，后一步仍使用已核实的前序结果；需重新核对当前事件范围。"
                        ],
                        "outcome": "partial",
                    }
                )
                applied.append({"source": path.name, "link_addition": link})
            for override in note.get("link_overrides", []):
                for snippet in review["snippets"]:
                    for link in snippet["links"]:
                        if (
                            link["from"] == override["from"]
                            and link["to"] == override["to"]
                        ):
                            link.update(
                                {
                                    k: v
                                    for k, v in override.items()
                                    if k not in {"from", "to"}
                                }
                            )
                applied.append({"source": path.name, "link_override": override})
            for override in note.get("sequence_overrides", []):
                seq = override["sequence"]
                for attempt in review["attempt_reviews"]:
                    if attempt["sequence"] == seq:
                        for field in ("purpose", "finding", "next_reason"):
                            if override.get(field):
                                attempt[field] = override[field]
                        if override.get("caution"):
                            caution = override["caution"]
                            cautions = (
                                caution if isinstance(caution, list) else [caution]
                            )
                            for snippet in review["snippets"]:
                                if seq not in snippet["sequences"]:
                                    continue
                                snippet["caution"] = list(
                                    dict.fromkeys([*snippet["caution"], *cautions])
                                )
                                if override.get("purpose"):
                                    for guide in snippet["step_guides"]:
                                        if guide["sequence"] == seq:
                                            guide["guide"] = override["purpose"]
                            review["snippets"].append(
                                {
                                    "name": override.get("purpose", attempt["purpose"]),
                                    "intent": override.get(
                                        "purpose", attempt["purpose"]
                                    ),
                                    "trigger_entity": {},
                                    "sequences": [seq],
                                    "step_guides": [
                                        {
                                            "sequence": seq,
                                            "guide": attempt["purpose"],
                                            "outputs": [],
                                        }
                                    ],
                                    "links": [],
                                    "caution": cautions,
                                    "outcome": "partial",
                                }
                            )
                        applied.append({"source": path.name, **override})
    return review, applied


def compile_episode(payload, review, schema):
    attempts = {}
    templates = {}
    proposal = []
    quality = []
    protected = []
    guides = {a["sequence"]: a for a in review["attempt_reviews"]}
    requested = {a["sequence"]: [] for a in payload["attempt_index"]}
    for snippet in review["snippets"]:
        for guide in snippet["step_guides"]:
            requested[guide["sequence"]].extend(guide["outputs"])
    for ref in payload["attempt_index"]:
        seq = ref["sequence"]
        sql = ref["sql"]
        obs = payload["messages"][ref["observation_index"]]["content"]
        tree = sqlglot.parse_one(sql, read="mysql")
        kind = (
            "schema_discovery"
            if isinstance(tree, (exp.Show, exp.Describe))
            else "evidence_query"
        )
        _, status, _, _ = classify_result(kind, obs)
        attempt = {
            **ref,
            "tree": tree,
            "observation": obs,
            "status": status,
            "role": guides[seq]["role"],
        }
        attempts[seq] = attempt
        vals = [
            v.this
            for v in tree.find_all(exp.Literal)
            if not v.find_ancestor(exp.Limit, exp.Offset)
        ]
        protected.extend(vals)
        if status != "rows_returned":
            attempt["excluded"] = status
            continue
        try:
            t, b = normalize_sql(
                sql, schema
            )  # Preserve the original projection; model output hints are not schema authority.
            templates[t["id"]] = t
            attempt.update(template_id=t["id"], bindings=b, input_specs=t["inputs"])
            field_names = {f for fs in t["schema_fields"].values() for f in fs}
            invalid_outputs = sorted(
                set(requested[seq]) - field_names - set(t["outputs"])
            )
            if invalid_outputs:
                quality.append(
                    {
                        "sequence": seq,
                        "issue": "model_output_fields_not_in_source",
                        "fields": invalid_outputs,
                    }
                )
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            attempt["excluded"] = str(exc)
    for snippet in review["snippets"]:
        for link in snippet["links"]:
            if (
                link["type"] == "dataflow"
                and attempts[link["from"]]["status"] == "schema_result"
            ):
                quality.append(
                    {
                        "issue": "schema_dependency_mislabeled_as_dataflow",
                        "from": link["from"],
                        "to": link["to"],
                    }
                )
                link["type"] = "control"

    def make_candidate(name, intent, trigger, seqs, links, kind, caution):
        steps = []
        for seq in seqs:
            a = attempts[seq]
            if "template_id" not in a:
                return None
            t = templates[a["template_id"]]
            steps.append(
                {
                    "id": "s" + str(len(steps) + 1),
                    "template_id": t["id"],
                    "planner_guide": safe_text(guides[seq]["purpose"], protected),
                    "outputs": {
                        field: output_contract(
                            next(iter(t["schema_fields"]), ""), field, field
                        )
                        for field in t["outputs"]
                    },
                    "source_sequence": seq,
                }
            )
        # Dedup by executable structure + typed relations, not free-form model titles.
        remap = {seq: "s" + str(i + 1) for i, seq in enumerate(seqs)}
        edges = [
            {
                "from": remap[l["from"]],
                "to": remap[l["to"]],
                "from_column": l["from_column"],
                "to_field": l["to_field"],
                "transform": l["transform"],
                **{
                    k: l[k]
                    for k in ("json_path", "to_parameter", "to_operator")
                    if k in l
                },
            }
            for l in links
            if l.get("verified")
        ]
        edges = list(
            {json.dumps(edge, sort_keys=True): edge for edge in edges}.values()
        )
        key = {
            "kind": kind,
            "templates": [s["template_id"] for s in steps],
            "edges": sorted(edges, key=lambda e: json.dumps(e, sort_keys=True)),
        }
        ident = "proc_" + digest(key)
        for step in steps:
            step.pop("source_sequence")
        requirements = []
        for step in steps:
            template = templates[step["template_id"]]
            derived = {
                key
                for e in edges
                if e["to"] == step["id"]
                for key, spec in template["inputs"].items()
                if spec["field"] == e["to_field"]
                and (not e.get("to_parameter") or e["to_parameter"] == key)
            }
            for key, spec in template["inputs"].items():
                field = spec["field"]
                if key in derived:
                    continue
                table = next(
                    (t for t, fs in template["schema_fields"].items() if field in fs),
                    "",
                )
                semantic = output_contract(table, field, field)
                requirements.append(
                    {
                        "step": step["id"],
                        "parameter": key,
                        "table": table,
                        "field": field,
                        "concept": semantic["entity_concept"]
                        or field
                        or "Investigation_Scope",
                        "kind": semantic["entity_kind"] or "physical_field_or_scope",
                    }
                )
        if requirements:
            primary = next(
                (
                    r
                    for r in requirements
                    if r["concept"] not in {"Event_Time", "Login_Time"}
                ),
                requirements[0],
            )
            trigger = {"concept": primary["concept"], "kind": primary["kind"]}
        else:
            trigger = {"concept": "Investigation_Scope", "kind": "discovery"}
        return {
            "id": ident,
            "kind": kind,
            "name": safe_text(name, protected),
            "intent": safe_text(intent, protected),
            "trigger_entity": trigger,
            "required_inputs": requirements,
            "steps": steps,
            "bindings": edges,
            "caution": [safe_text(c, protected) for c in caution],
            "source": {
                "episode_id": payload["episode_id"],
                "trajectory_index": payload["index"],
                "incident": payload["incident"],
                "sequences": seqs,
            },
            "support_bindings": {str(seq): attempts[seq]["bindings"] for seq in seqs},
        }

    # Every observed successful query contributes an atomic candidate, including those
    # the model did not explicitly package as a snippet. Unsupported cases are ledgered.
    for seq, a in attempts.items():
        if "template_id" not in a:
            continue
        t = templates[a["template_id"]]
        first = next(iter(t["inputs"].values()), {})
        trigger = {
            "concept": first.get("field") or "Investigation_Scope",
            "kind": "physical_field_or_scope",
        }
        p = make_candidate(
            guides[seq]["purpose"],
            guides[seq]["purpose"],
            trigger,
            [seq],
            [],
            "atomic",
            [
                "仅有原轨迹查询返回证据；仍需核对当前事件、主机和时间范围。",
                "空结果不能证明实体不存在；达到行数上限须标记结果不完整。",
                *dict.fromkeys(
                    c
                    for snippet in review["snippets"]
                    if seq in snippet["sequences"]
                    for c in snippet["caution"]
                ),
            ],
        )
        proposal.append(p)
    lessons = []
    all_edges = []
    for si, s in enumerate(review["snippets"]):
        verified = [verify_link(link, attempts, schema) for link in s["links"]]
        quality.extend(
            {"proposal": si, "link": v}
            for v in verified
            if v["type"] == "dataflow" and not v["verified"]
        )
        # Build only connected successful value-flow components. Recovery/control is
        # separately preserved; failed SQL is never made an executable prerequisite.
        edges = [v for v in verified if v["verified"]]
        all_edges.extend(edges)
        components = []
        for edge in edges:
            touching = [c for c in components if edge["from"] in c or edge["to"] in c]
            merged = {edge["from"], edge["to"]}
            for c in touching:
                merged |= c
                components.remove(c)
            components.append(merged)
        for component in components:
            seqs = sorted(component)
            links = [
                v for v in edges if v["from"] in component and v["to"] in component
            ]
            p = make_candidate(
                s["name"],
                s["intent"],
                s["trigger_entity"],
                seqs,
                links,
                "evidence_chain",
                s["caution"],
            )
            if p:
                proposal.append(p)
        if any(v["type"] != "dataflow" for v in verified):
            lessons.append(
                {
                    "name": safe_text(s["name"], protected),
                    "sequences": s["sequences"],
                    "steps": [
                        {
                            "sequence": g["sequence"],
                            "guide": safe_text(g["guide"], protected),
                            "observed_status": attempts[g["sequence"]]["status"],
                        }
                        for g in s["step_guides"]
                    ],
                    "links": [
                        {
                            k: v
                            for k, v in l.items()
                            if k not in {"value", "source_quote"}
                        }
                        for l in s["links"]
                    ],
                    "policy": "reference_only; do not repeat failed SQL or infer absence from empty results",
                    "source": {
                        "episode_id": payload["episode_id"],
                        "trajectory_index": payload["index"],
                    },
                }
            )
    # Model snippets can split A→B→C and C→D. Preserve their smaller reusable pieces,
    # and also reconstruct the complete evidenced DAG across the whole trajectory.
    components = []
    for edge in all_edges:
        touching = [c for c in components if edge["from"] in c or edge["to"] in c]
        merged = {edge["from"], edge["to"]}
        for c in touching:
            merged |= c
            components.remove(c)
        components.append(merged)
    for component in components:
        if len(component) < 3:
            continue
        seqs = sorted(component)
        links = [
            v for v in all_edges if v["from"] in component and v["to"] in component
        ]
        cautions = list(
            dict.fromkeys(
                c
                for s in review["snippets"]
                if set(s["sequences"]) & component
                for c in s["caution"]
            )
        )
        p = make_candidate(
            "连续证据关联：" + guides[seqs[-1]]["purpose"],
            guides[seqs[-1]]["purpose"],
            {},
            seqs,
            links,
            "evidence_chain",
            cautions,
        )
        if p:
            proposal.append(p)
    ledger = {
        "index": payload["index"],
        "episode_id": payload["episode_id"],
        "incident": payload["incident"],
        "message_count": len(payload["messages"]),
        "sql_attempts": len(attempts),
        "summary": review["episode_summary"],
        "attempts": [
            {
                "sequence": seq,
                "status": a["status"],
                "role": a["role"],
                "template_id": a.get("template_id"),
                "exclusion": a.get("excluded"),
                "purpose": guides[seq]["purpose"],
                "finding": guides[seq]["finding"],
                "next_reason": guides[seq]["next_reason"],
            }
            for seq, a in attempts.items()
        ],
        "model_proposals": len(review["snippets"]),
        "final_assessment": review["final_assessment"],
        "quality_findings": quality,
        "recovery": review["recovery"],
        "control_recipes": lessons,
    }
    return proposal, templates, ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-limit", type=int, default=10)
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Revalidate all published IDs with actual DAG binding",
    )
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    schemas = schema_snapshot(OUTPUT)
    proposals = {}
    templates = {}
    ledgers = []
    for payload in inputs():
        path = OUTPUT / f"{payload['index']:03d}" / "review.json"
        if not path.exists():
            continue
        result = json.loads(path.read_text())
        validate(result["review"], payload)
        if result["source_sha256"] != sha(json.dumps(payload, ensure_ascii=False)):
            raise ValueError("review source hash differs")
        review, overrides = apply_review_notes(
            result["review"], payload["index"], OUTPUT
        )
        ps, ts, ledger = compile_episode(payload, review, schemas[payload["incident"]])
        ledger["reviewer_overrides"] = overrides
        templates.update(ts)
        ledgers.append(ledger)
        save(OUTPUT / f"{payload['index']:03d}" / "audit.json", ledger)
        for p in ps:
            entry = proposals.setdefault(
                p["id"],
                {k: v for k, v in p.items() if k not in {"source", "support_bindings"}},
            )
            if p["source"] not in entry.setdefault("sources", []):
                entry["sources"].append(p["source"])
                entry.setdefault("replay_inputs", []).append(p["support_bindings"])
            entry["caution"] = list(dict.fromkeys([*entry["caution"], *p["caution"]]))
            variant = {
                "name": p["name"],
                "intent": p["intent"],
                "step_guides": [
                    {"step": s["id"], "guide": s["planner_guide"]} for s in p["steps"]
                ],
                "caution": p["caution"],
            }
            variant["id"] = "intent_" + digest(variant)
            if variant not in entry.setdefault("intent_variants", []):
                entry["intent_variants"].append(variant)
            entry.setdefault("intent_aliases", [])
            if p["intent"] not in entry["intent_aliases"]:
                entry["intent_aliases"].append(p["intent"])
    published_path = OUTPUT / "published.json"
    published = (
        json.loads(published_path.read_text())
        if published_path.exists()
        else {"ids": [], "replays": {}}
    )
    # Changed canonicalization invalidates prior replay results; archive rather than trust them.
    stale = [ident for ident in published["ids"] if ident not in proposals]
    if stale:
        save(OUTPUT / ("superseded_" + digest(published["ids"]) + ".json"), published)
        published["ids"] = [ident for ident in published["ids"] if ident in proposals]
    published["failed_ids"] = list(
        dict.fromkeys(
            ident for ident in published.get("failed_ids", []) if ident in proposals
        )
    )
    selected = (
        published["ids"][:]
        if args.recheck
        else [
            ident
            for ident in proposals
            if ident not in published["ids"]
            and ident not in published.get("failed_ids", [])
        ]
    )
    if not args.all and not args.recheck:
        selected = selected[: min(args.new_limit, 10 - len(published["ids"]) % 10)]
    config = dotenv_values(ROOT / "docker/excytin-mysql/.env")
    connections = {}
    new = []
    try:
        for ident in selected:
            p = proposals[ident]
            source = p["sources"][0]
            incident = source["incident"]
            schema = schemas[incident]
            checks = []
            if incident not in connections:
                db = MySQLQueryExecutor(
                    incident, password=config["MYSQL_ROOT_PASSWORD"]
                )
                connections[incident] = db
                db.connection.rollback()
                db.connection.start_transaction(readonly=True)
            db = connections[incident]
            local = {
                "format_version": "trajectory-snippets-2",
                "snippets": [{**p, "status": "candidate"}],
                "templates": {
                    step["template_id"]: templates[step["template_id"]]
                    for step in p["steps"]
                },
            }
            local["catalog_id"] = digest(local)
            try:
                store = TrajectorySnippetStore(local)
            except ValueError as exc:
                published["replays"][ident] = {
                    "source": source,
                    "passed": False,
                    "error": str(exc),
                }
                if ident in published["ids"]:
                    published["ids"].remove(ident)
                published.setdefault("failed_ids", []).append(ident)
                continue
            observations = {}
            for step, seq in zip(p["steps"], source["sequences"]):
                t = templates[step["template_id"]]
                b = p["replay_inputs"][0][str(seq)]
                check = {"step": step["id"], "template_id": t["id"], "success": False}
                try:
                    external = dict(b)
                    selections = {}
                    for edge in p["bindings"]:
                        if edge["to"] != step["id"]:
                            continue
                        keys = [
                            k
                            for k, spec in t["inputs"].items()
                            if spec["field"] == edge["to_field"]
                            and (
                                not edge.get("to_parameter")
                                or k == edge["to_parameter"]
                            )
                        ]
                        if len(keys) != 1:
                            raise ValueError("ambiguous_target_parameter")
                        value = external.pop(keys[0], b[keys[0]])
                        selections[edge["from"] + "." + edge["from_column"]] = (
                            value if isinstance(value, list) else [value]
                        )
                    prepared = store.prepare_step(
                        ident,
                        step["id"],
                        schema=schema,
                        external_inputs=external,
                        observations=observations,
                        selections=selections,
                        allow_candidates=True,
                    )
                    db.cursor.execute("EXPLAIN " + prepared["sql"], prepared["params"])
                    db.cursor.fetchall()
                    db.cursor.execute(prepared["sql"], prepared["params"])
                    rows = db.cursor.fetchall()
                    columns = [col[0] for col in db.cursor.description]
                    observations[step["id"]] = {
                        "success": True,
                        "rows": [dict(zip(columns, row)) for row in rows],
                        "truncated": len(rows) == t["row_cap"],
                    }
                    check.update(
                        success=bool(rows),
                        row_count=len(rows),
                        possibly_truncated=len(rows) == t["row_cap"],
                        binding_verified=True,
                    )
                except Exception as exc:
                    check.update(
                        error_type=type(exc).__name__,
                        error_code=getattr(exc, "errno", None),
                        validation_reason=str(exc)
                        if isinstance(exc, ValueError)
                        else "database_error",
                    )
                checks.append(check)
            published["replays"][ident] = {
                "source": source,
                "checks": checks,
                "passed": all(c["success"] for c in checks),
            }
            if all(c["success"] for c in checks):
                if ident not in published["ids"]:
                    published["ids"].append(ident)
                    new.append(ident)
            else:
                if ident in published["ids"]:
                    published["ids"].remove(ident)
                if ident not in published.setdefault("failed_ids", []):
                    published["failed_ids"].append(ident)
    finally:
        for db in connections.values():
            db.connection.rollback()
            db.close()
    save(published_path, published)
    # Rehydrate all prior IDs against current reviews: later support merges into them.
    snippets = []
    for ident in published["ids"]:
        p = proposals[ident]
        support = p["sources"]
        entry = {k: v for k, v in p.items() if k not in {"sources", "replay_inputs"}}
        entry["support"] = {
            "episodes": len({s["episode_id"] for s in support}),
            "incidents": len({s["incident"] for s in support}),
        }
        entry["status"] = (
            "candidate"  # Runtime integration/promotion is a separate decision.
        )
        entry["validation"] = {
            "level": "schema_and_readonly_mysql_replay",
            "chain_binding": all(
                c.get("binding_verified")
                for c in published["replays"][ident].get("checks", [])
            ),
            "validated_supports": 1,
            "not_an_answer_correctness_guarantee": True,
        }
        snippets.append(entry)
    catalog = {
        "format_version": "trajectory-snippets-2",
        "environment_id": "excytin-azure-mysql",
        "snippets": snippets,
        "templates": {
            tid: templates[tid]
            for s in snippets
            for tid in [step["template_id"] for step in s["steps"]]
        },
    }
    catalog["catalog_id"] = digest(catalog)
    save(OUTPUT / "catalog.json", catalog)
    coverage = {
        "reviewed": len(ledgers),
        "expected": 237,
        "attempts_reviewed": sum(l["sql_attempts"] for l in ledgers),
        "unique_proposals": len(proposals),
        "validated_snippets": len(snippets),
        "new_validated": len(new),
        "multi_step": sum(s["kind"] == "evidence_chain" for s in snippets),
        "rejected": len(
            set(published.get("failed_ids", []))
            & (proposals.keys() - set(published["ids"]))
        ),
        "pending": len(
            proposals.keys()
            - set(published["ids"])
            - set(published.get("failed_ids", []))
        ),
        "sql_statuses": dict(
            Counter(a["status"] for l in ledgers for a in l["attempts"])
        ),
        "quality_findings": sum(len(l["quality_findings"]) for l in ledgers),
        "source_support": {
            ident: proposals[ident]["sources"] for ident in published["ids"]
        },
    }
    save(OUTPUT / "coverage.json", coverage)
    print(
        json.dumps(
            {k: v for k, v in coverage.items() if k not in {"source_support"}},
            ensure_ascii=False,
        )
    )
    for ident in new:
        print(
            json.dumps(
                {
                    "id": ident,
                    "kind": proposals[ident]["kind"],
                    "name": proposals[ident]["name"],
                    "tables": sorted(
                        {
                            table
                            for s in proposals[ident]["steps"]
                            for table in templates[s["template_id"]]["schema_fields"]
                        }
                    ),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
