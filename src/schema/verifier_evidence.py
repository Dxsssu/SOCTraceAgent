"""Lossless fact compaction for Verifier; never rank or discard candidates."""
from copy import deepcopy
import json


def compact_verifier_itt(itt):
    view = deepcopy(itt)
    stats = {'facts_before': 0, 'facts_after': 0}

    def visit(node):
        # Retrieval/planning bookkeeping is not evidence.
        node.pop('procedure_run_id', None)
        node.pop('semantic_refs', None)
        node.pop('template_id', None)
        result = node.get('execution_result')
        if isinstance(result, dict) and isinstance(result.get('facts'), list):
            facts = result['facts']
            stats['facts_before'] += len(facts)
            unique, seen = [], set()
            for fact in facts:
                key = json.dumps(fact, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    unique.append(fact)
            stats['facts_after'] += len(unique)
            result['facts'] = unique
            if len(facts) != len(unique):
                result['duplicate_rows_removed'] = len(facts) - len(unique)
            if len(unique) > 1 and all(isinstance(f, dict) for f in unique):
                common = {k: v for k, v in unique[0].items()
                          if all(k in f and type(f[k]) is type(v) and f[k] == v for f in unique[1:])}
                if common:
                    result['facts_common'] = common
                    result['facts'] = [{k: v for k, v in f.items() if k not in common} for f in unique]
        for child in node.get('children', []):
            visit(child)
    for root in view:
        visit(root)
    return view, stats
