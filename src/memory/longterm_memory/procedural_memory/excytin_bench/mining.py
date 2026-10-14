"""Mine reusable partial operations; keep provenance outside Agent memories."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

import sqlglot
from sqlglot import exp

from src.memory.longterm_memory.episodic_memory.excytin_bench.training_history import (
    make_episode_id,
)
from src.memory.longterm_memory.episodic_memory.excytin_bench.sql_parser import (
    extract_execute_sql,
)

TABLE_GOALS = {
    "AlertInfo": "定位告警及告警 ID，核对名称、类别和攻击技术",
    "AlertEvidence": "确认告警涉及的设备、账号、进程、文件或网络实体",
    "SecurityAlert": "核查告警实体、扩展属性、人工调查及处置信息",
    "SecurityIncident": "核查安全事件的标题、描述和事件信息",
    "DeviceProcessEvents": "调查进程命令行、进程标识、父进程和运行账户",
    "DeviceNetworkEvents": "调查设备网络连接的远端地址、端口和发起进程",
    "DeviceFileEvents": "调查文件活动及其发起进程",
    "DeviceInfo": "核对设备标识、设备名称和设备属性",
    "EmailEvents": "核查邮件发件人、收件人、威胁判定及投递情况",
    "EmailUrlInfo": "定位邮件中的 URL 和对应消息标识",
    "EmailPostDeliveryEvents": "核查邮件投递后的处置活动",
    "UrlClickEvents": "调查 URL 点击、点击账户和来源地址",
    "SigninLogs": "调查账号登录、登录来源地址及登录结果",
    "AADRiskyUsers": "核查风险账号",
    "AADUserRiskEvents": "核查账号风险事件",
    "CloudAppEvents": "调查云应用中的账户操作",
    "IdentityLogonEvents": "调查身份登录活动",
    "IdentityDirectoryEvents": "调查目录和身份变更活动",
    "IdentityQueryEvents": "调查身份查询活动",
    "OfficeActivity": "调查 Office 服务中的账户活动",
    "AADNonInteractiveUserSignInLogs": "调查非交互式登录活动",
}


def value_filters(tree):
    where = tree.args.get("where")
    pending = [where.this] if where else []
    result = []
    while pending:
        node = pending.pop()
        if isinstance(node, exp.Paren):
            pending.append(node.this)
        elif isinstance(node, exp.And):
            pending.extend([node.left, node.right])
        elif isinstance(node, exp.EQ):
            for col, val in [(node.left, node.right), (node.right, node.left)]:
                if isinstance(col, exp.Column) and isinstance(val, exp.Literal):
                    result.append((col.name, str(val.this)))
        elif isinstance(node, exp.In) and isinstance(node.this, exp.Column):
            result.extend(
                (node.this.name, str(v.this))
                for v in node.expressions
                if isinstance(v, exp.Literal)
            )
    return result


def observed_value(value, observation):
    return len(value) >= 6 and (
        repr(value) in observation or json.dumps(value) in observation
    )


def records_from_training(episodes, mapped, semantic):
    by_id = {e["episode_id"]: e for e in episodes}
    tables = {t["name"].lower(): t for t in semantic["tables"]}
    records = []
    for trajectory, source, _ in mapped:
        episode = by_id[make_episode_id(source)]
        messages = trajectory["value"]["messages"]
        index = 0
        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                continue
            sql = extract_execute_sql(msg.get("content", ""))
            if sql is None:
                continue
            a = episode["attempts"][index]
            index += 1
            if i + 1 >= len(messages) or messages[i + 1].get("role") != "user":
                raise ValueError("Missing observation")
            try:
                tree = sqlglot.parse_one(sql, read="mysql")
            except sqlglot.errors.ParseError:
                tree = None
            refs = list(tree.find_all(exp.Table)) if tree is not None else []
            table = tables.get(refs[0].name.lower()) if len(refs) == 1 else None
            single = (
                isinstance(tree, exp.Select)
                and table is not None
                and tree.find(exp.Subquery) is None
            )
            fields = {f["name"].lower(): f for f in table["fields"]} if table else {}
            where = tree.args.get("where") if single else None
            filter_names = (
                sorted({c.name for c in where.find_all(exp.Column)}) if where else []
            )
            valid = single and all(n.lower() in fields for n in filter_names)
            projected = (
                {p.name.lower() for p in tree.expressions if isinstance(p, exp.Column)}
                if single
                else set()
            )
            if single:
                projected.update(
                    p.this.name.lower()
                    for p in tree.expressions
                    if isinstance(p, exp.Alias) and isinstance(p.this, exp.Column)
                )
            star = single and any(
                isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star)
                for p in tree.expressions
            )
            outputs = [
                f["field_id"]
                for f in fields.values()
                if star or f["name"].lower() in projected
            ]
            records.append(
                {
                    "episode_id": episode["episode_id"],
                    "incident": episode["source_incident"],
                    "attempt_id": a["attempt_id"],
                    "sequence": a["sequence_no"],
                    "status": a["result_status"],
                    "error_type": a.get("error_type_id", "").split(":")[-1],
                    "table": table["name"] if table else None,
                    "fields": filter_names,
                    "output_fields": outputs,
                    "tree": tree,
                    "sql": sql,
                    "valid": valid,
                    "observation": str(messages[i + 1].get("content", "")),
                }
            )
        if index != len(episode["attempts"]):
            raise ValueError("Training attempt alignment differs")
    return records


def evidence(*records):
    return {
        "episode_id": records[0]["episode_id"],
        "incident": records[0]["incident"],
        "attempt_ids": [r["attempt_id"] for r in records],
    }


def mine(records, semantic):
    """Every record is visited; one-off patterns remain in audit, not active memory."""
    proposals = {}
    by_episode = defaultdict(list)
    for r in records:
        by_episode[r["episode_id"]].append(r)
    tables = {t["name"]: t for t in semantic["tables"]}
    field_keys = {
        f: k
        for k in semantic["join_keys"]
        for f in k["field_ids"]
        if k["name"] != "tenant_id"
    }

    def add(signature, procedure, ev, family):
        item = proposals.setdefault(
            signature, {"procedure": procedure, "family": family, "evidence": []}
        )
        if ev not in item["evidence"]:
            item["evidence"].append(ev)

    def proc(name, goal, slots, ops):
        return {
            "name": name,
            "status": "candidate",
            "goal": goal,
            "input_slots": slots,
            "operations": ops,
        }

    for r in records:
        if r["status"] != "rows_returned" or not r["valid"]:
            continue
        table = r["table"]
        fields = r["fields"]
        field_text = "、".join(fields)
        goal = TABLE_GOALS.get(table, f"调查 {table} 中的相关记录")
        if fields:
            name = f"{table}：按 {field_text} 查证"
            slots = {
                f: f"当前调查中用于 {f} 的已知值或检索线索；匹配方式由实际任务确定。"
                for f in fields
            }
            ops = [
                f"在 {table} 中核对 {field_text} 的含义和当前可用字段，已有 schema 时直接复用。",
                f"用当前线索对 {field_text} 设定筛选；明确等值、模式匹配及与/或关系，避免把多个备选值错误地同时要求满足。",
                "只读取当前问题所需的字段并限制返回量；核对结果与事件范围，记录证据引用。",
            ]
            if table == "DeviceProcessEvents":
                ops.append(
                    "区分目标进程字段与 InitiatingProcess 发起进程字段；PID 与设备和创建时间联合核对。"
                )
            if table == "SecurityAlert":
                ops.append(
                    "若所需信息在 Entities 或 ExtendedProperties 中，检查返回字段的结构再定位目标属性，不假定所有实体在同一记录。"
                )
            add(
                ("lookup", table, tuple(fields)),
                proc(
                    name,
                    f"已知 {field_text} 相关线索时，{goal}；已完成的字段确认步骤可跳过。",
                    slots,
                    ops,
                ),
                evidence(r),
                "lookup",
            )
        else:
            add(
                ("sample", table),
                proc(
                    f"{table}：缺少筛选线索时检查少量样本",
                    f"尚不清楚 {table} 的记录形态或可用线索时检查少量样本，已有定位键时优先直接查证。",
                    {"table": "当前待调查的日志表。"},
                    [
                        f"确认 {table} 存在及其字段。",
                        "只取少量样本观察可用字段与值形态，不作无界全表扫描。",
                        "从当前样本提取可核实的定位线索，再转入有条件查询；样本不能代表全量。",
                    ],
                ),
                evidence(r),
                "sample",
            )
    for rs in by_episode.values():
        # Schema exploration followed by a successful query of the same table.
        for i, r in enumerate(rs):
            if r["status"] == "schema_result" and r["table"]:
                after = next(
                    (
                        q
                        for q in rs[i + 1 : i + 4]
                        if q["table"] == r["table"]
                        and q["status"] == "rows_returned"
                        and q["valid"]
                    ),
                    None,
                )
                if after:
                    add(
                        ("schema",),
                        proc(
                            "陌生日志表：先确认字段再查询",
                            "不知道表字段、字段含义或关联键时，为当前查询取得正确 schema；已有可信 schema 时跳过。",
                            {
                                "table": "当前需要查询的表名。",
                                "question": "当前要查明的事实。",
                            },
                            [
                                "检查当前环境可用表与目标表结构。",
                                "用 Semantic Memory 核对与问题相关的字段、类型和关联键，不编造字段。",
                                "将当前问题映射到真实字段后执行有范围限制的查询。",
                            ],
                        ),
                        evidence(r, after),
                        "schema",
                    )
            if r["status"] not in {"error", "empty_result"}:
                continue
            after = next(
                (
                    q
                    for q in rs[i + 1 : i + 6]
                    if q["status"] == "rows_returned"
                    and q["valid"]
                    and (q["table"] == r["table"] or r["status"] == "error")
                ),
                None,
            )
            if not after or after["sql"] == r["sql"]:
                continue
            if r["status"] == "error":
                kind = r["error_type"] or "sql_error"
                name = {
                    "unknown_column": "字段不存在：核对 schema 后修正查询",
                    "missing_table": "表不存在：确认环境与替代表",
                    "syntax_error": "SQL 语法错误：按当前方言修正",
                }.get(kind, "SQL 报错：检查结构并修正查询")
                ops = [
                    "读取本次数据库错误，定位出错的表、字段或语法。",
                    "检查当前环境表结构和 Semantic Memory；必要时调整实体所在日志表，不能凭名称猜字段。",
                    "保留当前调查目标与已验证约束，用修正后的查询重试，再核对结果。",
                ]
                add(
                    ("repair", kind),
                    proc(
                        name,
                        "当前查询报错时修复执行方式，避免重复原错误查询；修复后有记录不等于已回答问题。",
                        {
                            "error": "本次数据库错误。",
                            "query_constraints": "原调查目标、已知实体和范围。",
                        },
                        ops,
                    ),
                    evidence(r, after),
                    "error_repair",
                )
            elif r["valid"]:
                before = set(r["fields"])
                after_fields = set(after["fields"])
                kind = (
                    "relax"
                    if after_fields < before
                    else ("remap" if before != after_fields else "retry")
                )
                name = {
                    "relax": "空结果：逐项核查过度约束",
                    "remap": "空结果：检查筛选字段映射",
                    "retry": "空结果：核对值与匹配方式",
                }[kind]
                ops = [
                    "确认查询执行成功但没有记录，不直接把空结果作为不存在的证据。",
                    "对照当前线索核查字段、值、大小写、匹配方式和时间范围。",
                    "仅在证据支持下逐项调整条件并限定返回量；保留原约束用于复核候选，不能为得到结果任意放宽。",
                    "重新查询并检查候选是否仍回答原问题。",
                ]
                add(
                    ("empty", kind),
                    proc(
                        name,
                        "针对当前同表查询的空结果排查过滤条件，修正后重新验证相关性。",
                        {
                            "query_constraints": "原始筛选字段、值及作用域。",
                            "evidence": "允许修正条件的当前调查证据。",
                        },
                        ops,
                    ),
                    evidence(r, after),
                    "empty_repair",
                )
        # Search all earlier evidence queries, not only adjacent successful queries.
        successful = [r for r in rs if r["status"] == "rows_returned" and r["valid"]]
        edges = []
        for i, right in enumerate(successful):
            seen = set()
            for left in reversed(successful[:i]):
                if left["table"] == right["table"]:
                    continue
                target_fields = {
                    f["name"].lower(): f for f in tables[right["table"]]["fields"]
                }
                for column, value in value_filters(right["tree"]):
                    target = target_fields.get(column.lower())
                    key = field_keys.get(target["field_id"]) if target else None
                    if not key or not observed_value(value, left["observation"]):
                        continue
                    source_fields = sorted(
                        set(left["output_fields"]) & set(key["field_ids"])
                    )
                    if not source_fields:
                        continue
                    signature = (
                        left["table"],
                        right["table"],
                        key["name"],
                        target["name"],
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    edge = {
                        "signature": signature,
                        "left": left,
                        "right": right,
                        "source_fields": source_fields,
                        "scope": key["join_scope"],
                    }
                    edges.append(edge)
                    source, target_table, key_name, target_field = signature
                    ops = [
                        f"若尚无 {key_name}，在 {source} 定位相关记录并从 {', '.join(f.split(':')[-1] for f in source_fields)} 提取该键，保留本次证据引用；已有已核实键则跳过。",
                        f"核实关联键来自当前调查，作用域为 {key['join_scope']}；结合设备、租户、时间消歧。",
                        f"用该键过滤 {target_table}.{target_field}，只查询当前问题需要的字段。",
                        "检查关联记录是否满足原任务；空结果核对键和范围，不直接推断不存在。",
                    ]
                    if (
                        key_name in {"device_name", "ip_address"}
                        or "process" in key_name
                    ):
                        ops.append(
                            "相同设备/IP/PID 只确定候选；归因需额外核对设备、进程和时间。"
                        )
                    add(
                        ("pivot",) + signature,
                        proc(
                            f"{source} → {target_table} ({key_name})",
                            f"已知 {source} 的记录或 {key_name} 时，{TABLE_GOALS.get(target_table, '取得关联记录')}；支持从已有键直接继续。",
                            {
                                "task_constraints": "当前调查目标和范围。",
                                key_name: "本次调查的关联值；未知时先定位源记录。",
                            },
                            ops,
                        ),
                        evidence(left, right),
                        "pivot",
                    )
        # Three-table chains must share the actual middle query in one episode.
        for first in edges:
            for second in edges:
                if first["right"]["attempt_id"] != second["left"]["attempt_id"]:
                    continue
                a, b = first["signature"], second["signature"]
                if a[0] == b[1]:
                    continue
                slots = {
                    a[2]: "本次调查取得的首个关联键；缺失时从源记录取得。",
                    b[2]: "中间查询结果提供的关联键；已有可信证据可直接继续。",
                    "task_constraints": "当前问题和事件范围。",
                }
                ops = [
                    f"按当前线索定位 {a[0]}，取得 {a[2]}；这一步已完成时复用已有证据。",
                    f"用 {a[2]} 过滤 {a[1]}.{a[3]}，检查结果并从中提取 {b[2]}。",
                    f"用已核实的 {b[2]} 过滤 {b[1]}.{b[3]}，查询当前任务所需信息。",
                    "每次关联都检查字段含义、事件范围和候选相关性；若已处在中间步骤，只使用剩余操作，不重新执行已完成查询。",
                ]
                add(
                    ("chain", a, b),
                    proc(
                        f"{a[0]} → {a[1]} → {b[1]} ({a[2]} / {b[2]})",
                        f"从 {a[0]} 经 {a[1]} 取得关联键后，{TABLE_GOALS.get(b[1], '继续查证')}；已完成前段时只复用后段。",
                        slots,
                        ops,
                    ),
                    evidence(first["left"], first["right"], second["right"]),
                    "chain",
                )
    procedures = []
    audit = []
    singletons = Counter()
    for item in proposals.values():
        ev = item["evidence"]
        episode_ids = {x["episode_id"] for x in ev}
        incidents = {x["incident"] for x in ev}
        if len(episode_ids) < 2:
            singletons[item["family"]] += 1
            continue
        p = item["procedure"]
        p["status"] = "active" if len(incidents) >= 2 else "candidate"
        procedures.append(p)
        audit.append(
            {
                "name": p["name"],
                "family": item["family"],
                "status": p["status"],
                "episode_count": len(episode_ids),
                "incident_count": len(incidents),
                "evidence": ev,
            }
        )
    procedures.sort(key=lambda p: p["name"])
    audit.sort(key=lambda p: p["name"])
    covered = {
        aid
        for item in audit
        if item["status"] == "active"
        for ev in item["evidence"]
        for aid in ev["attempt_ids"]
    }
    report = {
        "attempt_count": len(records),
        "result_counts": dict(Counter(r["status"] for r in records)),
        "family_counts": dict(Counter(x["family"] for x in audit)),
        "active_family_counts": dict(
            Counter(x["family"] for x in audit if x["status"] == "active")
        ),
        "one_off_patterns_excluded": dict(singletons),
        "active_supported_attempt_count": len(covered),
        "active_supported_episode_count": len(
            {
                ev["episode_id"]
                for a in audit
                if a["status"] == "active"
                for ev in a["evidence"]
            }
        ),
        "uncovered_attempts": [
            {
                "attempt_id": r["attempt_id"],
                "status": r["status"],
                "reason": "no_repeated_active_pattern"
                if r["valid"] or r["status"] == "schema_result"
                else "unsupported_shape_or_no_successful_repair",
            }
            for r in records
            if r["attempt_id"] not in covered
        ],
        "procedures": audit,
    }
    return procedures, report
