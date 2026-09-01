"""MySQL trajectory parsing and Semantic Memory reference extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from .sanitize import parameterize_sql

EXECUTE_ACTION_RE = re.compile(r"Action:\s*execute\[(.*)\]\s*$", re.IGNORECASE | re.DOTALL)
SUBMIT_ACTION_RE = re.compile(r"Action:\s*submit\[(.*)\]\s*$", re.IGNORECASE | re.DOTALL)
SQL_ERROR_RE = re.compile(
    r"(?:ProgrammingError|OperationalError|DatabaseError|InterfaceError):", re.IGNORECASE
)
UNKNOWN_COLUMN_RE = re.compile(r"Unknown column\s+'([^']+)'", re.IGNORECASE)
UNKNOWN_TABLE_RE = re.compile(r"Table\s+'([^']+)'\s+doesn't exist", re.IGNORECASE)


@dataclass(frozen=True)
class SemanticIndex:
    catalog_id: str
    schema_hash: str
    tables_by_lower_name: dict[str, dict[str, Any]]
    fields_by_table: dict[str, dict[str, dict[str, Any]]]
    join_keys_by_name: dict[str, dict[str, Any]]

    @classmethod
    def from_knowledge(cls, knowledge: dict[str, Any]) -> SemanticIndex:
        tables = {table["name"].lower(): table for table in knowledge["tables"]}
        fields = {
            table["name"]: {field["name"].lower(): field for field in table["fields"]}
            for table in knowledge["tables"]
        }
        join_keys = {item["name"]: item for item in knowledge["join_keys"]}
        return cls(
            catalog_id=knowledge["catalog"]["catalog_id"],
            schema_hash=knowledge["catalog"]["schema_hash"],
            tables_by_lower_name=tables,
            fields_by_table=fields,
            join_keys_by_name=join_keys,
        )

    def table(self, name: str) -> dict[str, Any] | None:
        return self.tables_by_lower_name.get(name.lower())

    def field(self, table_name: str, field_name: str) -> dict[str, Any] | None:
        table = self.table(table_name)
        if not table:
            return None
        return self.fields_by_table[table["name"]].get(field_name.lower())


def extract_execute_sql(message: str) -> str | None:
    match = EXECUTE_ACTION_RE.search(message)
    return match.group(1).strip() if match else None


def is_submit(message: str) -> bool:
    return bool(SUBMIT_ACTION_RE.search(message))


def classify_error(observation: str) -> tuple[str | None, str | None]:
    if not SQL_ERROR_RE.search(observation):
        return None, None
    if UNKNOWN_COLUMN_RE.search(observation):
        return "unknown_column", UNKNOWN_COLUMN_RE.search(observation).group(1)
    if UNKNOWN_TABLE_RE.search(observation):
        table_name = UNKNOWN_TABLE_RE.search(observation).group(1).rsplit(".", 1)[-1]
        return "unknown_table", table_name
    if "syntax" in observation.lower():
        return "syntax_error", None
    return "other_sql_error", None


def classify_result(query_kind: str, observation: str) -> tuple[str, str, str | None, str | None]:
    error_type, error_subject = classify_error(observation)
    if error_type:
        return "sql_error", "error", error_type, error_subject
    if query_kind == "schema_discovery":
        return "executed", "schema_result", None, None
    if re.match(r"^\s*\[\s*\]", observation):
        return "executed", "empty_result", None, None
    return "executed", "rows_returned", None, None


def _table_roles(tree: exp.Expression, table_node: exp.Table) -> list[str]:
    return ["join"] if isinstance(table_node.parent, exp.Join) else ["source"]


def _column_roles(column: exp.Column) -> list[str]:
    roles: set[str] = set()
    current = column.parent
    while current is not None and not isinstance(current, exp.Select):
        if isinstance(current, exp.Join):
            roles.add("join")
        elif isinstance(current, (exp.Where, exp.Having)):
            roles.add("filter")
        elif isinstance(current, exp.Group):
            roles.add("group")
        elif isinstance(current, exp.Order):
            roles.add("order")
        current = current.parent
    if not roles:
        roles.add("select")
    return sorted(roles)


def _resolved_table_names(tree: exp.Expression, semantic: SemanticIndex) -> tuple[list[dict[str, Any]], dict[str, str]]:
    refs: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for table_node in tree.find_all(exp.Table):
        table = semantic.table(table_node.name)
        if not table:
            continue
        canonical_name = table["name"]
        refs.setdefault(table["table_id"], set()).update(_table_roles(tree, table_node))
        aliases[table_node.alias_or_name.lower()] = canonical_name
        aliases[canonical_name.lower()] = canonical_name
    return (
        [
            {"table_id": table_id, "roles": sorted(roles)}
            for table_id, roles in sorted(refs.items())
        ],
        aliases,
    )


def _resolve_column(
    column: exp.Column,
    *,
    table_names: list[str],
    aliases: dict[str, str],
    semantic: SemanticIndex,
) -> tuple[dict[str, Any] | None, str | None]:
    target_table: str | None = None
    if column.table:
        target_table = aliases.get(column.table.lower())
    elif len(table_names) == 1:
        target_table = table_names[0]
    else:
        candidates = [name for name in table_names if semantic.field(name, column.name)]
        if len(candidates) == 1:
            target_table = candidates[0]

    field = semantic.field(target_table, column.name) if target_table else None
    unresolved = f"{target_table}.{column.name}" if target_table else column.name
    return field, None if field else unresolved


def analyze_sql(sql: str, semantic: SemanticIndex) -> dict[str, Any]:
    """Parse one corpus SQL statement and return safe graph-ready metadata."""

    tree = sqlglot.parse_one(sql, read="mysql")
    query_kind = "schema_discovery" if isinstance(tree, (exp.Show, exp.Describe)) else "evidence_query"
    sql_template, parameter_types = parameterize_sql(tree)
    table_refs, aliases = _resolved_table_names(tree, semantic)
    table_names = [semantic.table(ref["table_id"].split(":", 1)[1])["name"] for ref in table_refs]

    field_roles: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    resolved_columns: dict[int, dict[str, Any]] = {}
    for column in tree.find_all(exp.Column):
        field, unresolved_name = _resolve_column(
            column,
            table_names=table_names,
            aliases=aliases,
            semantic=semantic,
        )
        if field:
            field_roles.setdefault(field["field_id"], set()).update(_column_roles(column))
            resolved_columns[id(column)] = field
        elif unresolved_name:
            unresolved.add(unresolved_name)

    join_key_ids: set[str] = set()
    for equality in tree.find_all(exp.EQ):
        left = equality.this
        right = equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_field = resolved_columns.get(id(left))
        right_field = resolved_columns.get(id(right))
        if not left_field or not right_field:
            continue
        left_key = left_field.get("join_key")
        if left_key and left_key == right_field.get("join_key") and left_key in semantic.join_keys_by_name:
            join_key_ids.add(semantic.join_keys_by_name[left_key]["join_key_id"])

    return {
        "query_kind": query_kind,
        "sql_template": sql_template,
        "parameter_types": parameter_types,
        "table_refs": table_refs,
        "field_refs": [
            {"field_id": field_id, "roles": sorted(roles)}
            for field_id, roles in sorted(field_roles.items())
        ],
        "join_key_ids": sorted(join_key_ids),
        "unresolved_field_names": sorted(unresolved),
    }
