"""Typed entry/terminal contracts and bounded composition of existing PM flows.

Composition creates a session-local plan, never new SQL or permanent memories.
Only projected terminal values with compatible namespaces may cross a PM boundary.
"""
from copy import deepcopy
from collections import defaultdict
import re

from .core import digest

SCOPE_FIELDS = {"Title", "AlertName", "AlertType", "EntityType", "EvidenceRole", "Category",
                "Categories", "ActionType", "OperationName", "DetectionSource", "ServiceSource"}


class MemoryContractMixin:
    def _slots_for_field(self, table, field):
        values = [{"concept": self.canonical(c), "kind": k, "field": field, "table": table}
                  for c, k in sorted(self.field_concepts(table, field))]
        return values or [{"concept": "Field:" + field, "kind": None, "field": field, "table": table}]

    def contract(self, snippet):
        explicit = snippet.get("memory_contract")
        if explicit:
            return explicit
        derived = {(edge["to"], edge.get("to_parameter"), edge["to_field"])
                   for edge in snippet["bindings"]}
        known, scopes = [], []
        for step in snippet["steps"]:
            template = self.store.templates[step["template_id"]]
            for parameter, spec in template["inputs"].items():
                if any(s == step["id"] and f == spec["field"] and p in (None, parameter) for s, p, f in derived):
                    continue
                declarations = [r for r in snippet.get("required_inputs", [])
                                if r["step"] == step["id"] and r.get("parameter") == parameter]
                tables = [r["table"] for r in declarations] or [t for t, fs in template["schema_fields"].items() if spec["field"] in fs]
                for table in tables:
                    explicit = [dict(r, concept=self.canonical(r['concept'])) for r in declarations if r.get('concept') and r['table']==table]
                    for item in explicit or self._slots_for_field(table, spec["field"]):
                        target = scopes if spec["field"] in SCOPE_FIELDS else known
                        target.append(dict(item, parameter=parameter, step=step["id"], type=spec["type"]))
        wanted = []
        sinks = [s for s in snippet['steps'] if s['id'] not in {e['from'] for e in snippet['bindings']}]
        for final in sinks:
            template = self.store.templates[final['template_id']]
            for column in template["outputs"]:
                declared = final.get("outputs", {}).get(column, {}).get("entity_concept")
                matches = [r for t, fs in template["schema_fields"].items() if column in fs
                           for r in self._slots_for_field(t, column)]
                if declared:
                    matches.append({"concept": self.canonical(declared), "kind": None})
                for item in matches:
                    wanted.append(dict(item, column=column, step=final["id"], composition_allowed=column not in {"Entities", "AdditionalFields", "ExtendedProperties"}))
            for item in snippet.get("evidence_outputs", []):
                if item["column"] in template["outputs"] and item.get("transform", "identity") == "identity" and not item.get("json_path"):
                    wanted.append(dict(item, concept=self.canonical(item["concept"]), step=final["id"], composition_allowed=not item.get("json_path")))
        targets = {self.canonical(c) for c in snippet.get("target_concepts", [])}
        effective = [x for x in wanted if not targets or x["concept"] in targets]
        return {"version": "memory-contract-1", "known": known, "scope_inputs": scopes,
                "wanted": wanted, "effective_wanted": effective,
                "candidate_wanted": snippet.get("candidate_output_concepts", [])}

    def terminal_outputs(self, snippet, variant_id=None):
        contract = self.contract(snippet)
        variant_id = variant_id or snippet.get("matched_variant_id")
        if variant_id:
            selected = next((v for v in contract.get("variants", []) if v["id"] == variant_id), None)
            if selected:
                contract = {**contract, **selected}
        outputs = contract.get("effective_wanted", contract.get("goal_wanted", contract.get("wanted", [])))
        targets = {self.canonical(c) for c in snippet.get("target_concepts", [])} if not variant_id else set()
        # A declared workflow goal constrains broad SELECT * output annotations.
        return [x for x in outputs if not targets or self.canonical(x["concept"]) in targets]

    def slot_matches(self, slot, concept, kind=None, *, namespace=None):
        physical_alias = concept.startswith("Field:") and concept[6:] == slot.get("field")
        if self.canonical(slot["concept"]) != self.canonical(concept) and not physical_alias:
            return False
        expected = slot.get("kind")
        policy = self.semantic.memory.concepts.get(self.canonical(slot['concept']), {}).get('kind_policy')
        # IP role labels are retrieval hints in SM: an observed sender IP may
        # legitimately be searched as a login/remote IP. This does not equate
        # the resulting roles; wanted and automatic bridges remain strict.
        if kind and expected and kind != expected and "physical_field_or_scope" not in (kind, expected) and policy != 'hint':
            return False
        if namespace and slot.get("namespace") and namespace != slot["namespace"]:
            return False
        return True

    def entity_matches(self, slot):
        return [e for e in self.entity_nodes.values()
                if (not getattr(self, "query_goal_id", None) or e["goal_id"] == self.query_goal_id)
                and self.slot_matches(slot, e["concept"], e.get("kind"), namespace=e.get("namespace"))]

    def entry_match(self, snippet, known):
        contract = self.contract(snippet)
        groups = defaultdict(list)
        for slot in contract.get("known", []):
            groups[(slot.get("step", "s1"), slot.get("parameter", slot.get("field", slot["concept"])))].append(slot)
        # Scope-only discovery is valid only when its actual scope is an entry E;
        # an arbitrary IP must not match a title-only global lookup.
        if not groups:
            for slot in contract.get("scope_inputs", []):
                if slot.get("step", "s1") == snippet["steps"][0]["id"] and any(self.slot_matches(slot, c, k) for c, k in known):
                    groups[(slot.get("step", "s1"), slot.get("parameter", slot["concept"]))].append(slot)
        if not groups:
            aliases=[snippet['trigger_entity'],*snippet.get('trigger_aliases',[])]
            if len(snippet['steps'])==1 and contract.get('scope_inputs') and any(self.slot_matches(a,c,k) for a in aliases for c,k in known):
                return True, list(known), []
            return False, [], ["no_grounded_entry"]
        matches, missing, common_goals = [], [], None
        for key, alternatives in groups.items():
            available = [(slot, c, k) for slot in alternatives for c, k in known if self.slot_matches(slot, c, k)]
            if self.entity_nodes:
                available = [(slot, c, k) for slot, c, k in available if self.entity_matches(slot)]
                goals = {e['goal_id'] for slot,_,_ in available for e in self.entity_matches(slot)}
                common_goals = goals if common_goals is None else common_goals & goals
            if not available:
                missing.append(f"{key[0]}.{key[1]}")
            else:
                matches.extend((c, k) for _, c, k in available)
        for slot in contract.get('scope_inputs', []):
            matches.extend((c, k) for c, k in known if self.slot_matches(slot, c, k)
                           and (not self.entity_nodes or self.entity_matches(slot)))
        if self.entity_nodes and not common_goals:
            missing.append('required_entities_do_not_share_a_goal')
        return not missing, list(set(matches)), missing

    def set_entities(self, tree):
        entities = {n.node_id: dict(n.metadata["entity"], id=n.node_id, goal_id=g.node_id)
                    for g in tree.root_nodes for n in g.children if n.metadata.get("entity")}
        fingerprint = digest([{k: v for k, v in e.items() if k != "source_refs"} for e in entities.values()])
        if getattr(self, "entity_fingerprint", None) != fingerprint:
            self.tried.clear()
            self.entity_fingerprint = fingerprint
        self.entity_nodes = entities

    def covers_wanted(self, outputs, wanted):
        return all(any(self.slot_matches(o, q.concept, q.kind)
                       and (not q.kind or o.get('kind') == q.kind) for o in outputs) for q in wanted)

    def ground_inputs(self, snippet, spec, step_inputs, parent_id):
        """Reuse only unambiguous, typed E values; never synthesize an entity bind."""
        if not self.entity_nodes:
            return []
        if parent_id not in self.entity_nodes:
            raise ValueError("A memory task must attach to an existing typed E")
        goal = self.entity_nodes[parent_id]["goal_id"]
        groups = defaultdict(list)
        for slot in self.contract(snippet).get("known", []):
            groups[(slot.get("step", "s1"), slot["parameter"])].append(slot)
        anchors = []
        for (step, parameter), alternatives in groups.items():
            candidates = {e["id"]: e for slot in alternatives for e in self.entity_matches(slot) if e["goal_id"] == goal}
            inputs = spec.setdefault("inputs", {}) if step == spec.get("step", "s1") else step_inputs.setdefault(step, {})
            template = self.store.templates[next(s for s in snippet["steps"] if s["id"] == step)["template_id"]]
            item_type = template["inputs"][parameter]["type"]
            if parameter not in inputs:
                values = list(dict.fromkeys(e["value"] for e in candidates.values()))
                if item_type.endswith("[]") and values:
                    inputs[parameter] = values
                elif len(values) == 1:
                    inputs[parameter] = values[0]
                else:
                    raise ValueError(f"Select an observed E value for {step}.{parameter}; multiple or missing inputs")
            supplied = inputs[parameter] if isinstance(inputs[parameter], list) else [inputs[parameter]]
            for value in supplied:
                matched = [e for e in candidates.values() if e["value"] == value or
                           template["inputs"][parameter]["operator"] == "LIKE" and str(value).strip("%") == str(e["value"])]
                if not matched:
                    raise ValueError(f"{step}.{parameter} must use a value from a compatible ITT E")
                anchors.extend(e["id"] for e in matched)
        return list(dict.fromkeys([parent_id, *anchors]))

    def observation_clues(self, context, observation):
        entities = {(self.canonical(e["concept"]), str(e["value"])) for e in self.entity_nodes.values()}
        core = {"IP", "URL", "Email_ID", "Email_Sender", "Email_Recipient", "Account_Name", "Account_UPN",
                "Device_Name", "Device_ID", "File_Name", "File_Hash", "Process_Command", "Process_ID", "Alert_ID",
                "Field:AccountSid", "Field:AccountObjectId", "Field:AadDeviceId", "Field:AccountDisplayName", "Field:UserDisplayName"}
        snippet = self.offered[context["snippet_id"]]
        template = self.store.templates[next(s for s in snippet["steps"] if s["id"] == context["step_id"])["template_id"]]
        out = []
        for row in observation["rows"]:
            for field, value in row.items():
                if value in (None, "") or type(value) not in (str, int, float):
                    continue
                concepts = [item for table, fields in template["schema_fields"].items() if field in fields for item in self._slots_for_field(table, field)]
                concepts += [x for x in snippet.get("evidence_outputs", []) if x["column"] == field and not x.get("json_path") and x.get("transform", "identity") == "identity"]
                for item in concepts:
                    concept = self.canonical(item["concept"])
                    key = (concept, str(value))
                    if concept in core and key not in entities and key not in self.em_clues_seen:
                        self.em_clues_seen.add(key)
                        out.append({"concept": concept, "kind": item.get("kind"), "value": value, "source_task": context.get("task_id")})
        return out

    def query_key(self, query):
        args = self.semantic.queries(query)
        return digest({"known": sorted({(self.canonical(x.concept), x.kind or "") for x in args["known"]}),
                       "wanted": sorted({(self.canonical(x.concept), x.kind or "") for x in args["wanted"]}),
                       "goal": query.get("goal_id"),
                       "entities": getattr(self, "entity_fingerprint", "")})

    def _bridge_matches(self, output, slot):
        if output.get("kind") != slot.get("kind"):
            return False
        if output.get("namespace") and slot.get("namespace") and output["namespace"] != slot["namespace"]:
            return False
        def value_type(value):
            value = (value or '').lower().removesuffix('[]')
            if any(x in value for x in ('char', 'text', 'string', 'uuid')):
                return 'text'
            if any(x in value for x in ('int', 'float', 'double', 'decimal', 'number')):
                return 'number'
            return value
        if output.get('type') and slot.get('type') and value_type(output['type']) != value_type(slot['type']):
            return False
        return output.get("composition_allowed", True) and self.slot_matches(
            slot, output["concept"], output.get("kind"), namespace=output.get("namespace"))

    def _compose_pair(self, first, second, known):
        first_contract, second_contract = self.contract(first), self.contract(second)
        if first_contract.get("auto_composition_allowed", first_contract.get("composition_allowed")) is False or second_contract.get("auto_composition_allowed", second_contract.get("composition_allowed")) is False:
            return None
        available = self.terminal_outputs(first)
        bridges = []
        groups = defaultdict(list)
        for slot in second_contract.get("known", []):
            groups[(slot.get("step", "s1"), slot.get("parameter"))].append(slot)
        if not groups:
            return None
        for alternatives in groups.values():
            if any(any(self.slot_matches(s, c, k) for c, k in known) for s in alternatives):
                continue
            link = next(((out, slot) for slot in alternatives for out in available if self._bridge_matches(out, slot)), None)
            if link is None:
                return None
            bridges.append(link)
        if not bridges:
            return None
        components = first.get("component_ids", [first["id"]]) + [second["id"]]
        if len(components) != len(set(components)):
            return None
        steps, maps = [], []
        for snippet in (first, second):
            renames = {}
            for step in snippet["steps"]:
                renames[step["id"]] = "s" + str(len(steps) + 1)
                steps.append(dict(deepcopy(step), id=renames[step["id"]]))
            maps.append(renames)
        edges = []
        for snippet, mapping in zip((first, second), maps):
            edges.extend(dict(deepcopy(edge), **{"from": mapping[edge["from"]], "to": mapping[edge["to"]]}) for edge in snippet["bindings"])
        for output, slot in bridges:
            target = next(st for st in second['steps'] if st['id'] == slot.get('step', 's1'))
            target_field = self.store.templates[target['template_id']]['inputs'][slot['parameter']]['field']
            edges.append({"from": maps[0][output.get("step", first["steps"][-1]["id"])],
                          "to": maps[1][slot.get("step", "s1")], "from_column": output["column"],
                          "to_field": target_field, "to_parameter": slot["parameter"], "transform": "identity"})
        required = [dict(deepcopy(r), step=m[r["step"]]) for snippet, m in zip((first, second), maps) for r in snippet.get("required_inputs", [])]
        required = [r for r in required if not any(e["to"] == r["step"] and e.get("to_parameter") == r.get("parameter") for e in edges)]
        remaining = [dict(deepcopy(s), step=m[s.get("step", "s1")]) for contract, m in zip((first_contract, second_contract), maps) for s in contract.get("known", [])]
        remaining = [s for s in remaining if not any(e["to"] == s["step"] and e.get("to_parameter") == s.get("parameter") for e in edges)]
        terminal = [dict(deepcopy(o), step=maps[1][o.get("step", second["steps"][-1]["id"])]) for o in self.terminal_outputs(second)]
        sid = "composed_" + digest({"components": components, "bindings": edges})
        snippet = dict(deepcopy(first), id=sid, name=" → ".join([first["name"], second["name"]]),
            intent=first["intent"] + "；" + second["intent"], steps=steps, bindings=edges,
            required_inputs=required, component_ids=components, composed=True,
            target_concepts=sorted({o["concept"] for o in terminal}),
            caution=list(dict.fromkeys(first["caution"] + second["caution"] + ["组合仅承诺模板类型兼容；中间值必须来自真实成功结果，角色与事件范围仍须核对。"])))
        snippet.pop("intent_variants", None)
        snippet.pop("matched_variant_id", None)
        snippet["memory_contract"] = {"version": "memory-contract-1", "known": remaining,
            "scope_inputs": [dict(deepcopy(s), step=m[s.get("step", "s1")]) for c, m in zip((first_contract, second_contract), maps) for s in c.get("scope_inputs", [])],
            "wanted": terminal, "effective_wanted": terminal}
        return snippet

    def retrieve_compositions(self, query, args):
        known = [(self.canonical(x.concept), x.kind) for x in args["known"]]
        wanted = {self.canonical(x.concept) for x in args["wanted"]}
        base = [s for s in self.store.snippets.values() if self.memory_type(s) == "procedural"
                and s["id"] not in self.disabled and not s.get("composed")]
        context = (args["intent"] + " " + self.semantic.default_intent).casefold()
        def words(text):
            result=set(re.findall(r'[a-z][a-z0-9_]+',text.casefold()))
            for run in re.findall(r'[\u4e00-\u9fff]+',text):
                result.update(run[i:i+2] for i in range(len(run)-1))
            return result
        query_words=words(context+' '+getattr(self.semantic,'goal_intent',''))
        resolved=[]
        for source in base:
            snippet=deepcopy(source)
            variants=snippet.get('intent_variants',[])
            if variants:
                variant=max(variants,key=lambda v:len(query_words & words(' '.join([v['name'],v['intent'],v.get('goal_intent',''),*v.get('intent_aliases',[])]))))
                snippet.update(matched_variant_id=variant['id'],name=variant['name'],intent=variant['intent'],caution=variant['caution'])
                guides={g['step']:g['guide'] for g in variant.get('step_guides',[])}
                for step in snippet['steps']:
                    step['planner_guide']=guides.get(step['id'],step['planner_guide'])
            resolved.append(snippet)
        base=resolved
        base = [s for s in base if not any(not any(t.casefold() in context for t in group) for group in s.get("required_intent_groups", []))]
        schema = self.semantic.memory.schema
        if schema is None:
            base = []
        else:
            base = [s for s in base if all(t in schema and set(fs) <= set(schema[t]) for st in s["steps"]
                    for t, fs in self.store.templates[st["template_id"]]["schema_fields"].items())]
        prefixes = [s for s in base if self.entry_match(s, known)[0]]
        hits, explored = {}, 0
        for depth in (2, 3):
            next_prefixes = []
            for first in prefixes[:64]:
                for second in base:
                    explored += 1
                    combined = self._compose_pair(first, second, known)
                    if combined is None:
                        continue
                    outputs = {self.canonical(o["concept"]) for o in self.terminal_outputs(combined)}
                    if wanted and self.covers_wanted(self.terminal_outputs(combined), args['wanted']):
                        combined["retrieval_coverage"] = {"mode": "direct", "covered_wanted": sorted(wanted), "missing_wanted": []}
                        hits[combined["id"]] = combined
                    elif depth < 3:
                        next_prefixes.append(combined)
            prefixes = next_prefixes
        chosen = sorted(hits.values(), key=lambda s: (len(s["steps"]), s["id"]))[:3]
        for snippet in chosen:
            self.store.snippets[snippet["id"]] = snippet
            self.offered[snippet["id"]] = snippet
            self.offered_queries[snippet["id"]] = deepcopy(query)
        record = {"lookup_id": "CPM" + str(1 + sum(r.get("layer") == "composed" for r in self.retrievals)),
            "memory_type": "procedural", "layer": "composed", "query": deepcopy(query),
            "route": "composed_candidate" if chosen else "episodic", "reason": "typed_terminal_to_entry_composition",
            "candidates": [self.view(s) for s in chosen], "search": {"max_components": 3, "examined_pairs": explored}}
        self.retrievals.append(record)
        return record
