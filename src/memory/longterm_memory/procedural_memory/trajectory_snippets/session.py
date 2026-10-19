"""Workflow adapter: retrieve small candidates, bind one ready step, retain live results."""

from copy import deepcopy
import json
from pathlib import Path
import re

import sqlglot
from sqlglot import exp
import yaml

from .store import TrajectorySnippetStore
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
        self.store = store or TrajectorySnippetStore.load(
            Path(__file__).parent / "excytin/catalog.json"
        )
        self.retrievals = []
        self.offered = {}
        self.tasks = {}
        self.runs = {}
        self.tried = set()
        self.fallbacks = []

    def canonical(self, name):
        return self.semantic.memory.aliases.get(_normalize(name), name)

    def vocabulary(self):
        """Expose PM-only entry concepts without publishing the snippet catalog."""
        result = {}
        for snippet in self.store.snippets.values():
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
            "steps": [
                {
                    "id": step["id"],
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
        # A repeated query explicitly requests the slow path after rejecting candidates.
        if key in self.tried:
            return None
        self.tried.add(key)
        schema = self.semantic.memory.schema
        known = [(self.canonical(q.concept), q.kind) for q in args["known"]]
        wanted = {self.canonical(q.concept) for q in args["wanted"]}
        intent = tokens(args["intent"] or self.semantic.default_intent)
        ranked = []
        if schema is not None:
            for snippet in self.store.snippets.values():
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
                if not any(
                    c == a and (not kind or kind == b or c == "IP")
                    for c, kind in known
                    for a, b in available
                ):
                    continue
                outputs = {
                    v.get("entity_concept")
                    for step in snippet["steps"]
                    for v in step["outputs"].values()
                }
                overlap = wanted & outputs
                variants = snippet.get("intent_variants") or [snippet]
                variant = max(
                    variants,
                    key=lambda v: len(intent & tokens(v["intent"] + " " + v["name"])),
                )
                lexical = len(
                    intent & tokens(variant["intent"] + " " + variant["name"])
                )
                if wanted and not overlap or not wanted and lexical < 2:
                    continue
                score = (
                    8 * len(overlap) / max(1, len(wanted))
                    + min(lexical, 10) * 0.2
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
            "lookup_id": f"PM{len(self.retrievals) + 1}",
            "query": deepcopy(query),
            "route": "procedural_candidate" if hits else "semantic",
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
        if isinstance(value, dict) and set(value) == {"semantic_query"}:
            self.semantic.queries(value["semantic_query"])
            return value
        clean = deepcopy(value)
        if not isinstance(clean, dict):
            raise ValueError("Return semantic_query or operations")
        for op in clean.get("operations", []):
            if "procedure" in op:
                if op.get("op") != "add_task" or any(
                    k in op
                    for k in ("table", "schema_probe", "semantic_refs", "template_id")
                ):
                    raise ValueError(
                        "procedure is exclusive to add_task and cannot mix with table/schema_probe"
                    )
                op.pop("procedure")
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
            if "procedure" in op:
                contexts.append(self.bind(op.pop("procedure")))
            else:
                # A normal table task explicitly declines the fast path. Resolve its
                # schema through Semantic before validation, even when the preceding
                # query returned PM candidates. Retrieval is local and cached.
                if self.retrievals:
                    query = self.retrievals[-1]["query"]
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
        ]
