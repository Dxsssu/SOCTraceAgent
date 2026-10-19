"""Planner-facing retrieval session; local dictionaries, no model or DB calls."""
from copy import deepcopy
import re

import yaml

from src.schema.planner_operations import validate_operations
from .store import ConceptQuery, SemanticMemoryV2, executor_context, render_view


class SemanticPlanningSession:
    def __init__(self, memory: SemanticMemoryV2, *, default_intent: str = "", goal_intent: str = ""):
        self.memory = memory
        self.default_intent = default_intent
        self.goal_intent = goal_intent
        self.retrievals: list[dict] = []

    def vocabulary(self):
        return {name: {"aliases": item["aliases"],
                       "kinds": sorted({r["kind"] for r in item["fields"]}),
                       **({"kind_policy": "hint"} if item.get("kind_policy") == "hint" else {})}
                for name, item in self.memory.concepts.items() if item.get("expose_in_vocabulary", True)}

    @staticmethod
    def queries(payload):
        example = 'memory_query: {known: [IP], wanted: [Login_Time], intent: "查询登录认证时间"}'
        if not isinstance(payload, dict) or set(payload) - {"known", "wanted", "offset", "intent", "layer", "exclude_ids", "goal_id"} or "known" not in payload:
            raise ValueError("Use known/wanted/intent, not query/concepts. Example: " + example)
        result = {}
        for role in ("known", "wanted"):
            values = payload.get(role, [])
            if not isinstance(values, list) or not (1 if role == "known" else 0) <= len(values) <= 12:
                raise ValueError("known requires 1..12 concepts; wanted allows 0..12. " + example)
            result[role] = []
            for value in values:
                v = {"concept": value} if isinstance(value, str) else value
                if not isinstance(v, dict) or set(v) - {"concept", "kind"} or not isinstance(v.get("concept"), str) or not v["concept"].strip():
                    raise ValueError("Use a concept string or {concept: name, kind: optional_kind}; omit entity values")
                if "kind" in v and (not isinstance(v["kind"], str) or not v["kind"].strip()):
                    raise ValueError("kind must be a nonempty string or omitted")
                result[role].append(ConceptQuery(**v))
        offset = payload.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 100:
            raise ValueError("offset must be an integer in 0..100")
        intent = payload.get("intent", "")
        if payload.get("layer", "auto") not in {"auto", "procedural", "composed", "episodic", "semantic"}:
            raise ValueError("memory_query.layer must be auto/procedural/composed/episodic/semantic")
        if not isinstance(payload.get("exclude_ids", []), list) or any(not isinstance(x, str) for x in payload.get("exclude_ids", [])):
            raise ValueError("exclude_ids must be a list of memory IDs")
        if 'goal_id' in payload and (not isinstance(payload['goal_id'], str) or not re.fullmatch(r'G[1-9]\d*', payload['goal_id'])):
            raise ValueError('goal_id must name an existing G node, such as G1')
        if not isinstance(intent, str) or len(intent) > 2000:
            raise ValueError("intent must be a short string (max 2000 characters)")
        return {**result, "offset": offset, "intent": intent}

    @staticmethod
    def _clean_operations(payload):
        cleaned = deepcopy(payload)
        for op in cleaned.get("operations", []):
            if not isinstance(op, dict):
                continue
            for key in ("table", "schema_probe"):
                if key in op:
                    if op.get("op") != "add_task":
                        raise ValueError(f"{key} is allowed only on add_task")
                    if key == "schema_probe" and op[key] is not True:
                        raise ValueError("schema_probe must be true")
                    if key == "table" and (not isinstance(op[key], str) or not op[key].strip()):
                        raise ValueError("table must be a nonempty candidate table name")
                    op.pop(key)
        validate_operations(cleaned)
        return cleaned

    def parse(self, text):
        fenced = re.fullmatch(r"\s*```(?:yaml|yml)?[ \t]*\r?\n(.*?)\r?\n```\s*", text, re.I | re.S)
        value = yaml.safe_load(fenced.group(1) if fenced else text)
        if isinstance(value, dict) and set(value) == {"memory_query"}:
            self.queries(value["memory_query"])
            return value
        if not isinstance(value, dict):
            raise ValueError("Return one YAML object: memory_query or operations")
        self._clean_operations(value)
        return value

    def retrieve(self, query):
        if len(self.retrievals) >= 32:
            raise ValueError("Semantic lookup budget exhausted (32 per investigation)")
        args = self.queries(query)
        # Preserve the actual question as ranking context even if the model omits intent.
        args["intent"] = " ".join(dict.fromkeys(filter(None, [self.default_intent, args["intent"]])))
        result = self.memory.retrieve(**args)
        record = {"lookup_id": f"SM{len(self.retrievals) + 1}", "query": deepcopy(query), "result": result}
        self.retrievals.append(record)
        return {"lookup_id": record["lookup_id"], "view": render_view(result)}

    def prepare(self, payload):
        """Bind selected table to cached field meanings; partial candidates are valid steps."""
        cleaned = self._clean_operations(payload)
        contexts = []
        for op in payload["operations"]:
            if op["op"] != "add_task":
                continue
            if op.get("schema_probe"):
                if op.get("semantic_refs") or op.get("table"):
                    raise ValueError("A schema_probe cannot also select a business table")
                if not self.retrievals:
                    raise ValueError("Try memory_query first, e.g. {known: [IP], intent: 查询登录认证时间}")
                contexts.append({"mode": "schema_probe"})
                continue
            table = op.get("table")
            refs = op.get("semantic_refs")
            # Compatibility for saved callers; new prompts request only table.
            legacy_fields = None
            if refs is not None:
                if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict) or set(refs[0]) != {"table", "fields"}:
                    raise ValueError("Use table: candidate_table; semantic_refs is no longer required")
                if table and table != refs[0]["table"]:
                    raise ValueError("Conflicting selected tables")
                table = refs[0]["table"]
                legacy_fields = refs[0]["fields"]
                if not isinstance(legacy_fields, list) or any(not isinstance(f, str) for f in legacy_fields):
                    raise ValueError("Legacy fields must be a list of field names")
            if table is None:
                available = {candidate["table"] for record in self.retrievals
                             for candidate in record["result"]["candidates"]}
                mentioned = [name for name in available if _identifier_mentioned(name, op["objective"])]
                if len(mentioned) == 1:
                    table = mentioned[0]
            if not isinstance(table, str) or not table:
                raise ValueError("Select one retrieved table using table: <name>, or name exactly one candidate table in objective")
            bindings = []
            for record in self.retrievals:
                try:
                    context = executor_context(record["result"], table)
                except ValueError:
                    continue
                bindings.append((record["lookup_id"], context))
            if not bindings:
                raise ValueError(f"{table} is not an eligible retrieved table; retrieve candidates first, then select table")
            lookup_id, context = bindings[-1]
            fields = {}
            for _, previous in bindings:
                for row in previous["fields"]:
                    fields[row["field"]] = row
            # The task itself may explicitly request additional physical fields.
            # Resolve only exact names in the selected table against the same flat catalog;
            # never send its complete schema or trust an unregistered model-invented field.
            expanded = []
            for concept, item in self.memory.concepts.items():
                for row in item["fields"]:
                    column = row["field"]
                    if row["table"] != table or column in fields or not _identifier_mentioned(column, op["objective"]):
                        continue
                    if self.memory.schema is not None and column not in self.memory.schema.get(table, {}):
                        continue
                    field = {k: v for k, v in row.items() if k != "table"}
                    if self.memory.schema is not None:
                        field["type"] = self.memory.schema[table][column]
                    fields[column] = {**field, "concept": concept, "use": "task"}
                    expanded.append(column)
            context["fields"] = list(fields.values())
            if expanded:
                context["task_resolved_fields"] = expanded
            allowed = set(fields) | ({context["time_field"]["field"]} if context["time_field"] else set())
            if legacy_fields is not None and not set(legacy_fields) <= allowed:
                raise ValueError("Requested fields were not retrieved; query additional concepts first")
            contexts.append({"lookup_id": lookup_id, "lookup_ids": [i for i, _ in bindings], **context})
        return cleaned, contexts


