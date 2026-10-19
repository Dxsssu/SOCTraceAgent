"""Replay every normalized successful training query and every retained chain support."""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from scripts.build_reviewed_procedural_memory import (
    compile_episode,
    apply_review_notes,
    schema_snapshot,
)
from scripts.review_procedural_trajectories import inputs, OUTPUT, save, validate, sha
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.core import (
    compile_sql,
)
from src.memory.longterm_memory.procedural_memory.trajectory_snippets.store import (
    TrajectorySnippetStore,
)


def validate_all(output=OUTPUT, require_complete=True):
    schemas = schema_snapshot(output)
    catalog = TrajectorySnippetStore.load(output / "catalog.json")
    config = dotenv_values(ROOT / "docker/excytin-mysql/.env")
    connections, queries, chains, reviewed = {}, [], [], []
    try:
        for payload in inputs():
            path = output / f"{payload['index']:03d}" / "review.json"
            if not path.exists():
                continue
            result = json.loads(path.read_text())
            validate(result["review"], payload)
            assert result["source_sha256"] == sha(
                json.dumps(payload, ensure_ascii=False)
            )
            reviewed.append(payload["index"])
            review, _ = apply_review_notes(result["review"], payload["index"], output)
            incident = payload["incident"]
            schema = schemas[incident]
            proposals, templates, ledger = compile_episode(payload, review, schema)
            if incident not in connections:
                db = MySQLQueryExecutor(
                    incident, password=config["MYSQL_ROOT_PASSWORD"]
                )
                db.connection.rollback()
                db.connection.start_transaction(readonly=True)
                connections[incident] = db
            db = connections[incident]
            for proposal in proposals:
                if proposal["kind"] != "atomic":
                    continue
                seq = proposal["source"]["sequences"][0]
                template = templates[proposal["steps"][0]["template_id"]]
                record = {
                    "index": payload["index"],
                    "sequence": seq,
                    "template_id": template["id"],
                }
                try:
                    sql, params = compile_sql(
                        template, proposal["support_bindings"][str(seq)], schema
                    )
                    db.cursor.execute("EXPLAIN " + sql, params)
                    db.cursor.fetchall()
                    db.cursor.execute(sql, params)
                    rows = db.cursor.fetchall()
                    record.update(
                        passed=bool(rows),
                        row_count=len(rows),
                        row_cap=template["row_cap"],
                    )
                except Exception as exc:
                    record.update(
                        passed=False,
                        error_type=type(exc).__name__,
                        error_code=getattr(exc, "errno", None),
                        reason=str(exc)
                        if isinstance(exc, ValueError)
                        else "database_error",
                    )
                queries.append(record)
            seen_chains = set()
            for proposal in proposals:
                if (
                    proposal["kind"] != "evidence_chain"
                    or proposal["id"] not in catalog.snippets
                ):
                    continue
                support_key = (proposal["id"], tuple(proposal["source"]["sequences"]))
                if support_key in seen_chains:
                    continue
                seen_chains.add(support_key)
                observations, checks = {}, []
                for step, seq in zip(
                    proposal["steps"], proposal["source"]["sequences"]
                ):
                    template = templates[step["template_id"]]
                    bindings = proposal["support_bindings"][str(seq)]
                    record = {"step": step["id"], "sequence": seq, "passed": False}
                    try:
                        external, selections = dict(bindings), {}
                        for edge in proposal["bindings"]:
                            if edge["to"] != step["id"]:
                                continue
                            keys = [
                                k
                                for k, spec in template["inputs"].items()
                                if spec["field"] == edge["to_field"]
                                and (
                                    not edge.get("to_parameter")
                                    or k == edge["to_parameter"]
                                )
                            ]
                            if len(keys) != 1:
                                raise ValueError("ambiguous_target_parameter")
                            value = external.pop(keys[0], bindings[keys[0]])
                            selections[edge["from"] + "." + edge["from_column"]] = (
                                value if isinstance(value, list) else [value]
                            )
                        prepared = catalog.prepare_step(
                            proposal["id"],
                            step["id"],
                            schema=schema,
                            external_inputs=external,
                            observations=observations,
                            selections=selections,
                            allow_candidates=True,
                        )
                        db.cursor.execute(
                            "EXPLAIN " + prepared["sql"], prepared["params"]
                        )
                        db.cursor.fetchall()
                        db.cursor.execute(prepared["sql"], prepared["params"])
                        rows = db.cursor.fetchall()
                        columns = [col[0] for col in db.cursor.description]
                        observations[step["id"]] = {
                            "success": True,
                            "truncated": len(rows) == template["row_cap"],
                            "rows": [dict(zip(columns, row)) for row in rows],
                        }
                        record.update(
                            passed=bool(rows),
                            row_count=len(rows),
                            row_cap=template["row_cap"],
                        )
                    except Exception as exc:
                        record.update(
                            error_type=type(exc).__name__,
                            error_code=getattr(exc, "errno", None),
                            reason=str(exc)
                            if isinstance(exc, ValueError)
                            else "database_error",
                        )
                    checks.append(record)
                chains.append(
                    {
                        "index": payload["index"],
                        "snippet_id": proposal["id"],
                        "sequences": proposal["source"]["sequences"],
                        "checks": checks,
                        "passed": all(c["passed"] for c in checks),
                    }
                )
    finally:
        for db in connections.values():
            db.connection.rollback()
            db.close()
    report = {
        "catalog_id": catalog.document["catalog_id"],
        "train_only": True,
        "test_used": False,
        "reviewed_trajectories": len(reviewed),
        "missing_indices": sorted(set(range(1, 238)) - set(reviewed)),
        "query_supports": len(queries),
        "query_passed": sum(q["passed"] for q in queries),
        "chain_supports": len(chains),
        "chain_passed": sum(c["passed"] for c in chains),
        "chain_steps": sum(len(c["checks"]) for c in chains),
        "method": "EXPLAIN + SELECT in read-only transactions; each chain consumes actual earlier query rows; historical target values are explicit verified selections, not autonomous selection.",
        "queries": queries,
        "chains": chains,
    }
    save(output / "all_support_validation.json", report)
    if require_complete and len(reviewed) != 237:
        raise ValueError("incomplete_training_review")
    return {k: v for k, v in report.items() if k not in {"queries", "chains"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(validate_all(require_complete=not args.partial), ensure_ascii=False)
    )
