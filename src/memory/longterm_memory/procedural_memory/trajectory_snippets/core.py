"""Evidence-preserving SQL normalization and explicit multi-step binding contracts."""

from __future__ import annotations
import ast
import hashlib
import json
import math
import re
import sqlglot
from sqlglot import exp
from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
from src.memory.longterm_memory.episodic_memory.excytin_bench.sanitize import (
    sanitize_text,
)


def digest(obj):
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:24]


def observation_rows(text):
    clean = re.sub(r"(?:\s*\.\.\.\s*\(Truncated\))+\s*$", "", text).strip()
    try:
        rows = ast.literal_eval(clean)
        return (
            rows
            if isinstance(rows, list)
            and all(isinstance(r, (tuple, list)) for r in rows)
            else None
        )
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        # Released observations sometimes end halfway through the last row.
        # Read only complete, balanced tuples; never invent the truncated tail.
        rows = []
        start = None
        depth = 0
        quote = None
        escape = False
        for i, char in enumerate(clean):
            if quote:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote:
                    quote = None
                continue
            if char in "'\"":
                quote = char
                continue
            if char == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif char == ")" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        row = ast.literal_eval(clean[start : i + 1])
                        if isinstance(row, tuple):
                            rows.append(row)
                    except (ValueError, SyntaxError):
                        return None
        return rows or None


def safe_text(text, values=()):
    protected = [str(v) for v in values if v is not None]
    # LIKE '%case-host%' often gets paraphrased without the wildcards.
    # Protect that spelling too; reusable descriptions must not retain case values.
    protected.extend(
        v.strip("%") for v in list(protected) if v.startswith("%") or v.endswith("%")
    )
    return sanitize_text(str(text), protected_values=protected)


def normalize_sql(sql, schema, requested_outputs=()):
    """Preserve predicates, joins, grouping and ordering. Parameterize business literals.

    No failed-query repair or field substitution occurs here. Unsupported cases
    stay in the coverage ledger rather than disappearing from the extraction.
    """
    ReadOnlySQLValidator.validate(sql)
    tree = sqlglot.parse_one(sql, read="mysql")
    if not isinstance(tree, exp.Select):
        raise ValueError("not_select")
    if tree.args.get("into") or tree.args.get("locks"):
        raise ValueError("write_or_lock")
    if tree.find(exp.Subquery) or tree.args.get("with_"):
        raise ValueError("nested_select_requires_review")
    tables = list(tree.find_all(exp.Table))
    aliases = {}
    for t in tables:
        canonical = next(
            (name for name in schema if name.casefold() == t.name.casefold()), None
        )
        if canonical is None:
            raise ValueError("unknown_table:" + t.name)
        if t.db and t.db != "env_monitor_db" or t.catalog:
            raise ValueError("foreign_database")
        aliases[t.alias_or_name.casefold()] = canonical
        t.set("this", exp.to_identifier(canonical))
        t.set("db", None)
    if not tables:
        raise ValueError("no_business_table")
    # Expand wildcard to explicit schema columns so the contract is checkable.
    projections = []
    for item in tree.expressions:
        if isinstance(item, exp.Star) or isinstance(item, exp.Column) and item.is_star:
            names = (
                [aliases[item.table.casefold()]]
                if isinstance(item, exp.Column) and item.table
                else [t.name for t in tables]
            )
            if len(names) != 1:
                raise ValueError("multi_table_star_requires_review")
            table = names[0]
            chosen = [f for f in requested_outputs if f in schema[table]]
            # No usable model projection: preserve all actual columns, not a guessed subset.
            chosen = chosen or list(schema[table])
            projections.extend(
                exp.column(
                    f,
                    table=(item.table or None)
                    if isinstance(item, exp.Column)
                    else None,
                )
                for f in chosen
            )
        else:
            projections.append(item)
    tree.set("expressions", projections)
    dependencies = {}
    for col in tree.find_all(exp.Column):
        if col.is_star:
            continue  # COUNT(*) is handled below, not a physical column.
        possible = (
            [aliases[col.table.casefold()]]
            if col.table and col.table.casefold() in aliases
            else [
                t.name
                for t in tables
                if any(f.casefold() == col.name.casefold() for f in schema[t.name])
            ]
        )
        possible = list(dict.fromkeys(possible))
        if len(possible) != 1:
            # ORDER BY projected aliases can be used without inventing a source field.
            if col.name in {
                p.alias for p in tree.expressions if p.alias
            } and col.find_ancestor(exp.Order):
                continue
            raise ValueError("unresolved_column:" + col.sql())
        table = possible[0]
        name = next(
            (f for f in schema[table] if f.casefold() == col.name.casefold()), None
        )
        if name is None:
            raise ValueError("unknown_column:" + col.name)
        col.set("this", exp.to_identifier(name))
        dependencies.setdefault(table, set()).add(name)
    inputs = {}
    bindings = {}

    def parameter(values, field, operator, many=False):
        key = "p" + str(len(inputs) + 1)
        val = values if many else [values]
        if not all(isinstance(v, exp.Literal) for v in val):
            raise ValueError("nonliteral_parameter")
        kinds = {v.is_string for v in val}
        if len(kinds) != 1:
            raise ValueError("mixed_parameter_type")
        kind = "string" if True in kinds else "number"
        parsed = [
            v.this
            if v.is_string
            else (
                float(v.this) if any(c in v.this.lower() for c in ".e") else int(v.this)
            )
            for v in val
        ]
        inputs[key] = {
            "field": field,
            "operator": operator,
            "type": kind + ("[]" if many else ""),
        }
        bindings[key] = parsed if many else parsed[0]
        return exp.Placeholder(this=key)

    for node in list(tree.find_all(exp.In)):
        if node.expressions and all(
            isinstance(v, exp.Literal) for v in node.expressions
        ):
            node.set(
                "expressions",
                [
                    parameter(
                        node.expressions,
                        node.this.name
                        if isinstance(node.this, exp.Column)
                        else node.this.sql(),
                        "IN",
                        True,
                    )
                ],
            )
    for node in list(tree.find_all(exp.Literal)):
        if node.find_ancestor(exp.Limit, exp.Offset):
            continue
        parent = node.parent
        # Structural constants such as JSON paths are retained only as reviewed literals.
        if node.is_string and isinstance(parent, (exp.JSONPath,)):
            continue
        container = node.find_ancestor(exp.Where, exp.Having)
        if container is None:
            raise ValueError("literal_outside_predicate_requires_review")
        field = ""
        probe = parent
        while probe is not None and probe is not container:
            cols = list(probe.find_all(exp.Column))
            if cols:
                field = cols[0].name
                break
            probe = probe.parent
        node.replace(parameter(node, field, parent.key.upper()))
    # A row cap is not a factual filter. Keep smaller observed limits; never drop WHERE.
    limit = tree.args.get("limit")
    row_cap = 100
    if limit and isinstance(limit.expression, exp.Literal):
        row_cap = min(100, int(limit.expression.this))
    tree.set("limit", exp.Limit(expression=exp.Literal.number(row_cap)))
    if tree.args.get("offset"):
        raise ValueError("offset_requires_review")
    template = {
        "sql": tree.sql(dialect="mysql"),
        "inputs": inputs,
        "outputs": [p.alias_or_name for p in tree.expressions],
        "schema_fields": {t: sorted(fields) for t, fields in dependencies.items()},
        "row_cap": row_cap,
        "scope": "filtered" if tree.args.get("where") else "unfiltered_discovery",
    }
    template["id"] = "sql_" + digest(template)
    compile_sql(template, bindings, schema)
    return template, bindings