def _identifier_mentioned(name: str, text: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.I))


def validate_sql_binding(sql: str, context: dict):
    """Verify physical tables and columns using only the projected field schema.

    SQL aliases/CTEs are resolved by sqlglot, not guessed from name substrings.
    """
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    expression = sqlglot.parse_one(sql, read="mysql")
    scopes = traverse_scope(expression)
    sources = [source for scope in scopes for _, source in scope.selected_sources.values()
               if isinstance(source, exp.Table)]
    table = context["table"]
    if not sources or any(source.name.casefold() != table.casefold() or source.catalog
                          or source.db not in ("", "env_monitor_db") for source in sources):
        raise ValueError(f"SQL must query selected table {table}; ask Planner to retrieve/select another table if needed")
    # A literal SELECT * would read the entire real schema despite the projection.
    if any(not isinstance(star.parent, exp.Count) for star in expression.find_all(exp.Star)):
        raise ValueError("Select explicit retrieved fields; SELECT * is not allowed")
    schema = {row["field"]: row["type"] for row in context["fields"]}
    if context.get("time_field"):
        schema[context["time_field"]["field"]] = context["time_field"]["type"]
    copy = expression.copy()
    for source in copy.find_all(exp.Table):
        source.set("db", None)
    try:
        qualify(copy, dialect="mysql", schema={table: schema}, infer_schema=False,
                validate_qualify_columns=True, identify=False)
    except sqlglot.errors.OptimizeError as exc:
        raise ValueError(f"SQL uses fields outside retrieved context: {exc}. Available fields: {', '.join(schema)}") from exc
