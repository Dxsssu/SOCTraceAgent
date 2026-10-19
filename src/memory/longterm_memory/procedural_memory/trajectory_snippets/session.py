"""Planner router: multi-step Procedural -> single-step Episodic -> Semantic."""

from copy import deepcopy
import json
import re

import sqlglot
from sqlglot import exp
import yaml

from .core import observation_rows
from src.memory.longterm_memory.semantic_memory.v2.store import _normalize


def tokens(text):
    text = str(text).casefold()
    words = set(re.findall(r"[a-z][a-z0-9_]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return words


def literal_sql(template, bindings):
    """The environment accepts a string: render typed AST literals, never interpolate SQL."""
    tree = sqlglot.parse_one(template["sql"], read="mysql")
    for node in list(tree.find_all(exp.Placeholder)):
        value = bindings[node.name]
        if isinstance(value, list):
            if not isinstance(node.parent, exp.In):
                raise ValueError("list_placeholder_outside_IN")
            node.parent.set("expressions", [exp.convert(v) for v in value])
        else:
            node.replace(exp.convert(value))
    return tree.sql(dialect="mysql")


class ProceduralPlanningSession:
    def __init__(self, semantic_session, store=None):
        self.semantic = semantic_session
        from ..flows import load_runtime_catalog
        self.store = store or load_runtime_catalog()
        self.disabled = set(self.store.document.get("disabled_procedural_ids", []))
        self.retrievals = []
        self.offered = {}
        self.tasks = {}
        self.runs = {}
        self.tried = set()
        self.fallbacks = []

    def canonical(self, name):
        return self.semantic.memory.aliases.get(_normalize(name), name)

    @staticmethod
    def memory_type(snippet):
        return "procedural" if len(snippet["steps"]) > 1 else "episodic"

    def vocabulary(self, memory_type=None):
        """Expose entry concepts for the requested layer without its full catalog."""
        result = {}
        for snippet in self.store.snippets.values():
            if snippet["id"] in self.disabled:
                continue
            if memory_type and self.memory_type(snippet) != memory_type:
                continue
            trigger = snippet["trigger_entity"]
            result.setdefault(trigger["concept"], set()).add(trigger["kind"])
        return {
            name: {"kinds": sorted(kinds)} for name, kinds in sorted(result.items())
        }

    def field_concepts(self, table, field):
        return {
            (name, row["kind"])
            for name, item in self.semantic.memory.concepts.items()
            for row in item["fields"]
            if row["table"] == table and row["field"] == field
        }

    def view(self, snippet):
        return {
            k: deepcopy(snippet[k])
            for k in (
                "id",
                "name",
                "intent",
                "trigger_entity",
                "required_inputs",
                "bindings",
                "caution",
            )
        } | {
            "memory_type": self.memory_type(snippet),
            "steps": [
                {
                    "id": step["id"],
                    **({"episodic_id": step["episodic_id"]} if "episodic_id" in step else {}),
                    "guide": step["planner_guide"],
                    "tables": list(
                        self.store.templates[step["template_id"]]["schema_fields"]
                    ),
                    "inputs": self.store.templates[step["template_id"]]["inputs"],
                }
                for step in snippet["steps"]
            ]
        }

    def retrieve(self, query):
        args = self.semantic.queries(query)
        key = json.dumps(query, sort_keys=True, ensure_ascii=False)
        last = None
        for memory_type in ("procedural", "episodic"):
            # Repeating a query rejects the previously offered layer, not all memory.
            if (key, memory_type) in self.tried:
                continue
            self.tried.add((key, memory_type))
            last = self._retrieve_layer(query, args, memory_type)
            if last["candidates"]:
                return last
        return last

    def _retrieve_layer(self, query, args, memory_type):
        schema = self.semantic.memory.schema
        known = [(self.canonical(q.concept), q.kind) for q in args["known"]]
        wanted = {self.canonical(q.concept) for q in args["wanted"]}
        intent = tokens(args["intent"] or self.semantic.default_intent)
        ranked = []
        if schema is not None:
            for snippet in self.store.snippets.values():
                if snippet["id"] in self.disabled:
                    continue
                if self.memory_type(snippet) != memory_type:
                    continue
                templates = [
                    self.store.templates[st["template_id"]] for st in snippet["steps"]
                ]
                if any(
                    t not in schema or not set(fs) <= set(schema[t])
                    for template in templates
                    for t, fs in template["schema_fields"].items()
                ):
                    continue
                first = snippet["steps"][0]["id"]
                required = [x for x in snippet["required_inputs"] if x["step"] == first]
                available = (
                    set().union(
                        *(self.field_concepts(x["table"], x["field"]) for x in required)
                    )
                    if required
                    else set()
                )
                available.add(
                    (
                        snippet["trigger_entity"]["concept"],
                        snippet["trigger_entity"]["kind"],
                    )
                )
                matched_known = {
                    (c, kind) for c, kind in known
                    if any(c == a and (not kind or kind == b or c == "IP")
                           for a, b in available)
                }
                if not matched_known:
                    continue
                outputs = {
                    v.get("entity_concept")
                    for step in snippet["steps"]
                    for v in step["outputs"].values()
                }
                # Historical annotations omit some physical outputs (e.g. ApplicationId).
                # Resolve only projected fields, never WHERE-only fields, via Semantic.
                for template in templates:
                    for table, fields in template['schema_fields'].items():
                        for field in set(fields) & set(template['outputs']):
                            outputs.update(concept for concept, _ in self.field_concepts(table, field))
                overlap = wanted & outputs
                variants = snippet.get("intent_variants") or [snippet]
                variant = max(
                    variants,
                    key=lambda v: len(intent & tokens(v["intent"] + " " + v["name"])),
                )
                lexical = len(
                    intent & tokens(variant["intent"] + " " + variant["name"])
                )
                if (wanted and not overlap or not wanted and lexical < 2
                        or memory_type == "procedural" and wanted and not wanted <= outputs):
                    continue
                score = (
                    8 * len(overlap) / max(1, len(wanted))
                    + min(lexical, 10) * 0.2
                    # Prefer entry steps that use more of the supplied evidence.
                    + 2 * len(matched_known) / max(1, len(set(known)))
                    - 0.12 * len(templates)
                )
                hit = deepcopy(snippet)
                hit.update(
                    name=variant["name"],
                    intent=variant["intent"],
                    caution=variant["caution"],
                )
                guides = {g["step"]: g["guide"] for g in variant.get("step_guides", [])}
                for step in hit["steps"]:
                    step["planner_guide"] = guides.get(
                        step["id"], step["planner_guide"]
                    )
                ranked.append((score, hit))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        hits = [s for _, s in ranked[:4]]
        for s in hits:
            self.offered[s["id"]] = s
        record = {
            "lookup_id": ("PM" if memory_type == "procedural" else "EM") + str(1 + sum(r["memory_type"] == memory_type for r in self.retrievals)),
            "memory_type": memory_type,
            "query": deepcopy(query),
            "route": memory_type + "_candidate" if hits else ("episodic" if memory_type == "procedural" else "semantic"),
            "reason": "ranked_entity_intent_candidates"
            if hits
            else "no_applicable_candidate_or_live_schema",
            "candidates": [self.view(s) for s in hits],
        }
        self.retrievals.append(record)
        return record

    def parse(self, text):
        fenced = re.fullmatch(r"\s*```(?:yaml|yml)?\s*\n(.*?)\n```\s*", text, re.S)
        value = yaml.safe_load(fenced.group(1) if fenced else text)
        if isinstance(value, dict) and set(value) == {"memory_query"}:
            self.semantic.queries(value["memory_query"])
            return value
        clean = deepcopy(value)
        if not isinstance(clean, dict):
            raise ValueError("Return memory_query or operations")
        for op in clean.get("operations", []):
            selectors = [k for k in ("procedure", "episode") if k in op]
            if selectors:
                if len(selectors) != 1 or op.get("op") != "add_task" or any(
                    k in op for k in ("table", "schema_probe", "semantic_refs", "template_id")
                ):
                    raise ValueError("Choose exactly one procedure/episode on add_task; do not mix table/schema_probe")
                op.pop(selectors[0])
        self.semantic._clean_operations(clean)
        return value

    def bind(self, spec):
        if not isinstance(spec, dict) or set(spec) - {
            "id",
            "step",
            "run",
            "inputs",
            "selections",
        }:
            raise ValueError(
                "procedure requires id, step, inputs; optional run, selections"
            )
        sid, step_id = spec.get("id"), spec.get("step")
        if sid in self.disabled:
            raise ValueError("This procedural flow is temporarily disabled")
        if sid not in self.offered:
            raise ValueError("Choose a retrieved procedural id")
        snippet = self.offered[sid]
        if step_id not in {s["id"] for s in snippet["steps"]}:
            raise ValueError("Unknown procedure step")
        run = spec.get("run")
        if run:
            if run not in self.runs or self.runs[run]["id"] != sid:
                raise ValueError("run must be the first task ID of this exact snippet")
            observations = self.runs[run]["observations"]
            if step_id in observations:
                raise ValueError(
                    "This procedure step was already executed; use Semantic Memory to investigate further"
                )
        else:
            if step_id != snippet["steps"][0]["id"]:
                raise ValueError(
                    "Start at s1 or reference an existing run with observed upstream results"
                )
            observations = {}
        inputs = spec.get("inputs", {})
        selections = spec.get("selections", {})
        if not isinstance(inputs, dict) or not isinstance(selections, dict):
            raise ValueError("inputs and selections must be mappings")
        prepared = self.store.prepare_step(
            sid,
            step_id,
            schema=self.semantic.memory.schema,
            external_inputs=inputs,
            observations=observations,
            selections=selections,
            allow_candidates=True,
        )
        template = self.store.templates[prepared["template_id"]]
        return {
            "mode": "procedure",
            "memory_type": self.memory_type(snippet),
            "episodic_id": (sid if self.memory_type(snippet) == "episodic" else next(s for s in snippet["steps"] if s["id"] == step_id).get("episodic_id")),
            "snippet_id": sid,
            "step_id": step_id,
            "run": run,
            "spec": deepcopy(spec),
            "sql_template": template["sql"],
            "bound_sql": literal_sql(template, prepared["bindings"]),
            "bindings": prepared["bindings"],
            "outputs": prepared["outputs"],
            "row_cap": prepared["row_cap"],
            "caution": snippet["caution"],
        }

    def prepare(self, payload):
        clean = deepcopy(payload)
        contexts = []
        for op in clean["operations"]:
            if op["op"] != "add_task":
                continue
            selectors = [k for k in ("procedure", "episode") if k in op]
            if selectors:
                if len(selectors) != 1 or any(k in op for k in ("table", "schema_probe", "semantic_refs", "template_id")):
                    raise ValueError("procedure, episode and table/schema_probe are mutually exclusive")
                key = selectors[0]
                ctx = self.bind(op.pop(key))
                expected = "procedural" if key == "procedure" else "episodic"
                if ctx["memory_type"] != expected:
                    raise ValueError("Use procedure for multi-step flows and episode for single-step Episodic snippets")
                contexts.append(ctx)
            else:
                # A normal table task explicitly declines the fast path. Resolve its
                # schema through Semantic before validation, even when the preceding
                # query returned PM candidates. Retrieval is local and cached.
                if self.retrievals:
                    query = self.retrievals[-1]["query"]
                    key = json.dumps(query, sort_keys=True, ensure_ascii=False)
                    if (key, "episodic") not in self.tried:
                        raise ValueError("Procedural candidates were declined. Repeat the same memory_query to retrieve Episodic before choosing a Semantic table.")
                    if not any(r["query"] == query for r in self.semantic.retrievals):
                        self.semantic.retrieve(query)
                        self.fallbacks.append(
                            {
                                "query": deepcopy(query),
                                "reason": "planner_selected_table_instead_of_template",
                            }
                        )
                    table = op.get("table")
                    # Ranking pages are not an allow-list. A Planner may name a
                    # registered table seen in a snippet or a schema probe. Search
                    # the remaining pages of the same concept query before rejecting
                    # it; eligibility and field validation still belong to Semantic.
                    if table in self.semantic.memory.tables:
                        page = next(
                            r
                            for r in reversed(self.semantic.retrievals)
                            if r["query"] == query
                        )
                        while not any(
                            table == c["table"]
                            for r in self.semantic.retrievals
                            for c in r["result"]["candidates"]
                        ):
                            offset = page["result"].get("next_offset")
                            if offset is None:
                                break
                            paged = {**query, "offset": offset}
                            cached = next(
                                (
                                    r
                                    for r in self.semantic.retrievals
                                    if r["query"] == paged
                                ),
                                None,
                            )
                            if cached is None:
                                self.semantic.retrieve(paged)
                                cached = self.semantic.retrievals[-1]
                                self.fallbacks.append(
                                    {
                                        "query": paged,
                                        "table": table,
                                        "reason": "selected_table_pagination",
                                    }
                                )
                            page = cached
                _, items = self.semantic.prepare({"operations": [op]})
                contexts.extend(items)
                for key in ("table", "schema_probe"):
                    op.pop(key, None)
        self.semantic._clean_operations(clean)
        return clean, contexts

    def register(self, node_id, context):
        if context.get("mode") != "procedure":
            return
        run = context["run"] or node_id
        context["run"] = run
        self.runs.setdefault(run, {"id": context["snippet_id"], "observations": {}})
        self.tasks[node_id] = context

    def observe(self, node_id, text, success, truncated=False):
        context = self.tasks.get(node_id)
        if context is None:
            return
        rows = observation_rows(text)
        valid = rows is not None and all(
            len(row) == len(context["outputs"]) for row in rows
        )
        self.runs[context["run"]]["observations"][context["step_id"]] = {
            "success": success and valid,
            "truncated": truncated
            or "Truncated" in text
            or text.startswith("Retrieved ")
            or (valid and len(rows) >= context["row_cap"]),
            "rows": [dict(zip(context["outputs"], row)) for row in rows]
            if valid
            else [],
        }

    def active_runs(self):
        return [
            {
                "run": key,
                "snippet_id": run["id"],
                "completed": [
                    {
                        "step": step,
                        "success": obs["success"],
                        "truncated": obs["truncated"],
                        "rows": len(obs["rows"]),
                    }
                    for step, obs in run["observations"].items()
                ],
                "plan": self.view(self.offered[run["id"]]),
            }
            for key, run in self.runs.items()
            if self.memory_type(self.offered[run["id"]]) == "procedural"
        ]