def compile_sql(template, bindings, schema):
    if set(bindings) != set(template["inputs"]):
        raise ValueError("input_contract_mismatch")
    for table, fields in template["schema_fields"].items():
        if table not in schema or not set(fields) <= set(schema[table]):
            raise ValueError("target_schema_mismatch")
    ReadOnlySQLValidator.validate(template["sql"])
    tree = sqlglot.parse_one(template["sql"], read="mysql")
    values = []
    marker_values = {}
    for node in list(tree.find_all(exp.Placeholder)):
        key = node.name
        spec = template["inputs"][key]
        value = bindings[key]
        many = spec["type"].endswith("[]")
        items = value if many else [value]
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise ValueError("invalid_list_size")
        for item in items:
            if spec["type"].startswith("string"):
                if not isinstance(item, str) or len(item) > 20000:
                    raise ValueError("invalid_string")
            elif type(item) not in (int, float) or not math.isfinite(item):
                raise ValueError("invalid_number")
        markers = []
        for item in items:
            marker = "__PM_BIND_" + str(len(marker_values)) + "__"
            marker_values[marker] = item
            markers.append(exp.Var(this=marker))
        if many:
            if not isinstance(node.parent, exp.In):
                raise ValueError("list_not_in_predicate")
            node.parent.set("expressions", markers)
        else:
            node.replace(markers[0])

    def substitute(match):
        values.append(marker_values[match.group(0)])
        return "%s"

    sql = re.sub(r"__PM_BIND_\d+__", substitute, tree.sql(dialect="mysql"))
    return sql, tuple(values)


def json_values_with_paths(value, path=()):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from json_values_with_paths(nested, (*path, key))
    elif isinstance(value, list):
        for nested in value:
            yield from json_values_with_paths(nested, (*path, "*"))
    else:
        yield path, value


