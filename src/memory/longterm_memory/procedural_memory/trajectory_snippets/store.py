"""Prepare one step of an evidenced DAG; it never executes SQL or selects a row silently."""

from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json
from .core import compile_sql, digest, json_values_with_paths


class TrajectorySnippetStore:
    def __init__(self, document):
        expected = document["catalog_id"]
        unsigned = {k: v for k, v in document.items() if k != "catalog_id"}
        if digest(unsigned) != expected:
            raise ValueError("catalog_digest_mismatch")
        if document["format_version"] != "trajectory-snippets-2":
            raise ValueError("unsupported_format")
        self.document = deepcopy(document)
        self.templates = self.document["templates"]
        self.snippets = {s["id"]: s for s in self.document["snippets"]}
        if len(self.snippets) != len(self.document["snippets"]):
            raise ValueError("duplicate_id")
        for s in self.snippets.values():
            step_ids = [step["id"] for step in s["steps"]]
            if len(step_ids) != len(set(step_ids)):
                raise ValueError("duplicate_step")
            for step in s["steps"]:
                if step["template_id"] not in self.templates:
                    raise ValueError("unknown_template")
            for link in s["bindings"]:
                if (
                    link["from"] not in step_ids
                    or link["to"] not in step_ids
                    or step_ids.index(link["from"]) >= step_ids.index(link["to"])
                ):
                    raise ValueError("non_forward_dependency")
                source = next(st for st in s["steps"] if st["id"] == link["from"])
                if (
                    link["from_column"]
                    not in self.templates[source["template_id"]]["outputs"]
                ):
                    raise ValueError("dependency_output_missing")

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def retrieve(self, entity, intent, kind=None):
        def norm(v):
            return "".join(str(v).casefold().split())

        hits = []
        for s in self.snippets.values():
            if s["trigger_entity"]["concept"] != entity:
                continue
            if kind and s["trigger_entity"]["kind"] != kind:
                continue
            if norm(intent) not in {
                norm(x) for x in [s["id"], s["intent"], *s.get("intent_aliases", [])]
            }:
                continue
            matched = [
                v
                for v in s.get("intent_variants", [])
                if norm(v["intent"]) == norm(intent)
            ]
            for variant in matched or [None]:
                hit = deepcopy(
                    {
                        k: s[k]
                        for k in [
                            "id",
                            "name",
                            "kind",
                            "intent",
                            "trigger_entity",
                            "required_inputs",
                            "steps",
                            "bindings",
                            "caution",
                            "status",
                        ]
                    }
                )
                if variant:
                    hit.update(
                        name=variant["name"],
                        intent=variant["intent"],
                        caution=variant["caution"],
                        intent_variant_id=variant["id"],
                    )
                    guides = {g["step"]: g["guide"] for g in variant["step_guides"]}
                    for step in hit["steps"]:
                        step["planner_guide"] = guides[step["id"]]
                hits.append(hit)
        return {
            "route": "procedural_candidate" if hits else "semantic",
            "candidates": hits,
        }

    def prepare_step(
        self,
        snippet_id,
        step_id,
        *,
        schema,
        external_inputs,
        observations=None,
        selections=None,
        allow_candidates=False,
    ):
        snippet = self.snippets[snippet_id]
        if snippet["status"] != "active" and not allow_candidates:
            raise ValueError("candidate_requires_explicit_validation_opt_in")
        if schema is None:
            raise ValueError("live_schema_required")
        step = next(s for s in snippet["steps"] if s["id"] == step_id)
        template = self.templates[step["template_id"]]
        observations = observations or {}
        selections = selections or {}
        bindings = {}
        derived_keys = set()
        for edge in snippet["bindings"]:
            if edge["to"] != step_id:
                continue
            upstream = observations.get(edge["from"])
            if not upstream or not upstream.get("success") or upstream.get("truncated"):
                raise ValueError("dependency_missing_failed_or_truncated")
            rows = upstream.get("rows", [])
            raw_values = [
                r[edge["from_column"]]
                for r in rows
                if r.get(edge["from_column"]) not in ("", None)
            ]
            if edge.get("transform") == "parse_json":
                extracted = []
                for raw in raw_values:
                    try:
                        data = json.loads(raw) if isinstance(raw, str) else raw
                    except (ValueError, TypeError):
                        raise ValueError("dependency_json_invalid")
                    extracted.extend(
                        v
                        for path, v in json_values_with_paths(data)
                        if list(path) == edge["json_path"]
                        and isinstance(v, (str, int, float))
                    )
                raw_values = extracted
            candidates = list(dict.fromkeys(raw_values))
            if not candidates:
                raise ValueError("dependency_has_no_values")
            selected = selections.get(edge["from"] + "." + edge["from_column"])
            if selected is None:
                if len(candidates) != 1:
                    raise ValueError("ambiguous_dependency_requires_explicit_selection")
                selected = candidates
            if (
                not isinstance(selected, list)
                or not selected
                or not all(v in candidates for v in selected)
            ):
                raise ValueError("selection_not_supported_by_observation")
            keys = [
                k
                for k, spec in template["inputs"].items()
                if spec["field"] == edge["to_field"]
                and (not edge.get("to_parameter") or k == edge["to_parameter"])
            ]
            if len(keys) != 1:
                raise ValueError("ambiguous_target_parameter")
            key = keys[0]
            spec = template["inputs"][key]
            if edge.get("to_operator") and spec["operator"] != edge["to_operator"]:
                raise ValueError("dependency_operator_mismatch")
            if spec["type"].endswith("[]"):
                value = selected
            elif len(selected) == 1:
                value = selected[0]
            else:
                raise ValueError("scalar_input_cannot_accept_multiple_values")
            if key in bindings and bindings[key] != value:
                raise ValueError("conflicting_dependencies")
            bindings[key] = value
            derived_keys.add(key)
        if set(external_inputs) & derived_keys:
            raise ValueError("cannot_override_derived_input")
        bindings.update(external_inputs)
        sql, params = compile_sql(template, bindings, schema)
        return {
            "sql": sql,
            "params": params,
            "template_id": template["id"],
            "outputs": template["outputs"],
            "row_cap": template["row_cap"],
            "scope": template["scope"],
            "bindings": bindings,
        }
