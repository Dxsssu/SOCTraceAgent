"""Planner router: multi-step Procedural -> single-step Episodic -> Semantic."""

from copy import deepcopy
from collections import Counter
import json
import math
import re

import sqlglot
from sqlglot import exp
import yaml

from .core import observation_rows
from .contracts import MemoryContractMixin
from src.memory.longterm_memory.semantic_memory.v2.store import _normalize

# Retrieval spelling aliases, never SQL column names or parameter conversions.
MEMORY_ALIASES = {
    "Account_SID": "Field:AccountSid",
    "Account_AAD_ID": "Field:AccountObjectId",
    "AAD_User_ID": "Field:AccountObjectId",
    "AadUserId": "Field:AccountObjectId",
    "Field:AadUserId": "Field:AccountObjectId",
}
DISPLAY_NAMES = {"Field:UserDisplayName", "Field:AccountDisplayName"}
ACCOUNT_LABELS = DISPLAY_NAMES | {"Account_Name", "Account_UPN"}
EXACT_IDENTIFIERS = {"Field:AccountSid", "Field:AccountObjectId", "Field:AadDeviceId"}


def intent_text(variant, snippet):
    return " ".join([variant["intent"], variant["name"],
                     *variant.get("intent_aliases", [])])


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


class ProceduralPlanningSession(MemoryContractMixin):
    def __init__(self, semantic_session, store=None):
        self.semantic = semantic_session
        from ..flows import load_runtime_catalog
        self.store = store or load_runtime_catalog()
        self.disabled = set(self.store.document.get("disabled_procedural_ids", []))
        self.retrievals = []
        self.offered = {}
        self.offered_queries = {}
        self.tasks = {}
        self.runs = {}
        self.tried = set()
        self.fallbacks = []
        self.needs_pm_recheck = False
        self.em_clues_seen = set()
        self.entity_nodes = {}
        self.executed_entries = set()
        self.pending_clues = []

    def canonical(self, name):
        name = MEMORY_ALIASES.get(name, name)
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
            for trigger in [snippet["trigger_entity"], *snippet.get("trigger_aliases", [])]:
                kinds = result.setdefault(trigger["concept"], set())
                if trigger.get("kind"):
                    kinds.add(trigger["kind"])
            for target in snippet.get("evidence_outputs", []):
                result.setdefault(self.canonical(target["concept"]), set())
            contract = self.contract(snippet)
            for slot in contract.get('known', []) + contract.get('scope_inputs', []):
                kinds = result.setdefault(self.canonical(slot['concept']), set())
                if slot.get('kind'):
                    kinds.add(slot['kind'])
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
            "known": deepcopy(self.contract(snippet).get("known", [])),
            "scope_inputs": deepcopy(self.contract(snippet).get("scope_inputs", [])),
            "wanted": deepcopy(self.terminal_outputs(snippet)),
            **({"component_ids": snippet["component_ids"], "composed": True} if snippet.get("composed") else {}),
            **({"target_concepts": snippet["target_concepts"]} if snippet.get("target_concepts") else {}),
            **({"required_intent_groups": snippet["required_intent_groups"]} if snippet.get("required_intent_groups") else {}),
            **({"matched_variant_id": snippet["matched_variant_id"]} if snippet.get("matched_variant_id") else {}),
            **({"coverage": snippet["retrieval_coverage"]} if "retrieval_coverage" in snippet else {}),
            **({"evidence_outputs": snippet["evidence_outputs"]} if snippet.get("evidence_outputs") else {}),
            **({"candidate_output_concepts": snippet["candidate_output_concepts"],
                "output_contract": "raw_evidence_candidates_require_role_validation"}
               if snippet.get("candidate_output_concepts") else {}),
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
        self.query_goal_id = query.get("goal_id")
        self.needs_pm_recheck = False
        key = self.query_key(query)
        requested = query.get("layer", "auto")
        order = ("procedural", "composed", "episodic")
        if requested == "semantic":
            for layer in order:
                if (key, layer) not in self.tried:
                    self.retrieve_compositions(query, args) if layer == 'composed' else self._retrieve_layer(query, args, layer)
                    self.tried.add((key, layer))
            return None
        start = order.index(requested) if requested in order else 0
        if requested != "auto":
            for layer in order[:start]:
                if (key, layer) not in self.tried:
                    self.retrieve_compositions(query, args) if layer == 'composed' else self._retrieve_layer(query, args, layer)
                    self.tried.add((key, layer))
            self.tried.discard((key, requested))
        last = None
        for memory_type in order[start:]:
            if (key, memory_type) in self.tried:
                continue
            self.tried.add((key, memory_type))
            last = self.retrieve_compositions(query, args) if memory_type == "composed" else self._retrieve_layer(query, args, memory_type)
            if last["candidates"]:
                return last
        return last

    def _retrieve_layer(self, query, args, memory_type):
        schema = self.semantic.memory.schema
        known = [(self.canonical(q.concept), q.kind) for q in args["known"]]
        wanted = {self.canonical(q.concept) for q in args["wanted"]}
        context_text = " ".join(filter(None, [args["intent"], self.semantic.default_intent]))
        inferred_known = []
        if re.search(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?", context_text):
            # A timestamp explicitly present in the user context is known evidence
            # even if Planner omitted its concept; it is not a fabricated value.
            if not any(c == "Event_Time" for c, _ in known):
                known.append(("Event_Time", None))
                inferred_known.append("Event_Time")
        focus = wanted & EXACT_IDENTIFIERS or wanted
        intent = tokens(context_text)
        primary = tokens(args["intent"] + " " + getattr(self.semantic, "goal_intent", ""))
        # Rare investigation terms distinguish workflows sharing the same entity
        # types. Generic 'query/account/time' words should not dominate ordering.
        documents = [tokens(intent_text(v, s)) for s in self.store.snippets.values()
                     if s["id"] not in self.disabled and self.memory_type(s) == memory_type
                     for v in (s.get("intent_variants") or [s])]
        frequencies = Counter(t for doc in documents for t in doc)
        def relevance(variant, snippet):
            doc = tokens(intent_text(variant, snippet))
            weight = lambda t: math.log(1 + (len(documents) + 1) / (frequencies[t] + 1))
            score = (sum(weight(t) for t in primary & doc)
                     + 0.2 * sum(weight(t) for t in (intent - primary) & doc)) / math.sqrt(max(1, len(doc)))
            goal = tokens(variant.get("goal_intent", snippet.get("goal_intent", "")))
            return score + 1.5 * sum(weight(t) for t in primary & goal) / math.sqrt(max(1, len(goal)))
        ranked = []
        rejected = Counter()
        if schema is not None:
            for snippet in self.store.snippets.values():
                if snippet.get("composed") or snippet["id"] in query.get("exclude_ids", []):
                    continue
                if (snippet["id"], getattr(self, "entity_fingerprint", "")) in self.executed_entries:
                    rejected["already_executed_with_same_entities"] += 1
                    continue
                if snippet["id"] in self.disabled:
                    continue
                if self.memory_type(snippet) != memory_type:
                    continue
                if memory_type == 'procedural' and self.contract(snippet).get('auto_execution_allowed') is False:
                    rejected['unsafe_internal_identity_or_scope_bridge'] += 1
                    continue
                if any(not any(term.casefold() in context_text.casefold() for term in group)
                       for group in snippet.get("required_intent_groups", [])):
                    rejected["investigation_scope_mismatch"] += 1
                    continue
                templates = [
                    self.store.templates[st["template_id"]] for st in snippet["steps"]
                ]
                if any(
                    t not in schema or not set(fs) <= set(schema[t])
                    for template in templates
                    for t, fs in template["schema_fields"].items()
                ):
                    rejected["schema_unavailable"] += 1
                    continue
                applicable, matched_known, missing_inputs = self.entry_match(snippet, known)
                if not applicable:
                    rejected["missing_required_known"] += 1
                    continue
                outputs = {self.canonical(item["concept"]) for item in self.terminal_outputs(snippet)}
                potential = set()
                # Raw JSON/text evidence can support an EM exploration step, but
                # cannot certify that a PM directly covers the requested outputs.
                if memory_type == "episodic":
                    potential = {self.canonical(c) for c in snippet.get("candidate_output_concepts", [])} - outputs
                overlap = wanted & outputs
                potential_overlap = wanted & potential
                # Account labels can help identify a person but are not interchangeable.
                # SID and directory-object IDs always retain their exact contracts.
                related = {c for c in wanted - outputs
                           if c in ACCOUNT_LABELS and outputs & ACCOUNT_LABELS}
                targets = {self.canonical(c) for c in snippet.get("target_concepts", [])}
                related_targets = {c for c in focus if c in ACCOUNT_LABELS and targets & ACCOUNT_LABELS}
                if targets and wanted and not (focus & targets or related_targets):
                    rejected["investigation_target_mismatch"] += 1
                    continue
                variants = snippet.get("intent_variants") or [snippet]
                eligible_variants = []
                for v in variants:
                    declared = v.get('requested_output', {}).get('entity_concept')
                    trigger = v.get('trigger_entity')
                    if declared and wanted and self.canonical(declared) not in focus:
                        if not (self.canonical(declared) in ACCOUNT_LABELS and focus & ACCOUNT_LABELS):
                            continue
                    if v is not snippet and trigger and not any(c == self.canonical(trigger['concept']) for c, _ in known):
                        continue
                    final_outputs = {self.canonical(item["concept"]) for item in self.terminal_outputs(snippet, v.get("id") if v is not snippet else None)}
                    if memory_type == "procedural" and wanted and not self.covers_wanted(self.terminal_outputs(snippet, v.get("id") if v is not snippet else None), args['wanted']):
                        continue
                    eligible_variants.append(v)
                if not eligible_variants:
                    rejected['variant_contract_mismatch'] += 1
                    continue
                variant = max(
                    eligible_variants,
                    key=lambda v: relevance(v, snippet),
                )
                outputs = {self.canonical(item["concept"]) for item in self.terminal_outputs(snippet, variant.get("id") if variant is not snippet else None)}
                overlap = wanted & outputs
                lexical = len(intent & tokens(intent_text(variant, snippet)))
                unknown = wanted - set(self.semantic.memory.concepts) - outputs
                clue_progress = memory_type == "episodic" and bool(outputs - {c for c, _ in known}) and lexical >= 1
                if (wanted and not (overlap or related or potential_overlap or clue_progress) or not wanted and lexical < 2
                        or memory_type == "procedural" and (unknown or not wanted <= outputs)):
                    rejected["output_or_intent_mismatch"] += 1
                    continue
                score = (
                    8 * (len(overlap) + 0.5 * len(related) + 0.3 * len(potential_overlap)) / max(1, len(wanted))
                    + min(relevance(variant, snippet), 12)
                    + (2 if wanted & targets or related_targets else 0)
                    # Prefer entry steps that use more of the supplied evidence.
                    + 2 * len(matched_known) / max(1, len(set(known)))
                    - 0.12 * len(templates)
                )
                hit = deepcopy(snippet)
                if variant is not snippet:
                    hit['matched_variant_id'] = variant.get('id')
                hit["retrieval_coverage"] = {
                    "mode": "direct" if wanted <= outputs else "partial",
                    "investigation_role": "clue_discovery" if clue_progress and not wanted <= outputs else "target_lookup",
                    "covered_wanted": sorted(overlap),
                    "related_wanted": sorted(related),
                    "potential_wanted": sorted(potential_overlap),
                    "missing_wanted": sorted(wanted - outputs),
                    "instruction": "Only covered outputs are promised; verify related entity roles and continue for missing goals.",
                }
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
            self.offered_queries[s["id"]] = deepcopy(query)
        record = {
            "lookup_id": ("PM" if memory_type == "procedural" else "EM") + str(1 + sum(r["memory_type"] == memory_type for r in self.retrievals)),
            "memory_type": memory_type,
            "query": deepcopy(query),
            "route": memory_type + "_candidate" if hits else ("composed" if memory_type == "procedural" else "semantic"),
            "reason": "ranked_entity_intent_candidates"
            if hits
            else "no_applicable_candidate_or_live_schema",
            "candidates": [self.view(s) for s in hits],
            "rejection_counts": dict(rejected),
            "inferred_known_from_explicit_context": inferred_known,
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
                if len(selectors) != 1 or op.get("op") not in {"add_task", "select_task"} or (op.get("op") == "select_task" and selectors != ["procedure"]) or any(
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
        if self.contract(snippet).get('auto_execution_allowed') is False:
            raise ValueError('This PM has an unsafe internal identity/scope bridge; use EM or SM')
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
        memory_query = (self.runs[run].get("memory_query", {}) if run
                        else self.offered_queries.get(sid, {}))
        bound_sql = literal_sql(template, prepared["bindings"])
        outputs = template["outputs"]
        sql_template = template["sql"]
        optimization = {}
        if self.store.document.get("query_optimization") and memory_query:
            from .query_optimization import optimize_query
            args = self.semantic.queries(memory_query)
            concepts = {self.canonical(q.concept) for q in args["known"] + args["wanted"]}
            fields = {field for table, names in template["schema_fields"].items() for field in names
                      if any(c in concepts for c, _ in self.field_concepts(table, field))}
            dependencies = {edge["from_column"] for edge in snippet["bindings"] if edge["from"] == step_id}
            dependencies.update(snippet.get("required_evidence_columns", []))
            if snippet.get("candidate_output_concepts"):
                dependencies.add("Entities")
            if step_id == snippet["steps"][-1]["id"]:
                dependencies.update(item["column"] for item in snippet.get("evidence_outputs", []))
            bound_sql, outputs, optimization = optimize_query(bound_sql, needed_fields=fields,
                                                               dependency_outputs=dependencies)
            sql_template = optimize_query(template["sql"], needed_fields=fields,
                                          dependency_outputs=dependencies)[0]
        return {
            "mode": "procedure",
            "memory_type": self.memory_type(snippet),
            "episodic_id": (sid if self.memory_type(snippet) == "episodic" else next(s for s in snippet["steps"] if s["id"] == step_id).get("episodic_id")),
            "snippet_id": sid,
            "step_id": step_id,
            "run": run,
            "spec": deepcopy(spec),
            "sql_template": sql_template,
            "bound_sql": bound_sql,
            "memory_query": deepcopy(memory_query),
            "query_optimization": optimization,
            "bindings": prepared["bindings"],
            "outputs": outputs,
            "row_cap": prepared["row_cap"],
            "caution": snippet["caution"],
        }

    def prepare(self, payload):
        if self.needs_pm_recheck:
            raise ValueError("New Episodic evidence: issue memory_query with the new clues to check Procedural first")
        clean = deepcopy(payload)
        contexts = []
        expanded = []
        for op in clean["operations"]:
            expanded.append(op)
            if op["op"] == "select_task" and "procedure" in op:
                task_id = op["task_id"]
                previous = self.tasks.get(task_id)
                if not previous or not previous.get("scheduled"):
                    raise ValueError("Select an existing scheduled procedural task")
                spec = op.pop("procedure")
                if any(spec.get(k) != previous["spec"].get(k) for k in ("id", "step", "run")):
                    raise ValueError("Preserve the scheduled task's procedure id, step and run")
                self.check_step_order(task_id)
                ctx = self.bind(spec)
                ctx.update(scheduled=True, task_id=task_id, deferred=False)
                contexts.append(ctx)
                continue
            if op["op"] == "select_task" and self.tasks.get(op["task_id"], {}).get("deferred"):
                raise ValueError("Resume a waiting procedural task with procedure inputs/selections")
            if op["op"] != "add_task":
                continue
            selectors = [k for k in ("procedure", "episode") if k in op]
            if selectors:
                if len(selectors) != 1 or any(k in op for k in ("table", "schema_probe", "semantic_refs", "template_id")):
                    raise ValueError("procedure, episode and table/schema_probe are mutually exclusive")
                key = selectors[0]
                if key == "episode" and self.retrievals:
                    latest = self.query_key(self.retrievals[-1]["query"])
                    if (latest, "episodic") not in self.tried:
                        raise ValueError("Decline current Procedural candidates with the same memory_query before selecting Episodic")
                spec = op.pop(key)
                step_inputs = deepcopy(spec.get("step_inputs", {})) if isinstance(spec, dict) else {}
                if isinstance(spec, dict):
                    spec = {k: v for k, v in spec.items() if k != "step_inputs"}
                if self.entity_nodes and isinstance(spec, dict) and not spec.get("run"):
                    snippet = self.offered.get(spec.get("id"))
                    if snippet:
                        anchors = self.ground_inputs(snippet, spec, step_inputs, op["entity_id"])
                        if op.get("input_entity_ids") and not set(anchors) <= set(op["input_entity_ids"]):
                            raise ValueError("T must link every E used by the memory's known inputs")
                        op["input_entity_ids"] = anchors
                ctx = self.bind(spec)
                expected = "procedural" if key == "procedure" else "episodic"
                if ctx["memory_type"] != expected:
                    raise ValueError("Use procedure for multi-step flows and episode for single-step Episodic snippets")
                if key == "procedure" and spec.get("run"):
                    raise ValueError("The flow tasks already exist; use select_task with procedure to resume")
                if key == "episode" and step_inputs:
                    raise ValueError("step_inputs is only for a multi-step procedure")
                if key == "procedure":
                    snippet = self.offered[ctx["snippet_id"]]
                    later = {s["id"] for s in snippet["steps"][1:]}
                    if not isinstance(step_inputs, dict) or set(step_inputs) - later or any(not isinstance(v, dict) for v in step_inputs.values()):
                        raise ValueError("step_inputs must map later step IDs to external input mappings")
                    group = sum("task_id" not in c for c in contexts)
                    ctx.update(scheduled=True, deferred=False, plan_group=group)
                contexts.append(ctx)
                if key == "procedure":
                    for step in snippet["steps"][1:]:
                        expanded.append({"op": "add_task", "entity_id": op["entity_id"],
                                         "objective": step["planner_guide"],
                                         **({"input_entity_ids": op["input_entity_ids"]} if op.get("input_entity_ids") else {})})
                        template = self.store.templates[step["template_id"]]
                        contexts.append({"mode": "procedure", "memory_type": "procedural",
                            "snippet_id": snippet["id"], "step_id": step["id"], "run": None,
                            "episodic_id": step["episodic_id"], "scheduled": True, "deferred": True,
                            "plan_group": group, "outputs": template["outputs"], "row_cap": template["row_cap"],
                            "spec": {"id": snippet["id"], "step": step["id"],
                                     "inputs": step_inputs.get(step["id"], {})},
                            "caution": snippet["caution"]})
            else:
                # A normal table task explicitly declines the fast path. Resolve its
                # schema through Semantic before validation, even when the preceding
                # query returned PM candidates. Retrieval is local and cached.
                if self.retrievals:
                    query = self.retrievals[-1]["query"]
                    key = self.query_key(query)
                    if (key, "episodic") not in self.tried:
                        if (key, "composed") not in self.tried:
                            self.retrieve_compositions(query, self.semantic.queries(query))
                        self.tried.add((key, "composed"))
                        self.tried.add((key, "episodic"))
                        self._retrieve_layer(query, self.semantic.queries(query), "episodic")
                        self.fallbacks.append({"query": deepcopy(query), "reason": "planner_declined_templates_for_semantic"})
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
        clean["operations"] = expanded
        self.semantic._clean_operations(clean)
        return clean, contexts

    def register(self, node_id, context):
        if context.get("mode") != "procedure":
            return
        run = context["run"] or node_id
        context["run"] = run
        context["spec"]["run"] = run
        record = self.runs.setdefault(run, {"id": context["snippet_id"], "observations": {},
                                            "tasks": {}, "status": "active",
                                            "memory_query": deepcopy(context.get("memory_query", {}))})
        record["tasks"][context["step_id"]] = node_id
        if not context.get("deferred"):
            record["status"] = "active"
        self.tasks[node_id] = context

    def check_step_order(self, node_id):
        context = self.tasks[node_id]
        run = self.runs[context["run"]]
        if run["status"] in {"interrupted", "completed"}:
            raise ValueError("This procedural run is no longer executable")
        for step in self.offered[run["id"]]["steps"]:
            if step["id"] == context["step_id"]:
                break
            if step["id"] not in run["observations"]:
                raise ValueError("Earlier procedural steps must execute first")

    def bind_scheduled(self, node_id):
        """Bind at dispatch time. Set parameters propagate the entire observed set;
        scalar ambiguity still requires an explicit Planner selection.
        """
        self.check_step_order(node_id)
        context = self.tasks[node_id]
        spec = deepcopy(context["spec"])
        run = self.runs[context["run"]]
        snippet = self.offered[context["snippet_id"]]
        step = next(s for s in snippet["steps"] if s["id"] == context["step_id"])
        template = self.store.templates[step["template_id"]]
        for edge in snippet["bindings"]:
            if edge["to"] != step["id"]:
                continue
            keys = [k for k, v in template["inputs"].items() if v["field"] == edge["to_field"]
                    and (not edge.get("to_parameter") or k == edge["to_parameter"])]
            if len(keys) != 1 or not template["inputs"][keys[0]]["type"].endswith("[]"):
                continue
            obs = run["observations"].get(edge["from"], {})
            from .store import dependency_candidates
            values = dependency_candidates(edge, obs.get("rows", []))
            if values:
                spec.setdefault("selections", {}).setdefault(edge["from"] + "." + edge["from_column"], values)
        bound = self.bind(spec)
        bound.update(scheduled=True, deferred=False)
        return bound

    def observe(self, node_id, text, success, truncated=False, execution_id=None):
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
        observation = self.runs[context["run"]]["observations"][context["step_id"]]
        self.executed_entries.add((context["snippet_id"], getattr(self, "entity_fingerprint", "")))
        if context["memory_type"] == "episodic" and observation["success"] and not observation["truncated"]:
            clues = self.observation_clues(context, observation)
            for clue in clues:
                clue["source_refs"] = [execution_id] if execution_id else []
            self.pending_clues = clues
            if clues:
                self.tried.clear()
                self.needs_pm_recheck = True

    def active_runs(self):
        return [
            {
                "run": key,
                "snippet_id": run["id"],
                "status": run["status"],
                "tasks": run["tasks"],
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
