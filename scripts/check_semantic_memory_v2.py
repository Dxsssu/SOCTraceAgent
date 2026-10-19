"""Reproducible SM v2 examples; optional read-only MySQL smoke checks.

Run: uv run --with tiktoken python scripts/check_semantic_memory_v2.py
Add --mysql-port 3306 to verify the target schema and execute three lookups.
No LLM calls; table selection is explicit test input, not a Planner evaluation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.memory.longterm_memory.semantic_memory.v2 import (
    ConceptQuery as Q, SemanticMemoryV2, executor_context, render_view,
)
from src.memory.longterm_memory.semantic_memory.v2.build import SOURCE

CASES = [
    ("邮件头 ID → 发件人", [Q("邮件ID", "internet_message_id")], [Q("发件人", "header_from")], "EmailEvents"),
    ("平台邮件 ID → 附件 SHA256", [Q("邮件ID", "network_message_id")], [Q("文件哈希", "sha256")], "EmailAttachmentInfo"),
    ("文件 SHA256 → 所在目录", [Q("文件哈希", "sha256")], [Q("文件夹路径")], "DeviceFileEvents"),
    ("账户 UPN → 登录 IP", [Q("账户UPN")], [Q("登录IP")], "SigninLogs"),
    ("主机＋PID → 进程命令行", [Q("主机名"), Q("PID")], [Q("命令行")], "DeviceProcessEvents"),
    ("邮件 ID → 点击用户", [Q("邮件ID", "network_message_id")], [Q("点击用户")], "UrlClickEvents"),
    ("未区分类型的邮件 ID", [Q("邮件ID")], [Q("发件人")], None),
    ("邮件头 ID → 文件目录（缺少中间步骤）", [Q("邮件ID", "internet_message_id")], [Q("文件夹路径")], None),
    ("未知概念", [Q("量子邮件编号")], [Q("发件人")], None),
]


def run_examples(store, encode):
    output = []
    for title, known, wanted, selected in CASES:
        start = time.perf_counter()
        result = store.retrieve(known=known, wanted=wanted, limit=5)
        elapsed = (time.perf_counter() - start) * 1000
        view = render_view(result)
        # Conservative comparison: only full schemas for the same returned tables,
        # excluding the old global L1 catalog and any other prompt overhead.
        baseline = "\n".join((SOURCE / "l2" / f"{c['table']}.yaml").read_text(encoding="utf-8") for c in result["candidates"])
        item = {"title": title, "known": [q.__dict__ for q in known], "wanted": [q.__dict__ for q in wanted],
                "lookup_ms": round(elapsed, 3), "result": result, "planner_view": view,
                "view_chars": len(view), "same_tables_full_schema_chars": len(baseline),
                "view_tokens": len(encode(view)) if encode else None,
                "same_tables_full_schema_tokens": len(encode(baseline)) if encode else None}
        if selected:
            context = executor_context(result, selected)
            item["selected_table"] = selected
            item["executor_context"] = context
            item["selected_columns_including_time"] = sorted({r["field"] for r in context["fields"]} | ({context["time_field"]["field"]} if context["time_field"] else set()))
            item["check_passed"] = True
        elif title == "未区分类型的邮件 ID":
            item["check_passed"] = bool(result["ambiguities"])
        elif title == "未知概念":
            item["check_passed"] = bool(result["unresolved"]) and not result["candidates"]
        else:
            item["check_passed"] = bool(result["candidates"]) and all(c["capability"] == "partial" for c in result["candidates"])
        output.append(item)
    return output


def quote_identifier(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("Unexpected SQL identifier")
    return f"`{name}`"


def run_mysql(port):
    import mysql.connector
    from dotenv import dotenv_values
    config = dotenv_values(ROOT / "docker/excytin-mysql/.env")
    connection = mysql.connector.connect(host="127.0.0.1", port=port, user="root", password=config["MYSQL_ROOT_PASSWORD"], database="env_monitor_db", connection_timeout=10)
    try:
        connection.start_transaction(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()")
            schema = {}
            for table, column, dtype in cursor.fetchall():
                schema.setdefault(table, {})[column] = dtype
            memory = SemanticMemoryV2.load(schema=schema)
            checks = []
            # Extract a test input from actual data; these are schema/query smoke
            # tests, not hidden benchmark questions or scored investigations.
            for index in (0, 1, 3):
                title, known, wanted, table = CASES[index]
                result = memory.retrieve(known=known, wanted=wanted)
                context = executor_context(result, table)
                key = next(r["field"] for r in context["fields"] if r["use"] == "known")
                target = next(r["field"] for r in context["fields"] if r["use"] == "wanted")
                qt, qk, qv = map(quote_identifier, (table, key, target))
                cursor.execute(f"SELECT /*+ MAX_EXECUTION_TIME(10000) */ {qk} FROM {qt} WHERE {qk} IS NOT NULL AND {qk} <> '' AND {qv} IS NOT NULL AND {qv} <> '' LIMIT 1")
                seed = cursor.fetchone()
                if seed is None:
                    checks.append({"title": title, "table": table, "status": "no_test_input"})
                    continue
                sql = f"SELECT /*+ MAX_EXECUTION_TIME(10000) */ {qv} FROM {qt} WHERE {qk} = %s LIMIT 3"
                cursor.execute(sql, (seed[0],))
                rows = cursor.fetchall()
                checks.append({"title": title, "table": table, "filter_field": key, "output_field": target,
                               "sql": sql, "status": "passed" if any(r[0] for r in rows) else "failed", "row_count": len(rows), "schema_scope": result["schema_scope"]})
            return checks
    finally:
        connection.rollback()
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/semantic_memory_v2")
    parser.add_argument("--mysql-port", type=int)
    args = parser.parse_args()
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        encode = lambda text: encoding.encode(text, disallowed_special=())
    except ImportError:
        encode = None
    cases = run_examples(SemanticMemoryV2.load(), encode)
    mysql = run_mysql(args.mysql_port) if args.mysql_port else []
    report = {"evaluation": "deterministic retrieval and optional MySQL smoke checks; no LLM/Planner accuracy measurement",
              "tokenizer": "cl100k_base (comparison only; not Fancybag model billing)" if encode else None,
              "baseline": "Full legacy L2 for exactly the returned candidate tables, no global L1 or role prompts",
              "cases": cases, "mysql": mysql}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Semantic Memory v2 示例结果", "", report["evaluation"], "", f"Tokenizer: {report['tokenizer']}", "", "| 示例 | 候选表数 | 返回 token | 同候选表完整 schema token | 检查 |", "|---|---:|---:|---:|---|"]
    for case in cases:
        lines.append(f"| {case['title']} | {case['result']['total']} | {case['view_tokens']} | {case['same_tables_full_schema_tokens']} | {case['check_passed']} |")
    for case in cases:
        lines.extend(["", f"## {case['title']}", "", "```text", case["planner_view"], "```"])
    lines.extend(["", "## MySQL smoke checks", "", "```json", json.dumps(mysql, ensure_ascii=False, indent=2), "```"])
    (args.output / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"passed": sum(c["check_passed"] for c in cases), "cases": len(cases), "mysql": mysql,
                      "metrics": [{k: c[k] for k in ("title", "view_tokens", "same_tables_full_schema_tokens")} for c in cases]}, ensure_ascii=False))
    if not all(c["check_passed"] for c in cases) or any(c["status"] != "passed" for c in mysql):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
