"""Evidence-backed G/E preparation before any retrieval or T allocation."""
import re

from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.schema.itt import compact_json, itt_payload
from src.schema.planner_operations import apply_planner_operations, parse_planner_response


class StagedPlanningMixin:
    def _apply_entity_updates(self, value, inputs, event_id, round_id, *, previous=None, executions=()):
        pm = self._procedural_session
        original = " ".join(str(inputs.get(k, "")) for k in ("context", "question"))
        execution_map = {e.execution_id: e for e in executions if e.query_success}
        old_entities = {(g.node_id, n.metadata['entity']['concept'], n.metadata['entity'].get('kind'), str(n.metadata['entity']['value']))
                        for g in previous.root_nodes for n in g.children if n.metadata.get('entity')} if previous else set()
        aliases = {'$'+op['ref']: 'G'+str(i+1) for i,op in enumerate(
            op for op in value['operations'] if op['op']=='add_goal') if 'ref' in op}
        allowed = {"add_goal", "add_entity"} if previous is None else {"add_entity"}
        for op in value["operations"]:
            if op["op"] not in allowed:
                raise ValueError("Entity phase permits G/E only; do not create or select T before memory retrieval")
            if op["op"] != "add_entity":
                continue
            if "concept" not in op or "value" not in op:
                raise ValueError("E requires concept and value; retain the observed identifier namespace")
            refs = op.get("source_refs", [])
            if not isinstance(refs, list) or any(r not in execution_map for r in refs):
                raise ValueError("E source_refs must reference actual successful execution IDs")
            source = " ".join(execution_map[r].observation for r in refs) if refs else original
            pattern = r"(?<![\w])" + re.escape(str(op["value"])) + r"(?![\w])"
            if not re.search(pattern, source, re.I):
                # JSON escaping in a raw cell is a representation difference.
                decoded = source.replace('\\"', '"').replace('\\\\', '\\')
                if not re.search(pattern, decoded, re.I):
                    raise ValueError("New E value must be present in the question/context or cited successful result")
            op["concept"] = pm.canonical(op["concept"])
            identity = (aliases.get(op['goal_id'],op['goal_id']), op['concept'], op.get('kind'), str(op['value']))
            if identity in old_entities:
                raise ValueError("Reuse the existing E ID instead of duplicating an observed entity")
            old_entities.add(identity)
        return apply_planner_operations(value, event_id, round_id, previous=previous,
            executions=executions, allow_pending_entities=True)

    def _plan_entity_stage(self, inputs, event_id, round_id, *, previous=None, executions=()):
        pm = self._procedural_session
        def validate(value):
            return self._apply_entity_updates(value, inputs, event_id, round_id, previous=previous, executions=executions)
        payload = {**inputs, "planning_phase": "initialize_entities" if previous is None else "update_entities",
            "instruction": "先只建立G/E，不建立T、不检索记忆。G写调查目标，E必须含concept、value，可含kind；只使用问题/context或真实SQL结果中的值。后续新增E引用source_refs中的execution_id；已有实体不重复。更新时没有新实体可operations: []。",
            "entity_concepts": pm.vocabulary(),
            "new_observed_clues": pm.pending_clues[:12]}
        if previous is not None:
            payload["itt"] = itt_payload(previous, executions, planner_view=True)
        value = self._call_yaml_role("planner", PLANNER_SYSTEM_PROMPT, compact_json(payload),
                                    validator=validate, response_parser=parse_planner_response)
        tree = validate(value)
        self._entity_snapshots.append(tree.to_dict())
        pm.set_entities(tree)
        pm.pending_clues = []
        self._record_trace("itt_entities_initialized" if previous is None else "itt_entities_updated",
                           {"operations": value["operations"], "itt": tree.to_dict()})
        return tree

    def _validate_known_entities(self, query):
        pm = self._procedural_session
        if not pm or not self.staged_planning:
            return
        args = self._semantic_session.queries(query)
        pm.query_goal_id = query.get('goal_id')
        missing = [q.concept for q in args["known"]
                   if not pm.entity_matches({"concept": pm.canonical(q.concept), "kind": q.kind})]
        if missing:
            raise ValueError("known must reference concepts actually present in ITT E nodes: " + ", ".join(missing))