def verify_link(link, attempts, schemas):
    source = attempts[link["from"]]
    target = attempts[link["to"]]
    verdict = {
        "type": link["type"],
        "from": link["from"],
        "to": link["to"],
        "verified": False,
        "reason": "",
    }
    if link["type"] != "dataflow":
        verdict["reason"] = "conditional_control_or_recovery_not_value_flow"
        return verdict
    if source["status"] != "rows_returned" or target["status"] != "rows_returned":
        verdict["reason"] = "source_or_target_not_successful"
        return verdict
    quote = link.get("source_quote", "")
    if not quote or quote not in source["observation"]:
        verdict["reason"] = "source_quote_not_present"
        return verdict
    rows = observation_rows(source["observation"])
    tree = source["tree"]
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tree.args.get("joins") or tree.args.get("group"):
        verdict["reason"] = "source_projection_requires_review"
        return verdict
    columns = []
    for item in tree.expressions:
        if isinstance(item, exp.Star):
            columns.extend(schemas.get(tables[0].name, {}))
        elif isinstance(item, exp.Column):
            columns.append(item.name)
        elif isinstance(item, exp.Alias) and isinstance(item.this, exp.Column):
            columns.append(item.alias)
        else:
            columns.append(item.alias_or_name)
    field = next(
        (c for c in columns if c.casefold() == link.get("from_column", "").casefold()),
        None,
    )
    if field is None or rows is None:
        verdict["reason"] = "source_field_or_rows_not_resolved"
        return verdict
    idx = columns.index(field)
    available = [r[idx] for r in rows if len(r) == len(columns)]
    where = target["tree"].args.get("where")
    dest = link.get("to_field", "")
    used = []
    if where:
        for node in where.walk():
            if (
                isinstance(node, (exp.EQ, exp.In))
                and isinstance(node.this, exp.Column)
                and node.this.name.casefold() == dest.casefold()
            ):
                vals = (
                    node.expressions if isinstance(node, exp.In) else [node.expression]
                )
                used.extend(v.this for v in vals if isinstance(v, exp.Literal))
    if not used:
        # A temporal boundary is one parameter, not every predicate on the field.
        # Require the exact observed boundary, its actual operator and an unambiguous
        # normalized parameter. The opposite bound remains an external input.
        claimed = link.get("value")
        boundaries = []
        if where and link.get("transform") in {"identity", "singleton_list"}:
            for node in where.walk():
                if (
                    isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE))
                    and isinstance(node.this, exp.Column)
                    and node.this.name.casefold() == dest.casefold()
                    and isinstance(node.expression, exp.Literal)
                ):
                    value = node.expression.this
                    if (
                        str(claimed) == value
                        and value in quote
                        and value in set(map(str, available))
                    ):
                        boundaries.append((value, node.key.upper()))
        if len(boundaries) != 1:
            verdict["reason"] = (
                "target_filter_is_not_equality_or_in_or_unique_observed_boundary"
            )
            return verdict
        value, operator = boundaries[0]
        keys = [
            key
            for key, spec in target.get("input_specs", {}).items()
            if spec["field"].casefold() == dest.casefold()
            and spec["operator"] == operator
            and str(target["bindings"][key]) == value
        ]
        if len(keys) != 1:
            verdict["reason"] = "boundary_parameter_not_unique"
            return verdict
        verdict.update(to_parameter=keys[0], to_operator=operator)
        used = [value]
    paths = {}
    if link.get("transform") == "parse_json":
        for raw in available:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            for path, value in json_values_with_paths(data):
                paths.setdefault(path, set()).add(str(value))
        matched = [
            path for path, values in paths.items() if set(map(str, used)) <= values
        ]
        if len(matched) != 1:
            verdict["reason"] = "json_path_not_uniquely_supported"
            return verdict
        verdict["json_path"] = list(matched[0])
        transform = "parse_json"
    else:
        if not set(map(str, used)) <= set(map(str, available)):
            verdict["reason"] = "value_not_in_declared_source_column"
            return verdict
        # Scalar/list cardinality is already defined by the target input type.
        # These labels describe the same value-preserving transfer at runtime.
        transform = "identity"
    if not any(str(value) in quote for value in used):
        verdict["reason"] = "quoted_evidence_does_not_contain_transferred_value"
        return verdict
    verdict.update(
        verified=True,
        from_column=field,
        to_field=dest,
        transform=transform,
        reason="all bound target values occur in the declared source field; quoted original evidence verified",
        evidence_values_sha256=[
            hashlib.sha256(str(value).encode()).hexdigest() for value in used
        ],
        values_checked=len(used),
    )
    return verdict
